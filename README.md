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
* **Tethys compatibility:** existing Tethys/ASA Deployment, Ensemble and
  Detections documents can be imported (`nereus.cli ingest`), and any of them
  exported again as ASA XML (`nereus.cli export`) for exchange or archiving.

This PoC answers three questions:

1. **Can ASA XML round-trip through SQL tables without losing anything?** Yes.
   All **262 real Detections documents** from the Tethys 3.2 demo database
   (3.6 GB, 478,073 detections, mostly whistle contours) and all **871
   Deployment documents** (858 distinct, including 29 drifters and towed
   arrays with GPS tracks) come back equivalent and still valid against Tethys's
   `tethys.xsd`. So do the two demo Ensembles, the Nilus examples, and test
   documents that use every element in the Detections and Deployment schemas.
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
.venv/bin/python -m nereus.cli roundtrip data/examples tests/fixtures
.venv/bin/python -m pytest -q tests
```

Round-trip a whole Tethys database's documents (a directory means every
`.xml` in it; `--quiet` prints only documents that differ):

```bash
.venv/bin/python -m nereus.cli roundtrip <demodb>/source-docs/Deployments --quiet
```

Deployments are best imported before the Detections that refer to them, but
either order works: references are kept as written and linked when the other
document arrives.

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
`pytest` also checks that every exported document validates. Set
`NEREUS_DEMO` to `<demodb>/source-docs` and `pytest` round-trips the demo
database's Deployments and Ensembles too.

## Layout

| Path | What |
|---|---|
| `sql/001_schema.sql` | Tables, indexes, linking and daily summary functions, read-only role |
| `nereus/writer.py` | **Native SQL writer**: create a detection set, append detections (COPY or batched INSERT) |
| `sql/examples/write_detections.sql` | The same writes as raw SQL, as a JDBC writer would send them |
| `nereus/documents.py` | One entry point: import or export any supported document by its root element or Id |
| `nereus/ingest.py` | Detections import: streaming parser, COPY into Postgres, strict about unknown elements |
| `nereus/export.py` | Detections back to ASA XML in schema element order, streamed |
| `nereus/deployment.py` | Deployment import and export (project, site, instrument, channels, QA, tracks, sensors) |
| `nereus/ensemble.py` | Ensemble import and export |
| `nereus/xmljson.py` | XML blocks ↔ searchable JSON (jsonb), exact in both directions |
| `nereus/canonical.py` | "Same information?" comparison used by the round-trip check (streaming) |
| `tests/` | Round-trip, linking, JSON search, writer, summary and rejection tests |
| `bench/` | Synthetic data generator and benchmark |
| `compare/` | Nereus (SQL) vs Tethys (XML) read and write speed test, run on Windows ([README](compare/README.md)) |
| `scripts/` | Local PostgreSQL cluster: `pg.sh` (macOS/Linux), `pg.ps1` (Windows) |
| `data/examples/` | The two Nilus example documents from Tethys 3.2 |

## The data model

Nereus follows Tethys/ASA element for element, but splits things differently:

* **project, site, instrument** are rows shared by many deployments (Tethys
  repeats them as strings in every Deployment; export writes them back).
* **Recorders and hydrophones are separate assets** (`instrument`, `sensor`):
  hydrophones move between recorders, and calibrations belong to them.
* **Two kinds of effort.** *Recording* effort is when usable audio exists
  (`recording_effort` view: channel spans minus unusable QA periods).
  *Analysis* effort is what was looked for, when, and at what granularity
  (`effort`, `effort_kind`). "Absent" means analysis effort without
  detections, within recording effort.
* **Every detection knows its deployment and its effort kind**
  (`detection.deployment_id`, `detection.kind_ord`), so a 1 s positive-second
  bin and a single call can share one table and still be told apart. With an
  Ensemble source, the detection's `UnitId` picks the deployment.
* **References are kept as written and linked whichever document arrives
  first** (`link_deployment()`, `link_ensemble()`), so importing Detections
  before their Deployment is fine.

## What goes in tables, and what goes in JSON

| ASA element | Stored as |
|---|---|
| `Deployment` Project, Site, Instrument | shared `project`, `site`, `instrument` rows |
| `Deployment` single-valued fields, DeploymentDetails, RecoveryDetails | `deployment` columns (longitudes as written; `deploy_location` is generated) |
| `SamplingDetails/Channel` and its Sampling, Gain, DutyCycle regimens | `channel`, `channel_sampling`, `channel_gain`, `channel_duty_cycle` rows |
| `QualityAssurance/Quality` | `recording_quality` rows |
| `Data/Tracks/Track/Point` | `track`, `track_point` rows (with a PostGIS point) |
| `Sensors/Audio`, `Depth`, `Sensor` | `deployment_sensor` rows; HydrophoneId, PreampId, SensorId become `sensor` assets |
| `Ensemble/Unit` | `ensemble`, `ensemble_unit` rows |
| `Detections` header (Id, Algorithm method/software/version, UserId) | `detection_set` columns |
| `DataSource`, `Effort` Start/End | `effort` (one per detection set) |
| `Effort/Kind` (species, call, granularity + attributes) | `effort_kind` rows |
| `Effort/AnalysisGaps` | `analysis_gap_periodic`, `analysis_gap_aperiodic` rows |
| `Detection` and every scalar in `Detection/Parameters` | `detection` columns; contours and other number lists are `float8[]` |
| `Description`, `QualityAssurance` descriptions, `MetadataInfo`, contacts, `Algorithm/Parameters`, `SupportSoftware`, `BespokeData`, `EventTrigger`, `TrackEffort`, sensor `Properties` | `jsonb` columns |
| `Detection/Parameters/UserDefined` | `detection.user_defined` (`jsonb`) |
| (derived) | `summary_daily`: detection-positive minutes per UTC day with effort minutes, filled on write |
| (sketch, not imported yet) | `localization_set`, `localization`, `localization_detection`, `calibration` |

The rule:

* **A table** for anything people filter, join or aggregate on, and for any
  list of uniform records that can grow large. Fixed structs are flattened into
  columns.
* **`jsonb`** for descriptive blocks and for lists of complicated or free-form
  structs that are read as a unit. It's still searchable:

  ```sql
  SELECT doc_id FROM nereus.detection_set
  WHERE algorithm_parameters @> '{"Classifier": {"@name": "porpoise"}}';

  SELECT t_start, (user_defined->>'ICI_ms')::float FROM nereus.detection
  WHERE user_defined ? 'ICI_ms';
  ```

  The XML ↔ JSON mapping (`nereus/xmljson.py`) is plain (`<MinICI_ms>2</MinICI_ms>`
  becomes `{"MinICI_ms": 2}`, attributes are `"@name"`, repeated elements are
  arrays), plus bookkeeping keys (`#order`, `#ns`, `#text`) that make it exact.
  Queries can ignore those.
