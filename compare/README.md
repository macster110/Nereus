# Nereus (SQL) vs Tethys (XML): speed comparison on Windows

Nereus is a SQL system. Detectors such as PAMGuard write detections to it with
SQL, and people read them back with SQL. Tethys XML is only used to bring
existing data across. This test measures what that means in practice, on the
**same Windows machine** with **the same data**:

1. **Build.** Make the PostgreSQL equivalent of your Tethys database: download
   every Deployment and Detections document from Tethys and import it into Nereus.
2. **Read.** Ask both the same questions: SQL against Nereus, XQuery against
   Tethys. Check that the answers match, and time pulling out every detection.
3. **Write.** Write the same new detections to both: Nereus with the native SQL
   writer (`nereus/writer.py`), Tethys as XML documents through its import API,
   its only way in. Then check the uploads landed in both, and remove them.

## What you need

| | |
|---|---|
| Tethys 3.2 | The usual install (`Server\`, `Python39\`, `databases\demodb\`), started with `databases\demodb\tethys.bat` |
| PostgreSQL 17 + PostGIS | [EDB installer](https://www.enterprisedb.com/downloads/postgres-postgresql-downloads) for PostgreSQL 17. At the end, let **Stack Builder** run and pick *Spatial Extensions → PostGIS 3.x*. You don't need to create a database or remember the installer's password: the script makes its own private cluster. |
| Python 3.10+ | From [python.org](https://www.python.org/downloads/windows/), with the *py launcher* option ticked. (Tethys's bundled Python 3.9 is too old and isn't touched.) |
| Disk | About 2× the Tethys `db` folder free |

## Run it

1. Start Tethys (`tethys.bat`) and wait for "Server starting".
2. Open PowerShell in the Nereus folder. A quick smoke test first (about 15–30 minutes, most of it the download and import):

   ```powershell
   powershell -ExecutionPolicy Bypass -File compare\run_windows.ps1 -TethysDb C:\path\to\Tethys\databases\demodb -Quick
   ```

3. Then the full run. The download and import are reused from step 2, so skip them:

   ```powershell
   powershell -ExecutionPolicy Bypass -Command "& .\compare\run_windows.ps1 -TethysDb 'C:\path\to\Tethys\databases\demodb' -Extra @('--skip-download','--skip-build')"
   ```

   (Use `-Command` whenever you pass `-Extra`: with `-File`, PowerShell hands
   `"--skip-download","--skip-build"` over as one argument.)

   Expect roughly an hour, mostly spent waiting on Tethys.

4. Send back the `compare\results\<date-time>\` folder. It has `report.md`,
   `results.json` with every timing, and the exact XQuery sent to Tethys.

The first run creates `.venv` (Python packages) and `.pgdata` (a PostgreSQL
cluster on port 5439 that only runs while you use it). Tethys's own database
is only read, apart from the write test's `NEREUSBENCH_*` documents, which are
removed at the end. Add `-NoUpload` to skip the write test. If a run is
interrupted, the next run removes any leftovers first.

## Writing: what's measured

The same synthetic PAMGuard-style detections go to both: 80 % click
detections with the usual measurements, 20 % whistles with ~50-point contours.

| Scenario | Nereus | Tethys |
|---|---|---|
| **New datasets**: 10 × 20,000 detections | SQL writer, timed with `COPY` and with batched `INSERT` (the two ways a Java/JDBC writer would do it) | Write each Detections XML document, then upload it. Both parts are timed and reported separately. |
| **Appending**: 10 batches of 1,000 added to a 20,000-detection dataset, as a detector does during a deployment | Each batch is one transaction of new rows | Tethys can't append: each batch means rewriting and re-uploading the whole, growing document |

Afterwards both systems are queried to confirm every detection arrived.
Change the sizes with `--upload-sets`, `--upload-size`, `--append-base`,
`--append-batches` and `--append-size`.

## Reading: what's measured

| Key | Question | Why it matters |
|---|---|---|
| q1a | Every detection of the most-detected species in its busiest month | Big result set |
| q1b | Same for a rarely detected species | Small result: shows per-query overhead |
| q2 | Everything detected on the busiest deployment | Filtering by deployment |
| q3 | One species, one year, deployments inside a 2°×2° box | Spatial join with Deployments |
| q4 | Which analyses looked for the species (effort) | What `getDetectionEffort` does |
| q5 | Days with detections, per deployment | Presence summaries (Tethys: fetch, then aggregate in pandas) |
| q6 | Every whistle contour in the biggest contour document | Heavy nested data |
| q7 | Catalogue of every document: position, effort span, detection count | What a map viewer loads first |
| all | Every detection: Nereus SQL into a DataFrame and to Parquet, against Tethys downloading every document | Bulk extraction |

Parameters are picked from the data and listed in the report. Timing is end
to end (query, transfer and a pandas DataFrame), because that's what a user
waits for. Tethys is asked two ways:

* **XQuery**: hand-written, in the same predicate style Tethys's own query
  translator produces. This is Tethys at its best.
* **R/MATLAB route**: the `select`/`return` JSON its clients send, which
  Tethys translates itself. This is what users actually get.

Nereus and Tethys row counts are compared for every question. A ✗ means the
answers differ, and that timing shouldn't be trusted until it's explained.

## Keeping it fair

* Same machine and same documents: Nereus is built from what this Tethys serves.
* Tethys's XQuery **result cache** (on by default) answers repeated queries
  from disk, so it's switched **off** while timing. A cache hit is measured
  separately, and the original setting is restored at the end.
* The first Tethys run of each query is recorded separately (cold), then the median.
* The Nereus writer is Python (psycopg). A Java writer in PAMGuard would use
  PgJDBC; Python's per-value conversion overhead makes these numbers conservative.

## Useful options

`python -m compare.run --help` lists everything. The most useful:

| Option | |
|---|---|
| `--skip-download --skip-build` | Reuse the Nereus copy from an earlier run |
| `--skip-extract` / `--skip-upload` | Only the write test / only the read test |
| `--questions q1a_common_species_month,q4_effort` | A subset of questions |
| `--repeats 5` / `--budget 300` | Runs per query / stop repeating after this many seconds |
| `--no-json` | Skip the R/MATLAB route |
| `--keep-uploads` | Leave the `NEREUSBENCH_*` documents in place |

## Testing the harness without Tethys

`compare/mock_tethys.py` serves a folder of documents through the same REST
endpoints (queries via Saxon, `pip install saxonche`; uploads and deletes
supported). It's only for checking the harness on macOS or Linux. Its
timings say nothing about Tethys.

```bash
python -m compare.mock_tethys --detections DIR --deployments DIR --port 9780
python -m compare.run --tethys http://localhost:9780 --repeats 2
```
