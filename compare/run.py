"""Nereus (SQL) vs Tethys (XML) speed comparison. Run on the machine hosting both.

    python -m compare.run --tethys http://localhost:9779

Nereus is a SQL system: detectors write detections with SQL and users read
them with SQL. XML is only used to bring existing Tethys data across. So:

  1. BUILD    Make the PostgreSQL equivalent of the Tethys database: download
              every Deployment and Detections document from Tethys and import
              it into Nereus. Both systems then hold the same data.
  2. EXTRACT  Ask the same questions of both: SQL against Nereus, XQuery
              against Tethys (plus the JSON route the R/MATLAB clients use),
              and pull every detection out of each. Row counts must agree.
  3. UPLOAD   Write the same new detections to both: Nereus through the
              native SQL writer (nereus.writer: COPY and batched INSERT),
              Tethys as XML documents through its import API (its only way
              in). Two scenarios: new datasets, and appending batches to a
              growing dataset (as a detector does during a deployment).
              Uploads are verified in both systems, then removed.
  4. CONCURRENCY  Several clients reading at once.

Results go to compare/results/<timestamp>/ (report.md, results.json, and the
exact XQuery sent to Tethys).
"""

import argparse
import json
import os
import platform
import statistics
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from compare import workload  # noqa: E402
from compare.queries import QUESTIONS, rows_from_xml, count_elements, sql_df, sql_result_bytes  # noqa: E402
from compare.tethys import Tethys  # noqa: E402
from nereus.deployment import ingest_deployment  # noqa: E402
from nereus.ingest import ingest, load_schema  # noqa: E402
from nereus.writer import append_detections, close_detection_set, create_detection_set  # noqa: E402

DEFAULT_DSN = os.environ.get("NEREUS_DSN", "postgresql://postgres@localhost:5439/nereus")
BENCH = "NEREUSBENCH_"  # prefix of every document the upload test creates


def jdefault(o):
    """JSON for datetimes (always UTC) and anything else."""
    if isinstance(o, datetime):
        return o.astimezone(timezone.utc).isoformat()
    return str(o)


def log(msg: str = "") -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def safe_name(doc: str) -> str:
    return "".join(c if c.isalnum() or c in "-_.()" else "_" for c in doc)


# ================================================================== 1. BUILD
def download_all(t: Tethys, cache: Path, res: dict, reuse: bool) -> None:
    out = res.setdefault("download", {})
    for coll in ("Deployments", "Detections"):
        d = cache / coll
        d.mkdir(parents=True, exist_ok=True)
        tt = time.perf_counter()
        docs = [x for x in t.list_documents(coll) if not x.startswith(BENCH)]
        list_s = time.perf_counter() - tt
        log(f"Tethys lists {len(docs)} {coll} documents ({list_s:.1f} s)")
        manifest, nbytes, failed, fetched = {}, 0, {}, 0
        tt = time.perf_counter()
        for i, doc in enumerate(docs, 1):
            dest = d / f"{safe_name(doc)}.xml"
            manifest[doc] = dest.name
            if reuse and dest.exists() and dest.stat().st_size > 0:
                continue
            try:
                nbytes += t.get_document(coll, doc, dest)
                fetched += 1
            except Exception as e:  # keep going; record the failure
                failed[doc] = str(e)[:500]
            if i % 25 == 0 or i == len(docs):
                log(f"  {coll}: {i}/{len(docs)} downloaded, {nbytes / 1e6:,.0f} MB")
        secs = time.perf_counter() - tt
        (d / "manifest.json").write_text(json.dumps(manifest, indent=1))
        out[coll] = {"documents": len(docs), "fetched": fetched, "bytes": nbytes,
                     "seconds": secs, "list_seconds": list_s, "failed": failed,
                     "reused_cache": reuse}


def build_nereus(conn, cache: Path, res: dict, schema) -> None:
    """Import the downloaded Tethys documents: the one-off migration."""
    out = res.setdefault("build", {})
    conn.execute("TRUNCATE nereus.detection_set, nereus.deployment, nereus.project, "
                 "nereus.instrument, nereus.sensor RESTART IDENTITY CASCADE")
    conn.commit()

    tt = time.perf_counter()
    dep_failed = {}
    files = sorted((cache / "Deployments").glob("*.xml"))
    for f in files:
        try:
            ingest_deployment(conn, str(f), replace=True)
        except Exception as e:
            dep_failed[f.name] = str(e)[:500]
    out["deployments"] = {"files": len(files), "seconds": time.perf_counter() - tt,
                          "failed": dep_failed}
    log(f"Nereus: {len(files) - len(dep_failed)}/{len(files)} deployments imported")

    files = sorted((cache / "Detections").glob("*.xml"))
    total_bytes = sum(f.stat().st_size for f in files)
    failed, n_det = {}, 0
    tt = time.perf_counter()
    for i, f in enumerate(files, 1):
        try:
            n_det += ingest(conn, str(f), replace=True, schema=schema)["detections"]
        except Exception as e:
            conn.rollback()
            failed[f.name] = f"{type(e).__name__}: {str(e)[:500]}"
        if i % 25 == 0 or i == len(files):
            log(f"  Nereus import: {i}/{len(files)} documents, {n_det:,} detections")
    secs = time.perf_counter() - tt
    conn.autocommit = True
    for tbl in ("detection", "detection_set", "effort", "effort_kind", "deployment", "summary_daily"):
        conn.execute(f"VACUUM ANALYZE nereus.{tbl}")
    conn.autocommit = False
    out["detections"] = {"files": len(files), "bytes": total_bytes, "detections": n_det,
                         "seconds": secs, "failed": failed, "xsd_validated": schema is not None}
    log(f"Nereus: imported {n_det:,} detections in {secs:.1f} s ({len(failed)} documents failed)")


