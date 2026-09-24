"""One entry point for every document type Nereus imports and exports.

    ingest_any(conn, "HAT_B_01.xml")          # Deployment, Ensemble or Detections
    export_any(conn, "HAT_B_01", out)         # finds which kind the Id belongs to
"""

import psycopg
from lxml import etree

from .asa import local
from .deployment import export_deployment, ingest_deployment
from .ensemble import export_ensemble, ingest_ensemble
from .export import export as export_detections
from .ingest import ingest as ingest_detections

# root element -> (import, export, table holding the Id, Id column, what is counted)
KINDS = {
    "Detections": (ingest_detections, export_detections, "detection_set", "doc_id", "detections"),
    "Deployment": (ingest_deployment, export_deployment, "deployment", "deployment_id", "channels"),
    "Ensemble": (ingest_ensemble, export_ensemble, "ensemble", "ensemble_id", "units"),
}


def root_name(source) -> str:
    """Local name of the document's root element (path or seekable file
    object), without reading the rest."""
    is_file = hasattr(source, "read")
    pos = source.tell() if is_file else None
    try:
        for _, el in etree.iterparse(source if is_file else str(source),
                                     events=("start",), huge_tree=True):
            return local(el.tag)
    finally:
        if is_file:
            source.seek(pos)
    raise ValueError(f"{source}: empty document")


def ingest_any(conn: psycopg.Connection, source, replace: bool = False, schema=None) -> dict:
    """Import a document of any supported kind (path or file object). The
    result has 'kind', 'doc_id' and 'count' (detections, channels or units)
    plus the importer's own fields."""
    kind = root_name(source)
    if kind not in KINDS:
        raise ValueError(f"{source}: {kind} documents are not supported yet")
    imp, _, _, _, counted = KINDS[kind]
    info = imp(conn, source if hasattr(source, "read") else str(source),
               replace=replace, schema=schema)
    return {**info, "kind": kind, "count": info[counted], "counted": counted}


def find_kind(conn: psycopg.Connection, doc_id: str) -> str:
    for kind, (_, _, table, col, _) in KINDS.items():
        if conn.execute(f"SELECT 1 FROM nereus.{table} WHERE {col} = %s",
                        (doc_id,)).fetchone():
            return kind
    raise KeyError(doc_id)


def export_any(conn: psycopg.Connection, doc_id: str, out, kind: str | None = None) -> int:
    kind = kind or find_kind(conn, doc_id)
    return KINDS[kind][1](conn, doc_id, out)
