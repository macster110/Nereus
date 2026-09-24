"""Benchmark: ingest synthetic ASA XML, run typical queries, time downloads.

  python bench/synth.py bench/data --deployments 200 --per-deployment 5000
  python bench/run_bench.py bench/data

Writes bench/RESULTS.md.
"""

import io
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psycopg
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nereus.cli import DSN, roundtrip  # noqa: E402
from nereus.export import export  # noqa: E402
from nereus.ingest import ingest  # noqa: E402

REPEATS = 5


def timed(fn):
    t = time.perf_counter()
    r = fn()
    return time.perf_counter() - t, r


def median_time(conn, sql, params=()):
    times, n = [], 0
    for _ in range(REPEATS):
        t = time.perf_counter()
        rows = conn.execute(sql, params).fetchall()
        times.append(time.perf_counter() - t)
        n = len(rows)
    return statistics.median(times), n


def to_parquet(conn, sql, params, path: Path):
    """Run a query and write the result as zstd Parquet (what a download would send)."""
    buf = io.BytesIO()
    with psycopg.ClientCursor(conn) as cur:  # ClientCursor can inline parameters
        q = cur.mogrify(sql, params)
        with cur.copy(f"COPY ({q}) TO STDOUT WITH (FORMAT csv, HEADER)") as cp:
            for chunk in cp:
                buf.write(chunk)
    buf.seek(0)
    table = pacsv.read_csv(buf)
    pq.write_table(table, path, compression="zstd")
    return table.num_rows, path.stat().st_size


def mb(n):
    return f"{n / 1e6:,.1f} MB"


DOWNLOAD_COLS = """
    SELECT dep.deployment_id, dep.lat, dep.lon, d.species_tsn, d.calls[1] AS call,
           d.t_start, d.t_end, d.channel, d.score, d.received_level_db, d.snr_db,
           d.min_freq_hz, d.max_freq_hz, d.duration_s
    FROM nereus.detection d
    JOIN (SELECT id, deployment_id, ST_Y(deploy_location::geometry) AS lat,
                 ST_X(deploy_location::geometry) AS lon FROM nereus.deployment) dep
      ON dep.id = d.deployment_id
"""


def main():
    data = Path(sys.argv[1])
    files = sorted(data.glob("SYN_*_detections.xml"))
    deps = json.loads((data / "deployments.json").read_text())
    xml_bytes = sum(f.stat().st_size for f in files)
    res = {}

    with psycopg.connect(DSN) as conn:
        conn.execute("DELETE FROM nereus.detection_set WHERE doc_id LIKE 'SYN_%'")
        conn.execute("DELETE FROM nereus.deployment WHERE deployment_id LIKE 'SYN_%'")
        conn.execute("DELETE FROM nereus.project WHERE name = 'SYNTHETIC'")
        conn.commit()

        # Deployments first, so detections are linked to them as they load.
        with conn.transaction():
            pid = conn.execute("INSERT INTO nereus.project (name) VALUES ('SYNTHETIC') "
                               "RETURNING id").fetchone()[0]
            for i, (dep, v) in enumerate(deps.items()):
                conn.execute(
                    "INSERT INTO nereus.deployment (deployment_id, project_id, deployment_number, "
                    "platform, deploy_lon, deploy_lat, t_deploy, t_recover) "
                    "VALUES (%s, %s, %s, 'mooring', %s, %s, %s, %s)",
                    (dep, pid, i + 1, v["lon"], v["lat"], v["start"], v["end"]))

        # ---------------------------------------------------------- ingest
        t = time.perf_counter()
        n_det = 0
        for f in files:
            n_det += ingest(conn, str(f))["detections"]
        res["ingest_s"] = time.perf_counter() - t
        conn.autocommit = True
        conn.execute("VACUUM ANALYZE nereus.detection")
        conn.execute("VACUUM ANALYZE nereus.summary_daily")
        conn.execute("VACUUM ANALYZE nereus.deployment")
        conn.autocommit = False
        res["n_det"] = n_det
        res["db_table"] = conn.execute("SELECT pg_table_size('nereus.detection')").fetchone()[0]
        res["db_index"] = conn.execute("SELECT pg_indexes_size('nereus.detection')").fetchone()[0]

        # ---------------------------------------------------------- queries
        q = {}
        q["Q1 one species, one month (all columns)"] = median_time(conn, """
            SELECT * FROM nereus.detection
            WHERE species_tsn = 180473
              AND t_start >= '2020-03-01' AND t_start < '2020-04-01'""")
        q["Q2 one species within 200 km of a point, one year"] = median_time(conn, """
            SELECT d.set_id, d.t_start, d.t_end, d.score
            FROM nereus.detection d
            JOIN nereus.deployment dep ON dep.id = d.deployment_id
            WHERE d.species_tsn = 180404
              AND ST_DWithin(dep.deploy_location, ST_MakePoint(-20, 50)::geography, 200000)
              AND d.t_start >= '2020-01-01' AND d.t_start < '2021-01-01'""")
        q["Q3 daily detection-positive minutes, all species, bounding box, one year (map layer)"] = median_time(conn, """
            SELECT m.deployment_id, m.species_tsn, m.day, m.dp_minutes, m.effort_minutes
            FROM nereus.summary_daily m
            JOIN nereus.deployment dep ON dep.id = m.deployment_id
            WHERE dep.deploy_location && ST_MakeEnvelope(-30, 45, -10, 58, 4326)::geography
              AND m.day >= '2020-01-01' AND m.day < '2021-01-01'""")
        q["Q4 hourly presence per deployment for one species (computed on the fly)"] = median_time(conn, """
            SELECT d.deployment_id, date_trunc('hour', d.t_start) AS hour, count(*)
            FROM nereus.detection d
            WHERE d.species_tsn = 180530
            GROUP BY 1, 2""")
        q["Q5 detections overlapping a 6-hour window, any species (range index)"] = median_time(conn, """
            SELECT * FROM nereus.detection
            WHERE t && tstzrange('2020-06-15 00:00Z', '2020-06-15 06:00Z')""")
        res["queries"] = q

        # --------------------------------------------------------- downloads
        tmp = Path(tempfile.mkdtemp())
        one = files[0].name.replace("_detections.xml", "")
        t, (rows_one, pq_one) = timed(lambda: to_parquet(
            conn, DOWNLOAD_COLS + " WHERE dep.deployment_id = %s", (one,), tmp / "one.parquet"))
        res["dl_one"] = (one, rows_one, pq_one, t, files[0].stat().st_size)

        t, (rows_all, pq_all) = timed(lambda: to_parquet(
            conn, DOWNLOAD_COLS, (), tmp / "all.parquet"))
        res["dl_all"] = (rows_all, pq_all, t)

        buf = io.StringIO()
        t, _ = timed(lambda: export(conn, f"{one}_detections", buf))
        res["xml_export"] = (t, len(buf.getvalue().encode()))

        _, diffs = roundtrip(conn, files[0])
        res["synthetic_roundtrip"] = "lossless" if not diffs else f"{len(diffs)} differences"

        res["pg_version"] = conn.execute("SHOW server_version").fetchone()[0]
        res["postgis"] = conn.execute("SELECT postgis_lib_version()").fetchone()[0]

    write_report(res, len(files), xml_bytes)