# ================================================================ 2. EXTRACT
def choose_parameters(conn) -> dict:
    q = lambda sql, *a: conn.execute(sql, a).fetchone()
    p = {}
    tsn = q("""SELECT species_tsn, count(*) FROM nereus.detection WHERE on_effort
               GROUP BY 1 ORDER BY 2 DESC LIMIT 1""")
    if not tsn:
        raise RuntimeError("Nereus holds no detections; run without --skip-build first")
    p["tsn"], p["tsn_count"] = tsn

    def busiest_month(species):
        m = q("""SELECT date_trunc('month', t_start, 'UTC'), count(*) FROM nereus.detection
                 WHERE on_effort AND species_tsn = %s GROUP BY 1 ORDER BY 2 DESC LIMIT 1""", species)
        start = m[0]
        return start, (start + timedelta(days=32)).replace(day=1)

    p["month_start"], p["month_end"] = busiest_month(p["tsn"])

    # A species with few detections (at least 20) for the small-result case.
    rare = q("""SELECT species_tsn, count(*) FROM nereus.detection WHERE on_effort
                GROUP BY 1 HAVING count(*) >= 20 ORDER BY 2 ASC LIMIT 1""") or tsn
    p["tsn_rare"], p["tsn_rare_count"] = rare
    p["month_start_rare"], p["month_end_rare"] = busiest_month(p["tsn_rare"])

    dep = q("""SELECT e.deployment_ref, count(*) FROM nereus.detection d
               JOIN nereus.effort e ON e.set_id = d.set_id
               WHERE d.on_effort AND e.deployment_ref IS NOT NULL
               GROUP BY 1 ORDER BY 2 DESC LIMIT 1""")
    p["deployment"] = dep[0] if dep else None

    # Spatial: the positioned deployment with most detections of the species,
    # a 2 x 2 degree box around it, and the calendar year of its detections.
    sp = q("""SELECT ST_Y(dep.deploy_location::geometry), ST_X(dep.deploy_location::geometry),
                     date_trunc('year', min(d.t_start), 'UTC')
              FROM nereus.detection d
              JOIN nereus.deployment dep ON dep.id = d.deployment_id
              WHERE d.on_effort AND d.species_tsn = %s AND dep.deploy_location IS NOT NULL
              GROUP BY dep.id ORDER BY count(*) DESC LIMIT 1""", p["tsn"])
    if sp:
        lat, lon, year = sp
        p.update(lat_lo=round(lat - 1, 4), lat_hi=round(lat + 1, 4),
                 lon_lo=round(max(lon - 1, -180), 4), lon_hi=round(min(lon + 1, 180), 4),
                 year_start=year, year_end=year.replace(year=year.year + 1))

    c = q("""SELECT s.doc_id FROM nereus.detection d JOIN nereus.detection_set s ON s.id = d.set_id
             WHERE d.has_tonal GROUP BY 1 ORDER BY count(*) DESC LIMIT 1""")
    p["contour_doc"] = c[0] if c else None
    conn.commit()
    return p


def needs(q, p) -> str | None:
    """Why a question can't run with these parameters (None if it can)."""
    if q.key == "q3_spatial_year" and "lat_lo" not in p:
        return "no deployment with a position has detections of the species"
    if q.key == "q6_contours" and not p.get("contour_doc"):
        return "no whistle contours in the data"
    if q.key == "q2_deployment" and not p.get("deployment"):
        return "no deployment references"
    return None


def measure(fn, repeats: int, budget: float) -> dict:
    """Run fn at least once, up to `repeats` times, stopping early once
    `budget` seconds have been spent. Returns timings and the last result."""
    times, result, spent = [], None, 0.0
    for _ in range(repeats):
        t = time.perf_counter()
        result = fn()
        dt = time.perf_counter() - t
        times.append(dt)
        spent += dt
        if spent > budget:
            break
    return {"times": times, "median": statistics.median(times), "min": min(times),
            "first": times[0], "result": result}


