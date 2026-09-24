-- Nereus schema: relational storage for ASA/Tethys passive acoustic data.
--
-- Every element of the Tethys Deployment, Ensemble and Detections documents
-- has a column or a jsonb field here, so those documents round-trip (import,
-- export, compare). Where Nereus differs from Tethys it is in how things are
-- split into tables, not in what is stored:
--
--   * project, site and instrument are rows that many deployments share.
--     Tethys holds them as strings inside each Deployment; export writes
--     them back as those strings.
--   * The recorder (instrument) and its hydrophones/preamps (sensor) are
--     separate physical assets: hydrophones move between recorders, and a
--     Calibration belongs to the hydrophone or preamp.
--   * Two kinds of effort:
--       - recording effort: when usable audio exists (channel start/end,
--         duty cycle, QA periods; view recording_effort).
--       - analysis effort: what was looked for, over which time, at what
--         granularity (effort + effort_kind, from Detections/Effort).
--     "Absent" means analysis effort without detections, within recording
--     effort.
--   * Every detection is linked to the deployment it came from and to the
--     Effort/Kind it answers, so its granularity (call, 1 s bin, encounter)
--     is explicit.
--
-- Where things go:
--   * Tables: anything people filter, join or aggregate on, and any list of
--     uniform records that can grow large. Fixed structs are flattened into
--     columns; numeric vectors (contours) are float8[] arrays.
--   * jsonb: descriptive blocks and free-form structs read as a unit
--     (Description, QualityAssurance, MetadataInfo, contacts, Algorithm
--     parameters, UserDefined, sensor Properties). Searchable with ->, ->>,
--     @> and jsonpath; see nereus/xmljson.py for the XML <-> JSON mapping.
--   * exact_xml / user_defined_xml: NULL unless a block could not be held
--     exactly as JSON (e.g. text between child elements); then the original
--     XML is kept too, and export uses it.
--   * Values are kept as written (e.g. Tethys 0-360 longitudes); columns
--     for querying (e.g. PostGIS points) are generated from them.
--
-- Not yet imported: Calibration and Localize documents (tables below are a
-- sketch for Localize).

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE SCHEMA IF NOT EXISTS nereus;
SET search_path = nereus, public;

-- Tethys longitudes run 0-360 east; PostGIS wants -180..180.
CREATE FUNCTION lonlat_point(lon double precision, lat double precision)
RETURNS geography LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN lon IS NULL OR lat IS NULL THEN NULL
                ELSE ST_SetSRID(ST_MakePoint(CASE WHEN lon > 180 THEN lon - 360 ELSE lon END,
                                             lat), 4326)::geography END
$$;

-- ==================================================================== project
CREATE TABLE project (
    id            bigserial PRIMARY KEY,
    name          text NOT NULL UNIQUE,          -- Deployment/Project
    description   jsonb,                         -- {Objectives, Abstract, Method}
    contact       jsonb,
    metadata_info jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- A named location that a project revisits across deployments. Deployments
-- keep their own Region and SiteAliases as written.
CREATE TABLE site (
    id          bigserial PRIMARY KEY,
    project_id  bigint NOT NULL REFERENCES project(id),
    name        text NOT NULL,                   -- Deployment/Site
    location    geography(Point, 4326),          -- nominal position (curated, optional)
    properties  jsonb,
    UNIQUE (project_id, name)
);
CREATE INDEX site_location_gix ON site USING gist (location);

-- ======================================================== physical equipment
-- A recorder or complete system, tracked across deployments.
CREATE TABLE instrument (
    id            bigserial PRIMARY KEY,
    type          text NOT NULL,                 -- Deployment/Instrument/Type
    instrument_id text NOT NULL,                 -- Deployment/Instrument/InstrumentId
    manufacturer  text,
    model         text,
    owner         text,
    properties    jsonb,
    UNIQUE (type, instrument_id)
);

-- A transducer, preamp or non-acoustic sensor, tracked across deployments.
CREATE TABLE sensor (
    id           bigserial PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('hydrophone','preamplifier','depth','other')),
    serial       text NOT NULL,                  -- HydrophoneId / PreampId / SensorId
    manufacturer text,
    model        text,
    properties   jsonb,
    UNIQUE (kind, serial)
);

