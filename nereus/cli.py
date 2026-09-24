"""nereus command line: ingest, export, round-trip check.

Works with Deployment, Ensemble and Detections documents.

  python -m nereus.cli ingest FILE.xml [...] [--replace]
  python -m nereus.cli export DOC_ID OUT.xml
  python -m nereus.cli roundtrip FILE.xml [...] [--quiet]

A directory in place of FILE.xml means every .xml file in it.
"""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

import psycopg
from lxml import etree

from .canonical import equivalent
from .documents import export_any, ingest_any
from .ingest import load_schema

DSN = os.environ.get("NEREUS_DSN", "postgresql://postgres@localhost:5439/nereus")
# Optional XSD (e.g. <tethys>/databases/demodb/lib/schema/tethys.xsd) to validate imports.
XSD = os.environ.get("NEREUS_XSD")


def connect() -> psycopg.Connection:
    return psycopg.connect(DSN)


def roundtrip(conn, path: Path, schema=None) -> tuple[dict, list[str]]:
    """Ingest `path`, export it again, and diff the two documents.
    With `schema`, the exported document is validated too."""
    info = ingest_any(conn, path, replace=True, schema=schema)
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False,
                                     encoding="utf-8") as f:
        export_any(conn, info["doc_id"], f, kind=info["kind"])
    try:
        diffs = equivalent(path, f.name)
        if schema is not None:
            try:
                for _ in etree.iterparse(f.name, schema=schema, huge_tree=True):
                    pass
            except etree.XMLSyntaxError as e:
                diffs.append(f"exported document fails XSD validation: {e}")
        return info, diffs
    finally:
        os.unlink(f.name)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nereus")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest"); p.add_argument("files", nargs="+"); p.add_argument("--replace", action="store_true")
    p = sub.add_parser("export"); p.add_argument("doc_id"); p.add_argument("out")
    p = sub.add_parser("roundtrip"); p.add_argument("files", nargs="+")
    p.add_argument("--quiet", action="store_true", help="only print documents that differ")
    for p in sub.choices.values():
        p.add_argument("--xsd", default=XSD, help="validate against this XSD (or set NEREUS_XSD)")
    a = ap.parse_args(argv)
    schema = load_schema(a.xsd) if getattr(a, "xsd", None) else None
    if hasattr(a, "files"):  # a directory means every .xml file in it
        a.files = [str(x) for f in a.files
                   for x in (sorted(Path(f).glob("*.xml")) if Path(f).is_dir() else [Path(f)])]

    status = 0
    with connect() as conn:
        if a.cmd == "ingest":
            for f in a.files:
                t = time.perf_counter()
                info = ingest_any(conn, f, replace=a.replace, schema=schema)
                print(f"{info['kind']} {info['doc_id']}: {info['count']} {info['counted']} "
                      f"in {time.perf_counter() - t:.2f}s")
        elif a.cmd == "export":
            with open(a.out, "w", encoding="utf-8") as out:
                n = export_any(conn, a.doc_id, out)
            print(f"wrote {a.doc_id} ({n} items) to {a.out}")
        elif a.cmd == "roundtrip":
            bad = 0
            for f in a.files:
                try:
                    info, diffs = roundtrip(conn, Path(f), schema)
                    head = f"{info['kind']} {Path(f).name}: {info['count']} {info['counted']}"
                except Exception as e:  # keep going; report the file as failed
                    conn.rollback()
                    diffs = [f"import failed: {type(e).__name__}: {e}"]
                    head = Path(f).name
                verdict = "LOSSLESS" if not diffs else f"{len(diffs)} DIFFERENCES"
                if diffs or not a.quiet:
                    print(f"{head} -> {verdict}")
                for d in diffs:
                    print("   ", d)
                bad += bool(diffs)
            print(f"{len(a.files) - bad}/{len(a.files)} documents lossless")
            status = 1 if bad else 0
    sys.exit(status)  # outside the with-block, so the connection commits first


if __name__ == "__main__":
    main()