def run_questions(conn, t: Tethys | None, p: dict, args, outdir: Path, res: dict) -> None:
    xq_dir = outdir / "xquery"
    xq_dir.mkdir(exist_ok=True)
    qres = res.setdefault("questions", {})
    selected = args.questions.split(",") if args.questions else list(QUESTIONS)

    for key in selected:
        q = QUESTIONS[key]
        r = qres.setdefault(key, {"title": q.title})
        why = needs(q, p)
        if why:
            r["skipped"] = why
            log(f"{key}: skipped ({why})")
            continue
        log(f"{key}: {q.title}")

        # ---- Nereus: SQL
        def nereus_run():
            df = q.nereus(conn, p)
            conn.commit()
            return df
        try:
            nereus_run()  # warm-up, not timed
            m = measure(nereus_run, args.repeats, args.budget)
            df = m.pop("result")
            r["nereus"] = {**m, "rows": len(df), "bytes": sql_result_bytes(conn, *sql_df.last)}
            if q.check_nereus:
                r["nereus"]["check"] = float(pd.to_numeric(df[q.check_nereus]).sum())
            log(f"   Nereus SQL      {m['median'] * 1000:10,.1f} ms   {len(df):>10,} rows")
        except Exception as e:
            conn.rollback()
            r["nereus"] = {"error": f"{type(e).__name__}: {e}"}
            log(f"   Nereus SQL      ERROR {e}")

        if t is None:
            continue

        # ---- Tethys: hand-written XQuery
        if q.xquery:
            xq = q.xquery(p)
            (xq_dir / f"{key}.xq").write_text(xq, encoding="utf-8")

            def tethys_run():
                xml = t.xquery(xq)
                df = rows_from_xml(xml, q.xquery_cols)
                if q.tethys_post:
                    df = q.tethys_post(df)
                return len(xml), df
            try:
                m = measure(tethys_run, args.repeats, args.budget)
                nbytes, df = m.pop("result")
                r["tethys_xquery"] = {**m, "rows": len(df), "bytes": nbytes}
                if q.check_tethys:
                    r["tethys_xquery"]["check"] = float(pd.to_numeric(df[q.check_tethys]).sum())
                log(f"   Tethys XQuery   {m['median'] * 1000:10,.1f} ms   {len(df):>10,} rows"
                    f"   (first run {m['first']:.1f} s, {nbytes / 1e6:,.1f} MB)")
            except Exception as e:
                r["tethys_xquery"] = {"error": f"{type(e).__name__}: {str(e)[:2000]}"}
                log(f"   Tethys XQuery   ERROR {str(e)[:300]}")

            # Same query with Tethys's result cache on: a repeat is a cache hit.
            if args.cache_hit and "error" not in r["tethys_xquery"]:
                try:
                    t.cache("on")
                    t.xquery(xq)
                    tt = time.perf_counter()
                    t.xquery(xq)
                    r["tethys_cache_hit_s"] = time.perf_counter() - tt
                except Exception as e:
                    r["tethys_cache_hit_error"] = str(e)[:1000]
                finally:
                    t.cache("off")

        # ---- Tethys: the JSON route used by the R and MATLAB clients
        if q.json and not args.no_json:
            spec = q.json(p)
            try:
                (xq_dir / f"{key}.json-generated.xq").write_bytes(t.json_query(spec, plan=2))
            except Exception as e:
                (xq_dir / f"{key}.json-generated.xq").write_text(f"plan=2 failed: {e}")

            def json_run():
                xml = t.json_query(spec)
                return len(xml), count_elements(xml, q.json_element)
            try:
                m = measure(json_run, args.repeats, args.budget)
                nbytes, df = m.pop("result")
                r["tethys_json"] = {**m, "rows": len(df), "bytes": nbytes}
                log(f"   Tethys client   {m['median'] * 1000:10,.1f} ms   {len(df):>10,} rows")
            except Exception as e:
                r["tethys_json"] = {"error": f"{type(e).__name__}: {str(e)[:2000]}"}
                log(f"   Tethys client   ERROR {str(e)[:300]}")

        # ---- same answer?
        n = r.get("nereus", {}).get("rows")
        tx = r.get("tethys_xquery", {}).get("rows")
        if n is not None and tx is not None:
            same = n == tx
            if q.check_nereus and same:
                same = r["nereus"].get("check") == r["tethys_xquery"].get("check")
            r["parity"] = "match" if same else "MISMATCH"
            if not same:
                log(f"   ** result mismatch: Nereus {n} rows, Tethys {tx} rows **")


def extract_everything(conn, outdir: Path, res: dict) -> None:
    """Every detection out of Nereus with one SQL query: into a DataFrame (what
    an R/MATLAB/Python user gets) and into a Parquet file (a bulk download).
    The Tethys equivalent is downloading every document, timed in BUILD."""
    out = res.setdefault("extract_all", {})
    sql = """SELECT s.doc_id, e.deployment_ref, d.species_tsn, d.calls[1] AS call,
                    d.t_start, d.t_end, d.channel, d.score, d.received_level_db,
                    d.min_freq_hz, d.max_freq_hz, d.duration_s
             FROM nereus.detection d JOIN nereus.detection_set s ON s.id = d.set_id
             JOIN nereus.effort e ON e.set_id = d.set_id"""
    tt = time.perf_counter()
    df = sql_df(conn, sql)
    conn.commit()
    out["dataframe"] = {"rows": len(df), "seconds": time.perf_counter() - tt}
    log(f"Nereus: every detection into a DataFrame, {len(df):,} rows in "
        f"{out['dataframe']['seconds']:.1f} s")
    del df

    ts = pa.timestamp("us", tz="UTC")
    schema = pa.schema([("doc_id", pa.string()), ("deployment_ref", pa.string()),
                        ("species_tsn", pa.int64()), ("call", pa.string()),
                        ("t_start", ts), ("t_end", ts), ("channel", pa.int64()),
                        ("score", pa.float64()), ("received_level_db", pa.float64()),
                        ("min_freq_hz", pa.float64()), ("max_freq_hz", pa.float64()),
                        ("duration_s", pa.float64())])
    path = outdir / "all_detections.parquet"
    tt, n = time.perf_counter(), 0
    with conn.cursor(name="parquet_export") as cur, \
            pq.ParquetWriter(path, schema, compression="zstd") as writer:
        cur.itersize = 200_000
        cur.execute(sql)
        while rows := cur.fetchmany(200_000):
            cols = list(zip(*rows))
            writer.write_table(pa.Table.from_arrays(
                [pa.array(c, type=f.type) for c, f in zip(cols, schema)], schema=schema))
            n += len(rows)
    conn.commit()
    out["parquet"] = {"rows": n, "bytes": path.stat().st_size, "seconds": time.perf_counter() - tt}
    path.unlink()  # can be large; only the timing matters
    log(f"Nereus: every detection to Parquet in {out['parquet']['seconds']:.1f} s "
        f"({out['parquet']['bytes'] / 1e6:,.1f} MB)")