* **Unknown means fail.** Any element the importer can't place raises
  `UnmappedElement` and the whole import rolls back, so data can never be
  dropped silently.
* **Exact, or keep the original.** Each block is converted to JSON and back
  on import and compared. The one case JSON can't hold (text *between* child
  elements) is kept as the original XML as well (`exact_xml`,
  `user_defined_xml`), and export uses it. Otherwise those columns are NULL.

### What "lossless" means here

Two documents count as equivalent when they have the same elements in the same
order and nesting, the same attributes, and the same values. Timestamps are
compared as instants, so `2021-06-01T05:00:00-07:00` equals
`2021-06-01T12:00:00.000Z`. Numbers are compared as doubles (`15` = `15.0`).
Indentation, comments and `<a/>` versus `<a></a>` are ignored. Export always
writes UTC. `tests/test_roundtrip.py::test_diff_detects_changes` confirms the
comparison does catch a real change.

## Not covered yet

* **Localize and Calibration documents:** the tables are sketched in the
  schema but there is no importer yet. They follow the same pattern.
* **Instrument identity in legacy data:** the demo HARP deployments use the
  deployment name as InstrumentId, so they produce one "instrument" each, and
  placeholder serials (a preamp called `custom` on 60 deployments) become one
  shared asset. New uploads can ask for real serials.
* **Duty cycles** are flagged in `recording_effort` but not expanded into
  on/off intervals.
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
  no namespace inside the Tethys namespace) and mix text with elements. That's
  valid XML, but it's easy to lose: lxml's `cleanup_namespaces` drops the
  `xmlns=""`, and so does lxml when *building* such elements. Nereus records it
  in the JSON (`"#ns": ""`) and writes it back explicitly, with tests.
* **Ensemble UnitId 0:** `Ensemble.xsd` declares `UnitId` as
  `xs:positiveInteger`, but both demo Ensembles number their units from 0, so
  neither validates. Nereus imports them without validation.
* **Pitch is non-negative:** `Deployment.xsd` restricts track
  `Pitch_deg` to ≥ 0, so a glider diving nose-down can't be recorded as a
  negative pitch.
