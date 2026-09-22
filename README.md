# Nereus

A multi platform database for passive acoustic data.

## Phase 0 proof of concept

Nereus is a **SQL database** for passive acoustic monitoring data, built on
**PostgreSQL + PostGIS** and following the structure of the ASA/Tethys
standard (deployments, effort, detections, localizations).

* **Writing:** detectors such as PAMGuard write detections with plain SQL
  (`nereus/writer.py`; the raw statements are in
  `sql/examples/write_detections.sql`). No XML is involved, and detections can
  be appended in batches while a deployment runs.
* **Reading:** users query with SQL, directly or through thin R, MATLAB and
  Python wrappers, and get tables back.
* **Tethys compatibility:** existing Tethys/ASA XML documents can be imported
  (`nereus/ingest.py`), and any dataset can be exported as ASA XML
  (`nereus/export.py`) for exchange or archiving.

This PoC answers three questions:

1. **Can ASA Detections XML round-trip through SQL tables without losing
   anything?** Yes. All **262 real Detections documents** from the Tethys 3.2
   demo database (3.6 GB, 478,073 detections, mostly whistle contours) come back
   equivalent and still valid against Tethys's `tethys.xsd`. So do the Nilus
   examples and a test document that uses every element in the Detections schema.
2. **Is it meaningfully faster and smaller?** On ~1M detections, typical queries
   take milliseconds, and a full-database download is 26 MB of Parquet instead
   of 531 MB of XML. See [bench/RESULTS.md](bench/RESULTS.md). A head-to-head
   comparison with a real Tethys server, reading and writing, runs on
   Windows: see [compare/README.md](compare/README.md).
3. **Can a detector write straight to it with SQL?** Yes. `nereus/writer.py`
   creates detection sets and appends detections with COPY or batched INSERT
   (about 44k and 27k detections/s from Python). Sets written this way still
   export as ASA XML that validates against `tethys.xsd` (`tests/test_writer.py`).

## Quick start

Requires Homebrew `postgresql@17` and `postgis`.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
scripts/pg.sh init                     # local cluster in ./.pgdata on port 5439
.venv/bin/python -m nereus.cli roundtrip data/examples/*.xml tests/fixtures/kitchen_sink.xml
.venv/bin/python -m pytest -q tests
```

Benchmark (about 1 minute to ingest; the data is ~530 MB):

```bash
.venv/bin/python bench/synth.py bench/data --deployments 200 --per-deployment 5000
.venv/bin/python bench/run_bench.py bench/data
```

Other commands: `nereus.cli ingest FILE...`, `nereus.cli export DOC_ID OUT.xml`,
`scripts/pg.sh psql`, `scripts/pg.sh stop`. On Windows use `scripts\pg.ps1`
with the same verbs.

To validate against the Tethys schema, point `--xsd` (or `NEREUS_XSD`) at a
Tethys install's `databases/<db>/lib/schema/tethys.xsd`. With it set,
`pytest` also checks that every exported document validates.

## Layout

| Path | What |
|---|---|
| `sql/001_schema.sql` | Tables, indexes, daily summary function, read-only role |
| `nereus/writer.py` | **Native SQL writer**: create a detection set, append detections (COPY or batched INSERT) |
| `sql/examples/write_detections.sql` | The same writes as raw SQL, as a JDBC writer would send them |
| `nereus/ingest.py` | Tethys XML import: streaming parser, COPY into Postgres, strict about unknown elements |
| `nereus/export.py` | Rows back to ASA XML in schema element order, streamed |
| `nereus/canonical.py` | "Same information?" comparison used by the round-trip check (streaming) |
| `nereus/deployment.py` | Minimal Deployment import: id, position, times |
| `tests/` | Round-trip, column, summary and rejection tests |
| `bench/` | Synthetic data generator and benchmark |
| `compare/` | Nereus (SQL) vs Tethys (XML) read and write speed test, run on Windows ([README](compare/README.md)) |
| `scripts/` | Local PostgreSQL cluster: `pg.sh` (macOS/Linux), `pg.ps1` (Windows) |
| `data/examples/` | The two Nilus example documents from Tethys 3.2 |

## How the XML maps to tables

| ASA element | Stored as |
|---|---|
| `Detections` (Id, DataSource, Algorithm, UserId, Effort Start/End) | `detection_set` columns |
| `Effort/Kind` (species, call, granularity + attributes) | `effort_kind` rows |
| `Effort/AnalysisGaps` | `analysis_gap_periodic`, `analysis_gap_aperiodic` rows |
| `Detection` and every scalar in `Detection/Parameters` | `detection` columns (lists become `float8[]`) |
| `Description`, `QualityAssurance`, `BespokeData`, `MetadataInfo`, `Algorithm/Parameters`, `UserDefined` | Exact XML fragment in a text column |
| (derived) | `summary_daily`: detection-positive minutes per UTC day with effort minutes, filled at ingest |

Principles:

* **Filterable means a column.** Species, times, frequencies, scores, channel
  and granularity are typed and indexed.
* **Descriptive or free-form means verbatim.** These blocks are rarely queried
  and in some cases are `xs:any`, so the fragment is kept byte for byte.
* **Unknown means fail.** Any element the importer can't place raises
  `UnmappedElement` and the whole import rolls back, so data can never be
  dropped silently.

### What "lossless" means here

Two documents count as equivalent when they have the same elements in the same
order and nesting, the same attributes, and the same values. Timestamps are
compared as instants, so `2021-06-01T05:00:00-07:00` equals
`2021-06-01T12:00:00.000Z`. Numbers are compared as doubles (`15` = `15.0`).
Indentation, comments and `<a/>` versus `<a></a>` are ignored. Export always
writes UTC. `tests/test_roundtrip.py::test_diff_detects_changes` confirms the
comparison does catch a real change.

## Not covered yet

* **Other document types:** Deployment (only a minimal table exists, with
  location, which the benchmark fills), Localize, Calibration and Ensemble.
  They would follow the same pattern.
* **The ANSI/ASA schema itself:** validation uses Tethys 3.2's `tethys.xsd`.
  The published standard's schema hasn't been obtained yet.
* **The ASA namespace:** the examples use the Tethys namespace
  (`http://tethys.sdsu.edu/schema/1.0`). Whatever namespace a document uses is
  stored and written back, but element differences between Tethys 3.2 and the
  published ASA standard haven't been checked.
* **Summaries:** duty cycles and analysis gaps aren't yet subtracted from
  effort minutes.
* **The Tethys comparison hasn't been run yet.** Tethys's Berkeley DB XML
  build only runs on Windows; `compare/` is ready for that. It was checked on
  macOS against a stand-in server (`compare/mock_tethys.py`), where every
  question returned identical row counts from both sides.
* **Synthetic benchmark data** is uniform random. Real deployments cluster in
  time and species, which usually helps index performance.
* **Single-machine, trust-auth Postgres.** No API server, authentication or
  object storage yet. Those are Phase 1.

## Things found in Tethys 3.2 along the way

* **Schema bug:** `Detections.v1_2.xsd` line 178 declares the element as
  `name="IntensityReference_uPa "`, with a trailing space. A document that uses
  `Effort/IntensityReference_uPa` can never validate. Worth reporting to the
  Tethys team.
* **PAMGuard settings in `Algorithm/Parameters` use `xmlns=""`** (elements with
  no namespace inside the Tethys namespace). That's valid XML, but it's easy to
  lose: lxml's `cleanup_namespaces` drops it. Nereus keeps it, with a test.