# ================================================================= 3. UPLOAD
def tethys_bench_counts(t: Tethys) -> dict[str, int]:
    """{doc id: detections} for every upload-test document in Tethys."""
    xml = t.xquery(f"""
<Result>{{
for $d in collection("Detections")/Detections[starts-with(Id, "{BENCH}")]
return <r doc="{{$d/Id}}" n="{{count($d/OnEffort/Detection)}}"/>
}}</Result>""")
    df = rows_from_xml(xml, ["doc", "n"])
    return dict(zip(df["doc"], df["n"].astype(int))) if len(df) else {}


def remove_bench_docs(conn, t: Tethys | None) -> None:
    conn.execute("DELETE FROM nereus.detection_set WHERE doc_id LIKE %s", (BENCH + "%",))
    conn.execute("DELETE FROM nereus.deployment WHERE deployment_id LIKE %s", (BENCH + "%",))
    conn.commit()
    if t is not None:
        left = list(tethys_bench_counts(t))
        if left:
            t.delete_documents("Detections", left)


def upload_tests(conn, t: Tethys, outdir: Path, args, res: dict) -> None:
    out = res.setdefault("upload", {})
    updir = outdir / "uploads"
    updir.mkdir(exist_ok=True)
    dep = BENCH + "DEP"
    span = timedelta(days=30)

    # ------------------------------------------------ U1: new datasets
    sets = [(f"{BENCH}new_{i:02d}",
             workload.detections(args.upload_size, workload.T0 + i * span, span, seed=i))
            for i in range(args.upload_sets)]
    total = sum(len(d) for _, d in sets)
    u1 = out.setdefault("new_datasets", {"datasets": len(sets), "detections": total})
    log(f"Upload U1: {len(sets)} new datasets, {total:,} detections in all")

    for method in ("copy", "insert"):
        tt = time.perf_counter()
        for name, dets in sets:
            start = workload.T0 + int(name[-2:]) * span
            sid = create_detection_set(conn, f"{name}_{method}", deployment=dep,
                                       effort_start=start, effort_end=start + span,
                                       kinds=workload.KINDS, software="PAMGuard",
                                       version="2.02.16", replace=True)
            append_detections(conn, sid, dets, method=method)
            close_detection_set(conn, sid)
        u1[f"nereus_{method}_s"] = time.perf_counter() - tt
        log(f"   Nereus SQL ({method:6}) {u1[f'nereus_{method}_s']:8.1f} s")

    gen_s = up_s = 0.0
    nbytes, errors = 0, {}
    for name, dets in sets:
        start = workload.T0 + int(name[-2:]) * span
        tt = time.perf_counter()
        xml = workload.tethys_xml(name, dep, start, start + span, dets)
        f = updir / f"{name}.xml"
        f.write_text(xml, encoding="utf-8")
        gen_s += time.perf_counter() - tt
        nbytes += f.stat().st_size
        tt = time.perf_counter()
        try:
            t.import_xml("Detections", f)
        except Exception as e:
            errors[name] = str(e)[:1000]
        up_s += time.perf_counter() - tt
    u1.update(tethys_xml_s=gen_s, tethys_upload_s=up_s, tethys_bytes=nbytes, tethys_errors=errors)
    log(f"   Tethys XML       {gen_s:8.1f} s to write the XML, {up_s:.1f} s to upload "
        f"({nbytes / 1e6:,.0f} MB, {len(errors)} errors)")

    # ------------------------------------------ U2: appending to a dataset
    base = workload.detections(args.append_base, workload.T0, span, seed=100)
    batches = [workload.detections(args.append_size, workload.T0 + span * (k + 1),
                                   timedelta(hours=6), seed=200 + k)
               for k in range(args.append_batches)]
    u2 = out.setdefault("append", {"base": len(base), "batches": len(batches),
                                   "batch_size": args.append_size})
    log(f"Upload U2: append {len(batches)} batches of {args.append_size:,} to a "
        f"{len(base):,}-detection dataset")
    name = f"{BENCH}append"

    sid = create_detection_set(conn, name, deployment=dep, effort_start=workload.T0,
                               effort_end=workload.T0 + span, kinds=workload.KINDS,
                               software="PAMGuard", replace=True)
    append_detections(conn, sid, base)
    close_detection_set(conn, sid)
    times = []
    for k, b in enumerate(batches):
        tt = time.perf_counter()
        append_detections(conn, sid, b)
        close_detection_set(conn, sid, effort_end=workload.T0 + span * (k + 2))
        times.append(time.perf_counter() - tt)
    u2["nereus_batch_s"] = times
    log(f"   Nereus SQL       median {statistics.median(times) * 1000:,.0f} ms per batch")

    # Tethys can't append: every batch means rewriting and re-uploading the
    # whole document with everything so far.
    so_far = list(base)
    f = updir / f"{name}.xml"
    f.write_text(workload.tethys_xml(name, dep, workload.T0, workload.T0 + span, so_far),
                 encoding="utf-8")
    try:
        t.import_xml("Detections", f)
    except Exception as e:
        u2["tethys_base_error"] = str(e)[:1000]
    gen, up, sizes, errs = [], [], [], {}
    for k, b in enumerate(batches):
        so_far.extend(b)
        tt = time.perf_counter()
        f.write_text(workload.tethys_xml(name, dep, workload.T0, workload.T0 + span * (k + 2),
                                         so_far), encoding="utf-8")
        gen.append(time.perf_counter() - tt)
        sizes.append(f.stat().st_size)
        tt = time.perf_counter()
        try:
            t.import_xml("Detections", f, overwrite=True)
        except Exception as e:
            errs[k] = str(e)[:1000]
        up.append(time.perf_counter() - tt)
    u2.update(tethys_xml_batch_s=gen, tethys_upload_batch_s=up, tethys_doc_bytes=sizes,
              tethys_errors=errs)
    log(f"   Tethys XML       median {statistics.median(gen) * 1000:,.0f} ms to rewrite + "
        f"{statistics.median(up) * 1000:,.0f} ms to re-upload per batch "
        f"(document grows to {sizes[-1] / 1e6:,.0f} MB)")

    # --------------------------------------------------------- verify
    expected = {f"{n}": len(d) for n, d in sets}
    expected[name] = len(base) + sum(len(b) for b in batches)
    nereus_counts = dict(conn.execute(
        "SELECT s.doc_id, count(d.*) FROM nereus.detection_set s "
        "LEFT JOIN nereus.detection d ON d.set_id = s.id WHERE s.doc_id LIKE %s GROUP BY 1",
        (BENCH + "%",)).fetchall())
    conn.commit()
    try:
        tethys_counts = tethys_bench_counts(t)
    except Exception as e:
        tethys_counts, out["verify_error"] = {}, str(e)[:1000]
    ok_n = all(nereus_counts.get(f"{n}_{m}") == c for n, c in expected.items() if n != name
               for m in ("copy", "insert")) and nereus_counts.get(name) == expected[name]
    ok_t = all(tethys_counts.get(n) == c for n, c in expected.items())
    out["verified"] = {"nereus": ok_n, "tethys": ok_t, "expected": expected,
                       "tethys_counts": tethys_counts}
    log(f"Upload check: Nereus {'OK' if ok_n else 'MISMATCH'}, "
        f"Tethys {'OK' if ok_t else 'MISMATCH (see report)'}")