-- Calibration documents (import not written yet; columns follow Calibrations.xsd).
CREATE TABLE calibration (
    id                  bigserial PRIMARY KEY,
    calibration_id      text NOT NULL,           -- Calibration/Id (usually the device serial)
    sensor_id           bigint REFERENCES sensor(id),
    instrument_id       bigint REFERENCES instrument(id),   -- 'recorder' / 'end-to-end'
    t                   timestamptz NOT NULL,
    type                text NOT NULL CHECK (type IN
                          ('transducer','preamplifier','transducer+preamplifier','recorder','end-to-end')),
    process             jsonb,
    responsible_party   jsonb,
    quality_assurance   jsonb,
    intensity_ref_upa   double precision NOT NULL,
    sensitivity_dbv     double precision,
    sensitivity_dbfs    double precision,
    poles               int,
    sensitivity_low_hz  double precision,
    sensitivity_high_hz double precision,
    adc_sensitivity_db  double precision,        -- AnalogToDigitalSensity_dB (sic)
    freq_response_hz    double precision[],
    freq_response_db    double precision[],
    freq_response_type  text,
    noise_floor         jsonb,
    metadata_info       jsonb,
    UNIQUE (calibration_id, t, type)
);
CREATE INDEX calibration_sensor_ix ON calibration (sensor_id, t);