def write_report(r, n_files, xml_bytes):
    cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                         capture_output=True, text=True).stdout.strip() or platform.processor()
    one, rows_one, pq_one, t_one, xml_one = r["dl_one"]
    rows_all, pq_all, t_all = r["dl_all"]
    lines = [
        "# Nereus Phase 0 benchmark",
        "",
        f"Machine: {cpu}, {platform.system()} {platform.mac_ver()[0]}. "
        f"PostgreSQL {r['pg_version']}, PostGIS {r['postgis']}, local socket, warm cache.",
        f"Data: {n_files} synthetic deployments, {r['n_det']:,} call-level detections "
        "(bench/synth.py, seed 1).",
        "",
        "## Ingest (ASA XML -> PostgreSQL)",
        "",
        "| | |", "|---|---|",
        f"| Source XML | {mb(xml_bytes)} in {n_files} files |",
        f"| Ingest time (parse + COPY + summaries) | {r['ingest_s']:.1f} s "
        f"({r['n_det'] / r['ingest_s']:,.0f} detections/s) |",
        f"| Detection table on disk | {mb(r['db_table'])} + {mb(r['db_index'])} indexes |",
        "",
        f"## Queries (median of {REPEATS} runs, time includes fetching all rows to Python)",
        "",
        "| Query | Rows | Time |", "|---|---:|---:|",
    ]
    for name, (t, n) in r["queries"].items():
        lines.append(f"| {name} | {n:,} | {t * 1000:,.0f} ms |")
    lines += [
        "",
        "## Downloads",
        "",
        "| | Rows | XML | Parquet (zstd) | Time |", "|---|---:|---:|---:|---:|",
        f"| One deployment ({one}) | {rows_one:,} | {mb(xml_one)} | {mb(pq_one)} | {t_one * 1000:,.0f} ms |",
        f"| Entire database | {rows_all:,} | {mb(xml_bytes)} | {mb(pq_all)} | {t_all:,.1f} s |",
        "",
        f"ASA XML export of one deployment: {r['xml_export'][0] * 1000:,.0f} ms "
        f"({mb(r['xml_export'][1])}). Round trip of a synthetic document: "
        f"**{r['synthetic_roundtrip']}**.",
        "",
    ]
    out = Path(__file__).with_name("RESULTS.md")
    out.write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