# ============================================================ 4. CONCURRENCY
def concurrency(t: Tethys, dsn: str, p: dict, args, res: dict) -> None:
    q = QUESTIONS["q1b_rare_species_month"]
    xq = q.xquery(p)
    out = res.setdefault("concurrency", {"clients": args.concurrency,
                                         "queries_per_client": args.concurrency_queries,
                                         "question": q.key})

    def nereus_client(_):
        with psycopg.connect(dsn) as c:
            for _ in range(args.concurrency_queries):
                q.nereus(c, p)
                c.commit()

    def tethys_client(_):
        tc = Tethys(t.url, t.timeout)
        for _ in range(args.concurrency_queries):
            rows_from_xml(tc.xquery(xq), q.xquery_cols)

    for name, fn in (("nereus", nereus_client), ("tethys", tethys_client)):
        tt = time.perf_counter()
        try:
            with ThreadPoolExecutor(args.concurrency) as ex:
                list(ex.map(fn, range(args.concurrency)))
            secs = time.perf_counter() - tt
            total = args.concurrency * args.concurrency_queries
            out[name] = {"seconds": secs, "queries_per_s": total / secs}
            log(f"Concurrency {name}: {total} queries by {args.concurrency} clients "
                f"in {secs:.1f} s ({total / secs:.2f}/s)")
        except Exception as e:
            out[name] = {"error": str(e)[:1000]}
            log(f"Concurrency {name}: ERROR {e}")


# ================================================================== helpers
def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def environment(conn, t: Tethys | None, args) -> dict:
    env = {
        "when": datetime.now().isoformat(timespec="seconds"),
        "machine": platform.node(), "platform": platform.platform(),
        "processor": platform.processor(), "cpus": os.cpu_count(),
        "python": platform.python_version(),
        "postgres": conn.execute("SHOW server_version").fetchone()[0],
        "postgis": conn.execute("SELECT postgis_lib_version()").fetchone()[0],
        "shared_buffers": conn.execute("SHOW shared_buffers").fetchone()[0],
        "repeats": args.repeats, "budget_s": args.budget,
    }
    try:
        import psutil  # optional
        env["memory_gb"] = round(psutil.virtual_memory().total / 1e9, 1)
    except ImportError:
        pass
    if t is not None:
        env["tethys_url"] = t.url
        env["tethys_version"] = t.version()
        env["tethys_cache_initially"] = t.cache_enabled()
    conn.commit()
    return env


# =================================================================== report
def fmt_s(x):
    if x is None:
        return "–"
    return f"{x * 1000:,.0f} ms" if x < 10 else f"{x:,.1f} s"


def fmt_bytes(n):
    if n is None:
        return "–"
    return f"{n / 1e3:,.0f} kB" if n < 1e6 else f"{n / 1e6:,.1f} MB"


