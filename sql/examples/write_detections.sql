-- What a native writer (e.g. PAMGuard over JDBC) sends to Nereus.
-- No XML anywhere: a detection set is a row, detections are rows.
-- nereus/writer.py does exactly this from Python.

BEGIN;

-- 1. The deployment the detections came from (created if new).
INSERT INTO nereus.deployment (deployment_id)
VALUES ('PAM_2026_Mooring03')
ON CONFLICT (deployment_id) DO NOTHING;

-- 2. The detection set: who/what/when (the old Detections document header).
INSERT INTO nereus.detection_set
    (doc_id, deployment_ref, deployment_id, user_id,
     algorithm_method, algorithm_software, algorithm_version,
     effort_start, effort_end)
SELECT 'PAM_2026_Mooring03_clicks', 'PAM_2026_Mooring03', id, 'jdjm',
       'Click detector + classifier', 'PAMGuard', '2.02.16',
       '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z'
FROM nereus.deployment WHERE deployment_id = 'PAM_2026_Mooring03'
RETURNING id;                                   -- say it returns 42

-- 3. What was looked for.
INSERT INTO nereus.effort_kind (set_id, ord, species_tsn, call, granularity)
VALUES (42, 0, 180473, 'Clicks', 'call');       -- harbour porpoise

COMMIT;

-- 4. Detections, appended in batches as PAMGuard produces them.
--    COPY is fastest (PgJDBC: CopyManager.copyIn); batched INSERT also works.
BEGIN;
SELECT 1 FROM nereus.detection_set WHERE id = 42 FOR UPDATE;   -- serialise appends
SELECT coalesce(max(ord) + 1, 0) FROM nereus.detection WHERE set_id = 42;  -- next ord, say 0

INSERT INTO nereus.detection
    (set_id, ord, on_effort, t_start, t_end, channel, species_tsn, calls,
     has_parameters, score, received_level_db, min_freq_hz, max_freq_hz, duration_s)
VALUES
    (42, 0, true, '2026-06-01T00:03:12.120Z', '2026-06-01T00:03:12.121Z', 0, 180473, '{Clicks}',
     true, 0.97, 128.4, 115000, 145000, 0.00012),
    (42, 1, true, '2026-06-01T00:03:12.180Z', '2026-06-01T00:03:12.181Z', 0, 180473, '{Clicks}',
     true, 0.91, 126.0, 116000, 144000, 0.00011);
COMMIT;

-- 5. When the run ends: extend effort and rebuild the daily summaries.
UPDATE nereus.detection_set SET effort_end = '2026-06-30T00:00:00Z' WHERE id = 42;
SELECT nereus.refresh_summary(42);