-- ================================================================ deployment
-- One instrument, placed once, for one period: a Tethys Deployment document.
CREATE TABLE deployment (
    id                    bigserial PRIMARY KEY,
    deployment_id         text NOT NULL UNIQUE,  -- Deployment/Id
    xml_namespace         text,
    root_attrs            jsonb,
    project_id            bigint NOT NULL REFERENCES project(id),
    site_id               bigint REFERENCES site(id),
    instrument_id         bigint REFERENCES instrument(id),
    deployment_number     int NOT NULL,
    alias                 text,                  -- DeploymentAlias
    site_aliases          text[],                -- SiteAliases/Site
    cruise                text,
    platform              text NOT NULL,
    region                text,
    geometry_type         text,                  -- Instrument/GeometryType: rigid | cabled
    description           jsonb,
    quality_assurance     jsonb,                 -- QA Description/ResponsibleParty; periods in recording_quality
    metadata_info         jsonb,
    exact_xml             jsonb,                 -- {block path: original XML}, only when needed
    -- Data/Audio
    audio_uri             text,
    audio_info_url        text,
    audio_service         text,
    audio_processed       text,
    audio_raw             text,
    -- Data/Tracks (points in track / track_point)
    has_tracks            boolean NOT NULL DEFAULT false,
    track_speed_unit      text,
    track_effort          jsonb,                 -- {OnPath, OffPath}
    track_uris            text[],
    track_info_url        text,
    track_service         text,
    -- DeploymentDetails
    deploy_lon            double precision,      -- as written (0-360)
    deploy_lat            double precision,
    deploy_location       geography(Point, 4326) GENERATED ALWAYS AS (lonlat_point(deploy_lon, deploy_lat)) STORED,
    deploy_elevation_instrument_m double precision,
    deploy_depth_instrument_m     double precision,
    deploy_elevation_m    double precision,
    t_deploy              timestamptz NOT NULL,
    t_deploy_audio        timestamptz,
    deploy_vessel         text,
    deploy_contact        jsonb,                 -- {"ResponsibleParty": {...}} or {"Person": ...}
    -- RecoveryDetails (all NULL when there is none, e.g. lost or still out)
    recover_lon           double precision,
    recover_lat           double precision,
    recover_location      geography(Point, 4326) GENERATED ALWAYS AS (lonlat_point(recover_lon, recover_lat)) STORED,
    recover_elevation_instrument_m double precision,
    recover_depth_instrument_m     double precision,
    recover_elevation_m   double precision,
    t_recover             timestamptz,
    t_recover_audio       timestamptz,
    recover_vessel        text,
    recover_contact       jsonb,
    sensor_reference_point text,                 -- Sensors/ReferencePoint
    ingested_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX deployment_project_ix   ON deployment (project_id, deployment_number);
CREATE INDEX deployment_site_ix      ON deployment (site_id);
CREATE INDEX deployment_instrument_ix ON deployment (instrument_id);
CREATE INDEX deployment_location_gix ON deployment USING gist (deploy_location);

-- Sensors/{Audio,Depth,Sensor}, in document order within each element.
CREATE TABLE deployment_sensor (
    deployment_id  bigint NOT NULL REFERENCES deployment(id) ON DELETE CASCADE,
    element        text   NOT NULL CHECK (element IN ('Audio','Depth','Sensor')),
    ord            int    NOT NULL,
    number         int    NOT NULL,              -- Number; channels refer to it
    sensor_ref     text   NOT NULL,              -- SensorId as written
    x_m            double precision,             -- Geometry, relative to the reference point
    y_m            double precision,
    z_m            double precision,
    name           text,
    description    text,
    hydrophone_ref text,                         -- Audio/HydrophoneId as written
    preamp_ref     text,                         -- Audio/PreampId as written
    type           text,                         -- Sensor/Type
    properties     jsonb,                        -- Sensor/Properties
    hydrophone_id  bigint REFERENCES sensor(id), -- resolved assets
    preamp_id      bigint REFERENCES sensor(id),
    sensor_id      bigint REFERENCES sensor(id), -- Depth/Sensor: from SensorId
    PRIMARY KEY (deployment_id, element, ord)
);
CREATE INDEX deployment_sensor_hydrophone_ix ON deployment_sensor (hydrophone_id);

-- SamplingDetails/Channel, in document order.
CREATE TABLE channel (
    deployment_id  bigint NOT NULL REFERENCES deployment(id) ON DELETE CASCADE,
    ord            int    NOT NULL,
    channel_number int    NOT NULL,
    sensor_number  int    NOT NULL,
    t_start        timestamptz NOT NULL,
    t_end          timestamptz NOT NULL,
    event_trigger  jsonb,                        -- {Description, Algorithm}
    has_gain       boolean NOT NULL DEFAULT false,       -- <Gain> present (it may be empty)
    has_duty_cycle boolean NOT NULL DEFAULT false,       -- <DutyCycle> present
    PRIMARY KEY (deployment_id, ord)
);

-- Sampling, Gain and DutyCycle regimens. Each applies from t until the
-- next regimen on the same channel.
CREATE TABLE channel_sampling (
    deployment_id  bigint NOT NULL,
    channel_ord    int    NOT NULL,
    ord            int    NOT NULL,
    t              timestamptz NOT NULL,
    sample_rate_khz double precision NOT NULL,
    sample_bits    int NOT NULL,
    PRIMARY KEY (deployment_id, channel_ord, ord),
    FOREIGN KEY (deployment_id, channel_ord) REFERENCES channel ON DELETE CASCADE
);
CREATE TABLE channel_gain (
    deployment_id  bigint NOT NULL,
    channel_ord    int    NOT NULL,
    ord            int    NOT NULL,
    t              timestamptz NOT NULL,
    gain_db        double precision,
    gain_rel       double precision,
    PRIMARY KEY (deployment_id, channel_ord, ord),
    FOREIGN KEY (deployment_id, channel_ord) REFERENCES channel ON DELETE CASCADE,
    CHECK ((gain_db IS NULL) <> (gain_rel IS NULL))
);
CREATE TABLE channel_duty_cycle (
    deployment_id  bigint NOT NULL,
    channel_ord    int    NOT NULL,
    ord            int    NOT NULL,
    t              timestamptz NOT NULL,
    duration_s     double precision NOT NULL,    -- RecordingDuration_s
    offset_s       double precision,             -- RecordingDuration_s/@Offfset_s (sic)
    interval_s     double precision NOT NULL,    -- RecordingInterval_s
    PRIMARY KEY (deployment_id, channel_ord, ord),
    FOREIGN KEY (deployment_id, channel_ord) REFERENCES channel ON DELETE CASCADE
);

-- QualityAssurance/Quality: periods of good or bad audio.
CREATE TABLE recording_quality (
    deployment_id  bigint NOT NULL REFERENCES deployment(id) ON DELETE CASCADE,
    ord            int    NOT NULL,
    t_start        timestamptz NOT NULL,
    t_end          timestamptz NOT NULL,
    category       text NOT NULL,                -- unverified | good | compromised | unusable
    low_hz         double precision,             -- FrequencyRange
    high_hz        double precision,
    channels       int[],                        -- NULL = all channels
    comment        text,
    PRIMARY KEY (deployment_id, ord)
);

-- Data/Tracks/Track, for gliders, towed arrays and drifters.
CREATE TABLE track (
    deployment_id  bigint NOT NULL REFERENCES deployment(id) ON DELETE CASCADE,
    ord            int    NOT NULL,
    track_id       double precision,             -- TrackId (xs:double in Tethys)
    PRIMARY KEY (deployment_id, ord)
);
CREATE TABLE track_point (
    deployment_id  bigint NOT NULL,
    track_ord      int    NOT NULL,
    ord            int    NOT NULL,
    t              timestamptz NOT NULL,
    lon            double precision,
    lat            double precision,
    location       geography(Point, 4326) GENERATED ALWAYS AS (lonlat_point(lon, lat)) STORED,
    heading_degn   double precision,
    cog_degn       double precision,
    cog_north      text,                         -- CourseOverGround_DegN/@north
    speed          double precision,
    sog            double precision,
    pitch_deg      double precision,
    roll_deg       double precision,
    elevation_m    double precision,
    ground_elevation_m double precision,
    PRIMARY KEY (deployment_id, track_ord, ord),
    FOREIGN KEY (deployment_id, track_ord) REFERENCES track ON DELETE CASCADE
);
CREATE INDEX track_point_t_ix         ON track_point (deployment_id, t);
CREATE INDEX track_point_location_gix ON track_point USING gist (location);

-- ================================================================== ensemble
-- Deployments used together, e.g. separate recorders forming a localisation
-- array. Detections with an Ensemble source name the unit in Detection/UnitId.
CREATE TABLE ensemble (
    id              bigserial PRIMARY KEY,
    ensemble_id     text NOT NULL UNIQUE,        -- Ensemble/Id
    xml_namespace   text,
    root_attrs      jsonb,
    zero_lon        double precision,            -- ZeroPosition, as written
    zero_lat        double precision,
    zero_location   geography(Point, 4326) GENERATED ALWAYS AS (lonlat_point(zero_lon, zero_lat)) STORED,
    zero_elevation_instrument_m double precision
);
CREATE TABLE ensemble_unit (
    ensemble_id    bigint NOT NULL REFERENCES ensemble(id) ON DELETE CASCADE,
    ord            int    NOT NULL,
    unit_id        int    NOT NULL,
    deployment_ref text   NOT NULL,              -- DeploymentId as written
    deployment_id  bigint REFERENCES deployment(id) ON DELETE SET NULL,
    PRIMARY KEY (ensemble_id, ord),
    UNIQUE (ensemble_id, unit_id)
);

-- ================================================================ data files
-- Audio, CPOD/FPOD, PAMGuard binaries, etc. stay outside the database; this
-- records where they are. Paths are relative to a storage root, so moving
-- data (server disk to object store) means changing one row.
CREATE TABLE storage_root (
    id        serial PRIMARY KEY,
    name      text NOT NULL UNIQUE,              -- e.g. 'server-main'
    base_uri  text NOT NULL                      -- e.g. 'file:///data/nereus/', 's3://bucket/'
);
CREATE TABLE data_file (
    id              bigserial PRIMARY KEY,
    deployment_id   bigint NOT NULL REFERENCES deployment(id),
    storage_root_id int    NOT NULL REFERENCES storage_root(id),
    rel_path        text   NOT NULL,
    format          text   NOT NULL,             -- 'wav','flac','cp1','cp3','fp1','fp3','pgdf',...
    role            text   NOT NULL CHECK (role IN ('raw','processed','detector_output','other')),
    channels        int[],                       -- NULL = all channels
    t_start         timestamptz,
    t_end           timestamptz,
    size_bytes      bigint,
    sha256          bytea,
    properties      jsonb,                       -- format-specific header fields
    registered_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (storage_root_id, rel_path)
);
CREATE INDEX data_file_deployment_ix ON data_file (deployment_id, t_start);

-- ============================================================ recording effort
-- When usable audio exists, per deployment channel: channel start/end minus
-- 'unusable' QA periods. Duty cycles are flagged, not expanded: the on/off
-- cycle is regular, so queries scale by duration_s / interval_s.
CREATE VIEW recording_effort AS
SELECT c.deployment_id,
       c.channel_number,
       tstzmultirange(tstzrange(c.t_start, c.t_end, '[)'))
         - coalesce((SELECT range_agg(tstzrange(q.t_start, q.t_end, '[)'))
                     FROM recording_quality q
                     WHERE q.deployment_id = c.deployment_id
                       AND q.category = 'unusable'
                       AND (q.channels IS NULL OR c.channel_number = ANY (q.channels))),
                    '{}'::tstzmultirange) AS usable,
       EXISTS (SELECT 1 FROM channel_duty_cycle d       -- an empty <DutyCycle/> is continuous
               WHERE d.deployment_id = c.deployment_id AND d.channel_ord = c.ord) AS duty_cycled
FROM channel c;

-- ============================================================ analysis effort
-- One row per Detections document: who analysed what, with what method.
CREATE TABLE detection_set (
    id                    bigserial PRIMARY KEY,
    doc_id                text NOT NULL UNIQUE,  -- Detections/Id
    xml_namespace         text,                  -- root namespace (Tethys or ASA)
    root_attrs            jsonb,                 -- e.g. xsi:schemaLocation, kept for round trip
    user_id               text,                  -- UserId ('' preserved)
    algorithm_method      text,
    algorithm_software    text,
    algorithm_version     text,
    algorithm_parameters  jsonb,                 -- Algorithm/Parameters (xs:any)
    algorithm_support     jsonb,                 -- [SupportSoftware, ...]
    description           jsonb,
    quality_assurance     jsonb,
    bespoke_data          jsonb,
    metadata_info         jsonb,
    exact_xml             jsonb,
    has_offeffort         boolean NOT NULL DEFAULT false,
    ingested_at           timestamptz NOT NULL DEFAULT now()
);

-- Detections/DataSource + Detections/Effort. One per detection set (as in
-- Tethys), but its own table so effort can be queried on its own ("where
-- has anyone looked for species X?"). The references are kept as written
-- and resolved to ids when the Deployment/Ensemble is known, whichever
-- document arrives first.
CREATE TABLE effort (
    set_id            bigint PRIMARY KEY REFERENCES detection_set(id) ON DELETE CASCADE,
    deployment_ref    text,                      -- DataSource/DeploymentId
    ensemble_ref      text,                      -- DataSource/EnsembleId
    deployment_id     bigint REFERENCES deployment(id) ON DELETE SET NULL,
    ensemble_id       bigint REFERENCES ensemble(id) ON DELETE SET NULL,
    t_start           timestamptz NOT NULL,      -- Effort/Start
    t_end             timestamptz NOT NULL,      -- Effort/End
    t                 tstzrange GENERATED ALWAYS AS (tstzrange(t_start, t_end, '[]')) STORED,
    intensity_ref_upa double precision           -- Effort/IntensityReference_uPa
);
CREATE INDEX effort_deployment_ix     ON effort (deployment_id);
CREATE INDEX effort_deployment_ref_ix ON effort (deployment_ref);
CREATE INDEX effort_ensemble_ref_ix   ON effort (ensemble_ref);
CREATE INDEX effort_t_gix             ON effort USING gist (t);

-- Effort/AnalysisGaps
CREATE TABLE analysis_gap_periodic (
    set_id        bigint NOT NULL REFERENCES effort(set_id) ON DELETE CASCADE,
    ord           int    NOT NULL,
    t             timestamptz NOT NULL,
    duration_s    double precision NOT NULL,
    offset_s      double precision,
    interval_s    double precision NOT NULL,
    PRIMARY KEY (set_id, ord)
);
CREATE TABLE analysis_gap_aperiodic (
    set_id  bigint NOT NULL REFERENCES effort(set_id) ON DELETE CASCADE,
    ord     int    NOT NULL,
    t_start timestamptz NOT NULL,
    t_end   timestamptz NOT NULL,
    reason  text,
    PRIMARY KEY (set_id, ord)
);

-- Effort/Kind: what was looked for, and at what granularity.
-- Positive seconds are 'binned' with a 1 s bin. Tethys gives bin size in
-- minutes (1 s is written 0.0166667), so bin_size_s rounds it back.
CREATE TABLE effort_kind (
    set_id             bigint NOT NULL REFERENCES effort(set_id) ON DELETE CASCADE,
    ord                int    NOT NULL,
    species_tsn        bigint NOT NULL,
    species_group      text,
    call               text,
    subtype            text,
    freq_measurements_hz double precision[],
    has_parameters     boolean NOT NULL DEFAULT false,
    granularity        text NOT NULL CHECK (granularity IN ('call','encounter','binned','grouped')),
    bin_size_min       double precision,         -- as written
    bin_size_s         double precision GENERATED ALWAYS AS
                         (round((bin_size_min * 60)::numeric, 3)::float8) STORED,
    first_bin_start    timestamptz,
    encounter_gap_min  double precision,
    PRIMARY KEY (set_id, ord)
);
CREATE INDEX effort_kind_species_ix ON effort_kind (species_tsn, call, granularity);

-- ================================================================ detections
-- The big table. Everything in Detection/Parameters that is a scalar is a column.
CREATE TABLE detection (
    set_id            bigint  NOT NULL REFERENCES detection_set(id) ON DELETE CASCADE,
    ord               int     NOT NULL,          -- position in document (round trip)
    kind_ord          int,                       -- the Effort/Kind this answers (NULL = ambiguous)
    deployment_id     bigint  REFERENCES deployment(id) ON DELETE SET NULL,
                                                 -- from DataSource, or UnitId via the ensemble
    data_file_id      bigint  REFERENCES data_file(id) ON DELETE SET NULL,
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
    user_defined      jsonb,                     -- Parameters/UserDefined (xs:any)
    user_defined_xml  text,                      -- original XML, only when JSON can't hold it exactly
    image             text,
    audio             text,
    comment           text,
    PRIMARY KEY (set_id, ord),
    FOREIGN KEY (set_id, kind_ord) REFERENCES effort_kind (set_id, ord)
);
-- Time-ordered appends make BRIN tiny and effective; the btrees serve
-- "species X between dates" and "deployment X", the GiST serves overlaps.
CREATE INDEX detection_species_time_ix    ON detection (species_tsn, t_start);
CREATE INDEX detection_deployment_time_ix ON detection (deployment_id, t_start);
CREATE INDEX detection_t_brin             ON detection USING brin (t_start);
CREATE INDEX detection_t_gix              ON detection USING gist (t);
-- Localizations point at detections by Event.
CREATE INDEX detection_event_ix           ON detection (set_id, event) WHERE event IS NOT NULL;
-- No index on user_defined by default: most datasets don't use it, and a GIN
-- index slows every insert. Where a project searches it, add one, e.g.
--   CREATE INDEX ON nereus.detection USING gin (user_defined jsonb_path_ops);
-- A year of CPOD/FPOD positive seconds can be millions of rows per
-- deployment; past ~1e9 rows, partition by hash(set_id) (the key leads with it).

-- =========================================================== localizations
-- SKETCH, not imported yet. Follows Localization.xsd: a Localize document
-- (localization_set + its effort), its localizations, and links from each
-- localization to the detections it was computed from, which can be on
-- several ensemble units.
CREATE TABLE localization_set (
    id                    bigserial PRIMARY KEY,
    doc_id                text NOT NULL UNIQUE,  -- Localize/Id
    xml_namespace         text,
    root_attrs            jsonb,
    deployment_ref        text,                  -- DataSource
    ensemble_ref          text,
    deployment_id         bigint REFERENCES deployment(id) ON DELETE SET NULL,
    ensemble_id           bigint REFERENCES ensemble(id) ON DELETE SET NULL,
    user_id               text,
    algorithm_method      text,
    algorithm_software    text,
    algorithm_version     text,
    algorithm_parameters  jsonb,
    algorithm_support     jsonb,
    description           jsonb,
    quality_assurance     jsonb,
    bespoke_data          jsonb,
    metadata_info         jsonb,
    exact_xml             jsonb,
    -- Effort
    t_start               timestamptz NOT NULL,
    t_end                 timestamptz NOT NULL,
    crs_subtype           text,                  -- CoordinateReferenceSystem/Subtype: Geographic, UTM, Engineering, ...
    crs_name              text,                  -- e.g. WGS84
    reference_frame       jsonb,                 -- {Anchor, Latitude, Longitude, UTMZone, Elevation_m, Datum}
    localization_types    text[] NOT NULL,       -- Bearing | PerpendicularRange | Point | Range | Track
    time_reference        text NOT NULL,         -- absolute | channel | relative | beam
    dimension             int NOT NULL
);

-- Effort/ReferencedDocuments: the Detections (or Localize) documents used.
CREATE TABLE localization_source_doc (
    set_id          bigint NOT NULL REFERENCES localization_set(id) ON DELETE CASCADE,
    idx             int    NOT NULL,             -- Document/Index, used by Reference/Index
    doc_type        text   NOT NULL CHECK (doc_type IN ('Detections','Localizations')),
    doc_ref         text   NOT NULL,             -- Document/Id as written
    detection_set_id bigint REFERENCES detection_set(id) ON DELETE SET NULL,
    PRIMARY KEY (set_id, idx)
);

CREATE TABLE localization (
    set_id          bigint NOT NULL REFERENCES localization_set(id) ON DELETE CASCADE,
    ord             int    NOT NULL,
    event           text,
    t               timestamptz NOT NULL,        -- TimeStamp
    species_tsn     bigint,
    species_group   text,
    time_ref_unit   int,                         -- References/TimeReferenceEnsembleUnit
    time_ref_channel int,                        -- References/TimeReferenceChannel
    coord_system    text NOT NULL CHECK (coord_system IN
                      ('WGS84','UTM','Cartesian','Bearing','Angular','Cylindrical')),
    -- Point-like localizations: Coordinate / CoordinateError in the coordinate
    -- system's own element order (e.g. WGS84: lon, lat, elevation).
    coord           double precision[],
    coord_error     double precision[],
    range_m         double precision,
    range_error_m   double precision,
    perp_range_m    double precision,
    perp_range_error_m double precision,
    -- Tracks: one array per component, aligned with track_t.
    is_track        boolean NOT NULL DEFAULT false,
    track_t         timestamptz[],
    track_c1        double precision[],          -- Longitude / Northing / X_m / Angle1
    track_c2        double precision[],          -- Latitude / Easting / Y_m / Angle2 or distance
    track_c3        double precision[],          -- Elevation_m / Z_m / Distance_m
    track_error     jsonb,                       -- CoordinatesError, Range/PerpendicularRange lists
    track_bounds    jsonb,                       -- CoordinateBounds
    -- WGS84 points and track starts, for maps
    location        geography(Point, 4326),
    instrument_telemetry jsonb,
    parameters      jsonb,                       -- Parameters (incl. UserDefined)
    PRIMARY KEY (set_id, ord)
);
CREATE INDEX localization_t_ix        ON localization (t);
CREATE INDEX localization_location_gix ON localization USING gist (location);

-- References/Reference: which detections this localization used. One
-- localization uses many detections, possibly on different units.
CREATE TABLE localization_detection (
    set_id          bigint NOT NULL,
    loc_ord         int    NOT NULL,
    ord             int    NOT NULL,
    doc_idx         int    NOT NULL,             -- Reference/Index -> localization_source_doc
    event_ref       text   NOT NULL,             -- Reference/EventRef -> Detection/Event
    det_set_id      bigint,                      -- resolved detection
    det_ord         int,
    PRIMARY KEY (set_id, loc_ord, ord),
    FOREIGN KEY (set_id, loc_ord) REFERENCES localization ON DELETE CASCADE,
    FOREIGN KEY (set_id, doc_idx) REFERENCES localization_source_doc,
    FOREIGN KEY (det_set_id, det_ord) REFERENCES detection (set_id, ord) ON DELETE SET NULL
);
CREATE INDEX localization_detection_det_ix ON localization_detection (det_set_id, det_ord);

-- ------------------------------------------------------------- summary layer
-- Detection-positive minutes per UTC day, per deployment/species/call, with the
-- analysed-effort minutes as denominator. Filled at ingest, used by the map.
CREATE TABLE summary_daily (
    set_id          bigint NOT NULL REFERENCES detection_set(id) ON DELETE CASCADE,
    deployment_id   bigint REFERENCES deployment(id) ON DELETE SET NULL,
    species_tsn     bigint NOT NULL,
    call            text   NOT NULL DEFAULT '',
    day             date   NOT NULL,
    dp_minutes      int    NOT NULL,             -- detection-positive minutes
    n_detections    int    NOT NULL,
    effort_minutes  double precision NOT NULL,   -- minutes of that day under effort
    PRIMARY KEY (set_id, species_tsn, call, day)
);
CREATE INDEX summary_daily_day_ix ON summary_daily (day, species_tsn);
CREATE INDEX summary_daily_deployment_ix ON summary_daily (deployment_id);

-- Rebuild the summary for one detection set.
-- Duty cycles and AnalysisGaps are not subtracted yet.
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
    SELECT e.set_id, e.deployment_id, p.species_tsn, p.call, p.day,
           p.dp_minutes, p.n_detections,
           extract(epoch FROM
               least(e.t_end,   ((p.day + 1)::timestamp AT TIME ZONE 'UTC'))
             - greatest(e.t_start, (p.day::timestamp       AT TIME ZONE 'UTC'))
           ) / 60.0
    FROM per_day p JOIN nereus.effort e ON e.set_id = p_set;
$$;

-- ================================================================== linking
-- References are stored as written and resolved to ids whichever document
-- arrives first. These run after a Deployment or Ensemble import; the
-- Detections importer resolves against what already exists.
CREATE FUNCTION link_deployment(p_id bigint) RETURNS void LANGUAGE sql AS $$
    UPDATE nereus.ensemble_unit u SET deployment_id = p_id
    FROM nereus.deployment d
    WHERE d.id = p_id AND u.deployment_ref = d.deployment_id
      AND u.deployment_id IS DISTINCT FROM p_id;

    UPDATE nereus.effort e SET deployment_id = p_id
    FROM nereus.deployment d
    WHERE d.id = p_id AND e.deployment_ref = d.deployment_id
      AND e.deployment_id IS DISTINCT FROM p_id;

    -- detections from a single-deployment source
    UPDATE nereus.detection x SET deployment_id = p_id
    FROM nereus.effort e
    WHERE e.deployment_id = p_id AND x.set_id = e.set_id
      AND x.deployment_id IS DISTINCT FROM p_id;

    -- detections from an ensemble source, on this deployment's unit
    UPDATE nereus.detection x SET deployment_id = p_id
    FROM nereus.effort e JOIN nereus.ensemble_unit u ON u.ensemble_id = e.ensemble_id
    WHERE u.deployment_id = p_id AND e.deployment_ref IS NULL
      AND x.set_id = e.set_id AND x.unit_id = u.unit_id
      AND x.deployment_id IS DISTINCT FROM p_id;

    UPDATE nereus.summary_daily m SET deployment_id = p_id
    FROM nereus.effort e
    WHERE e.deployment_id = p_id AND m.set_id = e.set_id
      AND m.deployment_id IS DISTINCT FROM p_id;
$$;

CREATE FUNCTION link_ensemble(p_id bigint) RETURNS void LANGUAGE sql AS $$
    UPDATE nereus.ensemble_unit u SET deployment_id = d.id
    FROM nereus.deployment d
    WHERE u.ensemble_id = p_id AND d.deployment_id = u.deployment_ref
      AND u.deployment_id IS DISTINCT FROM d.id;

    UPDATE nereus.effort e SET ensemble_id = p_id
    FROM nereus.ensemble s
    WHERE s.id = p_id AND e.ensemble_ref = s.ensemble_id
      AND e.ensemble_id IS DISTINCT FROM p_id;

    UPDATE nereus.detection x SET deployment_id = u.deployment_id
    FROM nereus.effort e JOIN nereus.ensemble_unit u ON u.ensemble_id = e.ensemble_id
    WHERE e.ensemble_id = p_id AND e.deployment_ref IS NULL
      AND x.set_id = e.set_id AND x.unit_id = u.unit_id
      AND x.deployment_id IS DISTINCT FROM u.deployment_id;
$$;

-- ============================================================ convenience views
-- A deployment with the Tethys strings filled back in.
CREATE VIEW deployment_flat AS
SELECT d.id, d.deployment_id, p.name AS project, s.name AS site, d.region,
       i.type AS instrument_type, i.instrument_id, d.platform, d.deployment_number,
       d.deploy_location, d.t_deploy, d.t_recover
FROM deployment d
JOIN project p         ON p.id = d.project_id
LEFT JOIN site s       ON s.id = d.site_id
LEFT JOIN instrument i ON i.id = d.instrument_id;

-- Each detection with the granularity it was reported at.
CREATE VIEW detection_granularity AS
SELECT d.set_id, d.ord, k.granularity, k.bin_size_s
FROM detection d
LEFT JOIN effort_kind k ON k.set_id = d.set_id AND k.ord = d.kind_ord;

-- ------------------------------------------------------------ read-only role
-- What R/MATLAB users would connect as for direct SQL.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nereus_reader') THEN
        CREATE ROLE nereus_reader LOGIN PASSWORD 'reader';
    END IF;
END $$;
GRANT USAGE ON SCHEMA nereus TO nereus_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA nereus TO nereus_reader;