def ratio(nereus, tethys):
    """'12× faster' / '1.4× slower': Nereus relative to Tethys."""
    if not nereus or not tethys:
        return "–"
    r = tethys / nereus
    word = "faster" if r >= 1 else "slower"
    r = r if r >= 1 else 1 / r
    return f"{r:,.0f}× {word}" if r >= 10 else f"{r:,.1f}× {word}"


def write_report(res: dict, outdir: Path) -> Path:
    env, L = res["environment"], []
    L += ["# Nereus (SQL) vs Tethys (XML): speed comparison", "",
          f"Run {env['when']} on `{env['machine']}` ({env['platform']}, {env['cpus']} CPUs"
          + (f", {env['memory_gb']} GB RAM" if "memory_gb" in env else "") + "). "
          f"Tethys {env.get('tethys_version', '–')} at {env.get('tethys_url', '–')}; "
          f"PostgreSQL {env['postgres']} + PostGIS {env['postgis']}. "
          "Both servers and the client on the same machine.", ""]

    # ---- upload
    up = res.get("upload")
    if up:
        u1, u2 = up.get("new_datasets", {}), up.get("append", {})
        L += ["## Writing detections", "",
              "The same synthetic PAMGuard-style detections (80 % clicks with measurements, "
              "20 % whistles with ~50-point contours) written to both. Nereus uses the "
              "native SQL writer (`nereus/writer.py`); Tethys only accepts XML documents, "
              "so its time includes writing the XML and uploading it.", ""]
        if u1:
            tx = (u1.get("tethys_xml_s") or 0) + (u1.get("tethys_upload_s") or 0)
            L += [f"**New datasets:** {u1['datasets']} datasets, {u1['detections']:,} detections "
                  f"({u1.get('tethys_bytes', 0) / 1e6:,.0f} MB as XML).", "",
                  "| | Time | vs Tethys |", "|---|---:|---:|",
                  f"| Tethys: write XML + upload | {fmt_s(tx)} "
                  f"({fmt_s(u1.get('tethys_xml_s'))} + {fmt_s(u1.get('tethys_upload_s'))}) | |",
                  f"| Nereus: SQL COPY | {fmt_s(u1.get('nereus_copy_s'))} | "
                  f"{ratio(u1.get('nereus_copy_s'), tx)} |",
                  f"| Nereus: SQL batched INSERT | {fmt_s(u1.get('nereus_insert_s'))} | "
                  f"{ratio(u1.get('nereus_insert_s'), tx)} |", ""]
            if u1.get("tethys_errors"):
                L += [f"Tethys upload errors: {len(u1['tethys_errors'])} (see results.json).", ""]
        if u2.get("nereus_batch_s"):
            nb, tg, tu = u2["nereus_batch_s"], u2.get("tethys_xml_batch_s", []), u2.get("tethys_upload_batch_s", [])
            L += [f"**Appending** {u2['batches']} batches of {u2['batch_size']:,} detections to a "
                  f"{u2['base']:,}-detection dataset, as a detector does during a deployment. "
                  "Nereus appends rows. Tethys can't append, so each batch means rewriting and "
                  "re-uploading the whole document.", "",
                  "| Batch | Nereus SQL | Tethys: rewrite + re-upload | Nereus vs Tethys | Tethys document size |",
                  "|---:|---:|---:|---:|---:|"]
            for k in range(len(nb)):
                tt = (tg[k] + tu[k]) if k < len(tg) and k < len(tu) else None
                size = u2.get("tethys_doc_bytes", [None] * len(nb))[k]
                L.append(f"| {k + 1} | {fmt_s(nb[k])} | {fmt_s(tt)} | {ratio(nb[k], tt)} | "
                         f"{'–' if size is None else f'{size / 1e6:,.1f} MB'} |")
            L.append("")
        v = up.get("verified")
        if v:
            L += [f"Uploads checked afterwards by counting detections in each system: "
                  f"Nereus {'✓' if v['nereus'] else '✗'}, Tethys {'✓' if v['tethys'] else '✗'}"
                  + ("" if v["tethys"] else f" (Tethys holds {v['tethys_counts']}, expected {v['expected']})")
                  + ". Test documents were removed from both afterwards.", ""]

    # ---- extraction
    if res.get("questions"):
        p = res.get("parameters", {})
        L += ["## Reading detections", "",
              f"Median of up to {env['repeats']} runs (fewer when a run exceeds the "
              f"{env['budget_s']:.0f} s budget). End to end: query, transfer, and a pandas "
              "DataFrame. Tethys's XQuery result cache was **off**; the cache-hit column is "
              "a repeat with it on. Data size is the result as it comes back: the rows "
              "in PostgreSQL's text format for Nereus (what psycopg receives), the XML "
              "response for Tethys.", "",
              "| Question | Rows | Data size: Nereus / Tethys XML | Nereus SQL | Tethys XQuery | Tethys R/MATLAB route | Tethys cache hit | SQL vs Tethys XQuery | Same answer |",
              "|---|---:|---:|---:|---:|---:|---:|---:|:--:|"]
        for key, r in res["questions"].items():
            if "skipped" in r:
                L.append(f"| **{key}** {r['title']} | – | | skipped: {r['skipped']} | | | | | |")
                continue
            n, tx, tj = r.get("nereus", {}), r.get("tethys_xquery", {}), r.get("tethys_json", {})
            cell = lambda m: ("error" if "error" in m else fmt_s(m.get("median")))
            rows = f"{n['rows']:,}" if "rows" in n else "–"
            size = f"{fmt_bytes(n.get('bytes'))} / {fmt_bytes(tx.get('bytes'))}"
            L.append(f"| **{key}** {r['title']} | {rows} | {size} | {cell(n)} | {cell(tx)} | "
                     f"{cell(tj) if tj else '–'} | {fmt_s(r.get('tethys_cache_hit_s'))} | "
                     f"{ratio(n.get('median'), tx.get('median'))} | "
                     f"{'✓' if r.get('parity') == 'match' else ('✗' if r.get('parity') else '–')} |")
        ea, dl = res.get("extract_all", {}), res.get("download", {}).get("Detections", {})
        if ea:
            L += ["", "**Everything:**", "", "| | Time |", "|---|---:|"]
            if dl and not dl.get("reused_cache"):
                L.append(f"| Tethys: download every Detections document ({dl['bytes'] / 1e6:,.0f} MB XML, "
                         f"not yet parsed into a table) | {fmt_s(dl['seconds'])} |")
            if "dataframe" in ea:
                L.append(f"| Nereus: one SQL query, every detection into a DataFrame "
                         f"({ea['dataframe']['rows']:,} rows) | {fmt_s(ea['dataframe']['seconds'])} |")
            if "parquet" in ea:
                L.append(f"| Nereus: every detection to a Parquet file "
                         f"({ea['parquet']['bytes'] / 1e6:,.1f} MB) | {fmt_s(ea['parquet']['seconds'])} |")
        L += ["", "Parameters chosen from the data:", "", "```",
              json.dumps(p, indent=1, default=jdefault), "```", ""]
        errs = [(k, s, r[s]["error"]) for k, r in res["questions"].items()
                for s in ("nereus", "tethys_xquery", "tethys_json") if "error" in r.get(s, {})]
        if errs:
            L += ["### Errors", ""] + [f"- **{k} / {s}**: `{e[:500]}`" for k, s, e in errs] + [""]
        mism = [k for k, r in res["questions"].items() if r.get("parity") == "MISMATCH"]
        if mism:
            L += ["### Answers that disagree", "",
                  "Check these before trusting the timing (the exact XQuery is in `xquery/`):", ""]
            for k in mism:
                r = res["questions"][k]
                L.append(f"- **{k}**: Nereus {r['nereus'].get('rows')} rows"
                         f" (check {r['nereus'].get('check')}), Tethys {r['tethys_xquery'].get('rows')} rows"
                         f" (check {r['tethys_xquery'].get('check')})")
            L.append("")

    c = res.get("concurrency")
    if c and "nereus" in c:
        L += ["## Several clients at once", "",
              f"{c['clients']} clients, {c['queries_per_client']} queries each ({c['question']}).", "",
              "| | Total time | Queries/s |", "|---|---:|---:|"]
        for s in ("tethys", "nereus"):
            if "error" in c.get(s, {}):
                L.append(f"| {s.title()} | error | |")
            elif s in c:
                L.append(f"| {s.title()} | {fmt_s(c[s]['seconds'])} | {c[s]['queries_per_s']:.2f} |")
        L.append("")

    b = res.get("build", {}).get("detections")
    if b or res.get("disk"):
        L += ["## Building the PostgreSQL copy (one-off migration)", ""]
        if b:
            L.append(f"Imported {b['files']} Detections documents from Tethys ({b['bytes'] / 1e6:,.0f} MB XML, "
                     f"{b['detections']:,} detections) in {fmt_s(b['seconds'])}"
                     + (", validating each against Tethys's `tethys.xsd`." if b.get("xsd_validated") else "."))
            if b.get("failed"):
                L += ["", f"**{len(b['failed'])} documents could not be imported**, so answers "
                      "touching them may differ:", ""]
                L += [f"- `{k}`: {v}" for k, v in list(b["failed"].items())[:20]]
        if res.get("disk"):
            dk = res["disk"]
            L.append(f"\nDisk used: Tethys {dk.get('tethys_db', '–')}, Nereus {dk.get('nereus_db', '–')}.")
        L.append("")

    L += ["## Notes", "",
          "- Tethys is queried with hand-written XQuery in the predicate style its own "
          "translator produces (Tethys at its best; text in `xquery/`), and through the JSON "
          "route the R and MATLAB clients use (what users get; generated XQuery in "
          "`xquery/*.json-generated.xq`). That route can return a different shape, so only "
          "the XQuery row counts are compared.",
          "- Nereus was built from exactly the documents this Tethys server returned.",
          "- PostgreSQL has its normal buffer cache but no result cache.", ""]
    if res.get("fatal"):
        L += ["## The run stopped early", "", "```", res["fatal"][-3000:], "```", ""]

    path = outdir / "report.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


