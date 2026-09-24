-- What a native writer (e.g. PAMGuard over JDBC) sends to Nereus.
-- No XML anywhere: a detection set is a row, detections are rows.
-- nereus/writer.py does exactly this from Python.

BEGIN;

-- 1. The detection set: who/what (the old Detections document header).
INSERT INTO nereus.detection_set
    (doc_id, user_id, algorithm_method, algorithm_software, algorithm_version)
VALUES ('PAM_2026_Mooring03_clicks', 'jdjm',
        'Click detector + classifier', 'PAMGuard', '2.02.16')
RETURNING id;                                   -- say it returns 42

-- 2. Its effort: which deployment, and when. The deployment is linked by
--    Id; if its Deployment document isn't in Nereus yet, deployment_id
--    stays NULL and is filled in when it arrives.
INSERT INTO nereus.effort (set_id, deployment_ref, deployment_id, t_start, t_end)
VALUES (42, 'PAM_2026_Mooring03',
        (SELECT id FROM nereus.deployment WHERE deployment_id = 'PAM_2026_Mooring03'),
        '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z');

-- 3. What was looked for, and at what granularity.
INSERT INTO nereus.effort_kind (set_id, ord, species_tsn, call, granularity)
VALUES (42, 0, 180473, 'Clicks', 'call');       -- harbour porpoise clicks
-- Positive seconds would be:
--   (42, 1, 180473, 'Clicks', 'binned') with bin_size_min = 1/60.0

COMMIT;

-- 4. Detections, appended in batches as PAMGuard produces them.
--    COPY is fastest (PgJDBC: CopyManager.copyIn); batched INSERT also works.
--    kind_ord says which Kind each detection answers; deployment_id is the
--    effort's deployment (NULL until known).
BEGIN;
SELECT 1 FROM nereus.detection_set WHERE id = 42 FOR UPDATE;   -- serialise appends
SELECT coalesce(max(ord) + 1, 0) FROM nereus.detection WHERE set_id = 42;  -- next ord, say 0

INSERT INTO nereus.detection
    (set_id, ord, kind_ord, deployment_id, on_effort, t_start, t_end, channel, species_tsn,
     calls, has_parameters, score, received_level_db, min_freq_hz, max_freq_hz, duration_s)
SELECT 42, v.ord, 0, e.deployment_id, true, v.t_start, v.t_end, 0, 180473,
       '{Clicks}', true, v.score, v.rl, v.fmin, v.fmax, v.dur
FROM nereus.effort e,
     (VALUES (0, timestamptz '2026-06-01T00:03:12.120Z', timestamptz '2026-06-01T00:03:12.121Z',
              0.97, 128.4, 115000, 145000, 0.00012),
             (1, '2026-06-01T00:03:12.180Z', '2026-06-01T00:03:12.181Z',
              0.91, 126.0, 116000, 144000, 0.00011))
       AS v(ord, t_start, t_end, score, rl, fmin, fmax, dur)
WHERE e.set_id = 42;
COMMIT;

-- 5. When the run ends: extend effort and rebuild the daily summaries.
UPDATE nereus.effort SET t_end = '2026-06-30T00:00:00Z' WHERE set_id = 42;
SELECT nereus.refresh_summary(42);
