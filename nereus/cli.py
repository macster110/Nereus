"""nereus command line: ingest, export, round-trip check.

  python -m nereus.cli ingest FILE.xml [...] [--replace]
  python -m nereus.cli export DOC_ID OUT.xml
  python -m nereus.cli roundtrip FILE.xml [...]
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
from .export import export
from .ingest import ingest, load_schema

DSN = os.environ.get("NEREUS_DSN", "postgresql://postgres@localhost:5439/nereus")
# Optional XSD (e.g. <tethys>/databases/demodb/lib/schema/tethys.xsd) to validate imports.
XSD = os.environ.get("NEREUS_XSD")


def connect() -> psycopg.Connection:
    return psycopg.connect(DSN)


def roundtrip(conn, path: Path, schema=None) -> tuple[dict, list[str]]:
    """Ingest `path`, export it again, and diff the two documents.
    With `schema`, the exported document is validated too."""
    info = ingest(conn, str(path), replace=True, schema=schema)
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False,
                                     encoding="utf-8") as f:
        export(conn, info["doc_id"], f)
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
    for p in sub.choices.values():
        p.add_argument("--xsd", default=XSD, help="validate against this XSD (or set NEREUS_XSD)")
    a = ap.parse_args(argv)
    schema = load_schema(a.xsd) if getattr(a, "xsd", None) else None

    with connect() as conn:
        if a.cmd == "ingest":
            for f in a.files:
                t = time.perf_counter()
                info = ingest(conn, f, replace=a.replace, schema=schema)
                print(f"{info['doc_id']}: {info['detections']} detections "
                      f"in {time.perf_counter() - t:.2f}s")
        elif a.cmd == "export":
            with open(a.out, "w", encoding="utf-8") as out:
                n = export(conn, a.doc_id, out)
            print(f"wrote {n} detections to {a.out}")
        elif a.cmd == "roundtrip":
            bad = 0
            for f in a.files:
                info, diffs = roundtrip(conn, Path(f), schema)
                status = "LOSSLESS" if not diffs else f"{len(diffs)} DIFFERENCES"
                print(f"{Path(f).name}: {info['detections']} detections -> {status}")
                for d in diffs:
                    print("   ", d)
                bad += bool(diffs)
            sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