# ===================================================================== main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tethys", default="http://localhost:9779", help="Tethys server URL")
    ap.add_argument("--no-tethys", action="store_true", help="Nereus only (e.g. to test the harness)")
    ap.add_argument("--dsn", default=DEFAULT_DSN, help="Nereus PostgreSQL connection string")
    ap.add_argument("--cache", default=str(ROOT / "compare" / "cache"),
                    help="where documents downloaded from Tethys are kept")
    ap.add_argument("--out", default=str(ROOT / "compare" / "results"))
    g = ap.add_argument_group("steps")
    g.add_argument("--skip-download", action="store_true",
                   help="reuse documents already in --cache instead of downloading")
    g.add_argument("--reuse-cache", action="store_true",
                   help="download only documents not already in --cache")
    g.add_argument("--skip-build", action="store_true", help="Nereus already holds the data")
    g.add_argument("--skip-extract", action="store_true", help="skip the reading tests")
    g.add_argument("--skip-upload", action="store_true", help="skip the writing tests")
    g = ap.add_argument_group("reading")
    g.add_argument("--questions", help="comma-separated subset, e.g. q1a_common_species_month,q4_effort")
    g.add_argument("--repeats", type=int, default=5, help="timed runs per query (default 5)")
    g.add_argument("--budget", type=float, default=300,
                   help="stop repeating a query after this many seconds (default 300)")
    g.add_argument("--no-json", action="store_true", help="skip the Tethys R/MATLAB JSON route")
    g.add_argument("--no-cache-hit", dest="cache_hit", action="store_false",
                   help="skip measuring Tethys cache hits")
    g.add_argument("--concurrency", type=int, default=4, help="parallel clients (0 to skip)")
    g.add_argument("--concurrency-queries", type=int, default=5)
    g = ap.add_argument_group("writing")
    g.add_argument("--upload-sets", type=int, default=10, help="new datasets to write (default 10)")
    g.add_argument("--upload-size", type=int, default=20000, help="detections per dataset (default 20000)")
    g.add_argument("--append-base", type=int, default=20000, help="size of the dataset appended to")
    g.add_argument("--append-batches", type=int, default=10)
    g.add_argument("--append-size", type=int, default=1000, help="detections per appended batch")
    g.add_argument("--keep-uploads", action="store_true", help="don't remove the test uploads")
    ap.add_argument("--timeout", type=float, default=3600, help="Tethys request timeout, s")
    ap.add_argument("--tethys-db", help="Tethys database folder (e.g. ...\\databases\\demodb), for disk use")
    ap.add_argument("--xsd", help="validate documents against this XSD while building Nereus")
    ap.add_argument("--dry-run", action="store_true",
                    help="choose parameters and write the queries, but don't touch Tethys")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # survive non-ASCII on a cp1252 console
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass

    outdir = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache)
    res: dict = {}

    t = None
    if not args.no_tethys:
        t = Tethys(args.tethys, args.timeout)
        if not t.ping():
            sys.exit(f"Tethys is not answering at {args.tethys}. Start it (tethys.bat) or use --no-tethys.")
    live = t if not args.dry_run else None

    with psycopg.connect(args.dsn) as conn:
        res["environment"] = environment(conn, t, args)
        log(f"Tethys {res['environment'].get('tethys_version', '(not used)')}, "
            f"PostgreSQL {res['environment']['postgres']}")
        original_cache = res["environment"].get("tethys_cache_initially")
        try:
            if live:
                live.cache("off")  # time real query work, not cached results
                try:
                    remove_bench_docs(conn, live)  # leftovers from an interrupted run
                except Exception as e:
                    log(f"Could not check for leftover {BENCH}* documents: {e}")
            if live and not args.skip_download:
                download_all(live, cache, res, args.reuse_cache)
            if not args.skip_build:
                build_nereus(conn, cache, res, load_schema(args.xsd) if args.xsd else None)

            if not args.skip_extract:
                p = choose_parameters(conn)
                if live:  # the R/MATLAB route names species in Latin
                    names = live.latin_names([p["tsn"], p["tsn_rare"]])
                    p["latin"], p["latin_rare"] = names.get(p["tsn"]), names.get(p["tsn_rare"])
                res["parameters"] = p
                log("Parameters: " + json.dumps(p, default=jdefault))
                run_questions(conn, live, p, args, outdir, res)
                if args.dry_run and t is not None:
                    for q in QUESTIONS.values():
                        if q.xquery and not needs(q, p):
                            (outdir / "xquery" / f"{q.key}.xq").write_text(q.xquery(p), encoding="utf-8")
                extract_everything(conn, outdir, res)
                if live and args.concurrency > 0:
                    concurrency(live, args.dsn, p, args, res)

            if live and not args.skip_upload:
                try:
                    upload_tests(conn, live, outdir, args, res)
                finally:
                    if not args.keep_uploads:
                        try:
                            remove_bench_docs(conn, live)
                        except Exception as e:
                            res.setdefault("upload", {})["cleanup_error"] = str(e)
                            log(f"Could not remove {BENCH}* documents: {e}")

            disk = {"nereus_db": f"{conn.execute('SELECT pg_database_size(current_database())').fetchone()[0] / 1e6:,.0f} MB"}
            if args.tethys_db:
                disk["tethys_db"] = f"{dir_size(Path(args.tethys_db) / 'db') / 1e6:,.0f} MB"
            res["disk"] = disk
        except KeyboardInterrupt:
            log("Interrupted; writing what we have.")
        except Exception:
            res["fatal"] = traceback.format_exc()
            log(res["fatal"])
        finally:
            if live and original_cache is not None:
                try:
                    live.cache("on" if original_cache else "off")
                except Exception:
                    pass
            (outdir / "results.json").write_text(json.dumps(res, indent=1, default=jdefault),
                                                 encoding="utf-8")
            report = write_report(res, outdir)
            log(f"Report: {report}")


if __name__ == "__main__":
    main()
