-- Nereus Phase 0 proof of concept: relational storage for ASA/Tethys Detections.
--
-- Mapping rule of thumb:
--   * Anything people filter, join or aggregate on is a typed column.
--   * Descriptive or open-ended blocks (Description, QualityAssurance,
--     MetadataInfo, BespokeData, xs:any Parameters/UserDefined) are kept as the
--     exact XML fragment so export is lossless, with a JSONB projection where
--     it is useful for searching.
--
-- Scope: Detections documents plus a minimal deployment table. Localize,
-- Calibration and Ensemble follow the same pattern and are not in this PoC.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE SCHEMA IF NOT EXISTS nereus;
SET search_path = nereus, public;

-- ---------------------------------------------------------------- deployments
-- Minimal: Detections reference a deployment by Id. The full Deployment
-- document (channels, sampling, duty cycle, sensors, tracks) is Phase 1.
CREATE TABLE deployment (
    id            bigserial PRIMARY KEY,
    deployment_id text NOT NULL UNIQUE,          -- ASA Deployment/Id
    project       text,
    site          text,
    location      geography(Point, 4326),        -- NULL when unknown
    t_deploy      timestamptz,
    t_recover     timestamptz
);
CREATE INDEX deployment_location_gix ON deployment USING gist (location);

-- ------------------------------------------------------------ detection sets
-- One row per Detections document.
CREATE TABLE detection_set (
    id                    bigserial PRIMARY KEY,
    doc_id                text NOT NULL UNIQUE,  -- Detections/Id
    xml_namespace         text,                  -- root namespace (Tethys or ASA)
    root_attrs            jsonb,                 -- e.g. xsi:schemaLocation, kept for round trip
    deployment_ref        text,                  -- DataSource/DeploymentId (as written)
    ensemble_ref          text,                  -- DataSource/EnsembleId
    deployment_id         bigint REFERENCES deployment(id),
    user_id               text,                  -- UserId ('' preserved)
    algorithm_method      text,
    algorithm_software    text,
    algorithm_version     text,
    algorithm_params_xml  text,                  -- Algorithm/Parameters (exact fragment)
    algorithm_support_xml text[],                -- SupportSoftware elements (exact fragments)
    description_xml       text,
    quality_assurance_xml text,
    bespoke_data_xml      text,
    metadata_info_xml     text,
    effort_start          timestamptz NOT NULL,
    effort_end            timestamptz NOT NULL,
    intensity_ref_upa     double precision,
    has_offeffort         boolean NOT NULL DEFAULT false,
    ingested_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX detection_set_deployment_ix ON detection_set (deployment_id);

-- Effort/AnalysisGaps
CREATE TABLE analysis_gap_periodic (
    set_id        bigint NOT NULL REFERENCES detection_set(id) ON DELETE CASCADE,
    ord           int    NOT NULL,
    t             timestamptz NOT NULL,
    duration_s    double precision NOT NULL,
    offset_s      double precision,
    interval_s    double precision NOT NULL,
    PRIMARY KEY (set_id, ord)
);
CREATE TABLE analysis_gap_aperiodic (
    set_id  bigint NOT NULL REFERENCES detection_set(id) ON DELETE CASCADE,
    ord     int    NOT NULL,
    t_start timestamptz NOT NULL,
    t_end   timestamptz NOT NULL,
    reason  text,
    PRIMARY KEY (set_id, ord)
);

-- Effort/Kind: what was looked for, and at what granularity.
CREATE TABLE effort_kind (
    set_id             bigint NOT NULL REFERENCES detection_set(id) ON DELETE CASCADE,
    ord                int    NOT NULL,
    species_tsn        bigint NOT NULL,
    species_group      text,
    call               text,
    subtype            text,
    freq_measurements_hz double precision[],
    has_parameters     boolean NOT NULL DEFAULT false,
    granularity        text NOT NULL CHECK (granularity IN ('call','encounter','binned','grouped')),
    bin_size_min       double precision,
    first_bin_start    timestamptz,
    encounter_gap_min  double precision,
    PRIMARY KEY (set_id, ord)
);
CREATE INDEX effort_kind_species_ix ON effort_kind (species_tsn, call);

-- ---------------------------------------------------------------- detections
-- The big table. Everything in Detection/Parameters that is a scalar is a column.
CREATE TABLE detection (
    set_id            bigint  NOT NULL REFERENCES detection_set(id) ON DELETE CASCADE,
    ord               int     NOT NULL,          -- position in document (round trip)
    on_effort         boolean NOT NULL,
    t_start           timestamptz NOT NULL,
    t_end             timestamptz,
    t                 tstzrange GENERATED ALWAYS AS
                        (tstzrange(t_start, coalesce(t_end, t_start), '[]')) STORED,
    input_file        text,
    count             bigint,
    event             text,
    unit_id           bigint,
    channel           bigint,
    species_tsn       bigint  NOT NULL,
    species_group     text,
    calls             text[],                    -- Call is repeatable
    has_parameters    boolean NOT NULL DEFAULT false,
    subtype           text,
    score             double precision,
    confidence        double precision,
    qa                text,                      -- QualityValueBasic
    received_level_db double precision,
    freq_measurements_db double precision[],
    snr_db            double precision,
    min_freq_hz       double precision,
    max_freq_hz       double precision,
    peak_freq_hz      double precision,
    peaks_hz          double precision[],
    duration_s        double precision,
    sideband_hz       double precision[],
    tonal_offset_s    double precision[],
    tonal_hz          double precision[],
    tonal_db          double precision[],
    has_tonal         boolean NOT NULL DEFAULT false,
    event_ref         text[],
    user_defined_xml  text,
    image             text,
    audio             text,
    comment           text,
    PRIMARY KEY (set_id, ord)
);
-- Time-ordered appends make BRIN tiny and effective; the btree serves
-- "species X between dates" and the GiST serves overlap queries.
CREATE INDEX detection_species_time_ix ON detection (species_tsn, t_start);
CREATE INDEX detection_t_brin          ON detection USING brin (t_start);
CREATE INDEX detection_t_gix           ON detection USING gist (t);

-- ------------------------------------------------------------- summary layer
-- Detection-positive minutes per UTC day, per deployment/species/call, with the
-- analysed-effort minutes as denominator. Filled at ingest, used by the map.
CREATE TABLE summary_daily (
    set_id          bigint NOT NULL REFERENCES detection_set(id) ON DELETE CASCADE,
    deployment_id   bigint REFERENCES deployment(id),
    species_tsn     bigint NOT NULL,
    call            text   NOT NULL DEFAULT '',
    day             date   NOT NULL,
    dp_minutes      int    NOT NULL,             -- detection-positive minutes
    n_detections    int    NOT NULL,
    effort_minutes  double precision NOT NULL,   -- minutes of that day under effort
    PRIMARY KEY (set_id, species_tsn, call, day)
);
CREATE INDEX summary_daily_day_ix ON summary_daily (day, species_tsn);

-- Rebuild the summary for one detection set.
-- Duty cycles and AnalysisGaps are not subtracted yet (Phase 1).
CREATE OR REPLACE FUNCTION refresh_summary(p_set bigint) RETURNS void
LANGUAGE sql AS $$
    DELETE FROM nereus.summary_daily WHERE set_id = p_set;

    WITH mins AS (      -- every UTC minute each on-effort detection touches
        SELECT d.species_tsn,
               coalesce(d.calls[1], '') AS call,
               m AS minute,
               d.ord
        FROM nereus.detection d,
             generate_series(date_trunc('minute', d.t_start),
                             date_trunc('minute', coalesce(d.t_end, d.t_start)),
                             interval '1 minute') AS m
        WHERE d.set_id = p_set AND d.on_effort
    ),
    per_day AS (
        SELECT species_tsn, call, (minute AT TIME ZONE 'UTC')::date AS day,
               count(DISTINCT minute) AS dp_minutes,
               count(DISTINCT ord)    AS n_detections
        FROM mins GROUP BY 1, 2, 3
    )
    INSERT INTO nereus.summary_daily
    SELECT s.id, s.deployment_id, p.species_tsn, p.call, p.day,
           p.dp_minutes, p.n_detections,
           extract(epoch FROM
               least(s.effort_end,   ((p.day + 1)::timestamp AT TIME ZONE 'UTC'))
             - greatest(s.effort_start, (p.day::timestamp       AT TIME ZONE 'UTC'))
           ) / 60.0
    FROM per_day p JOIN nereus.detection_set s ON s.id = p_set;
$$;

-- ------------------------------------------------------------ read-only role
-- What R/MATLAB users would connect as for direct SQL.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nereus_reader') THEN
        CREATE ROLE nereus_reader LOGIN PASSWORD 'reader';
    END IF;
END $$;
GRANT USAGE ON SCHEMA nereus TO nereus_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA nereus TO nereus_reader;
