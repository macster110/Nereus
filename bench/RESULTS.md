# Nereus Phase 0 benchmark

Machine: Apple M2 Pro, Darwin 26.7. PostgreSQL 17.11 (Homebrew), PostGIS 3.6.4, local socket, warm cache.
Data: 200 synthetic deployments, 1,011,824 call-level detections (bench/synth.py, seed 1).

## Ingest (ASA XML -> PostgreSQL)

| | |
|---|---|
| Source XML | 531.0 MB in 200 files |
| Ingest time (parse + COPY + summaries) | 44.2 s (22,888 detections/s) |
| Detection table on disk | 415.4 MB + 236.2 MB indexes |

## Queries (median of 5 runs, time includes fetching all rows to Python)

| Query | Rows | Time |
|---|---:|---:|
| Q1 one species, one month (all columns) | 5,778 | 45 ms |
| Q2 one species within 200 km of a point, one year | 2,269 | 5 ms |
| Q3 daily detection-positive minutes, all species, bounding box, one year (map layer) | 14,721 | 11 ms |
| Q4 hourly presence per deployment for one species (computed on the fly) | 151,015 | 183 ms |
| Q5 detections overlapping a 6-hour window, any species (range index) | 290 | 2 ms |

## Downloads

| | Rows | XML | Parquet (zstd) | Time |
|---|---:|---:|---:|---:|
| One deployment (SYN_0000) | 4,944 | 2.6 MB | 0.2 MB | 37 ms |
| Entire database | 1,011,824 | 531.0 MB | 25.9 MB | 1.8 s |

ASA XML export of one deployment: 120 ms (2.6 MB). Round trip of a synthetic document: **lossless**.
