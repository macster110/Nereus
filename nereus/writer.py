"""Native SQL writer: store detections without going through XML.

This is the prototype for how PAMGuard (or any other detector) would write
to Nereus. It uses plain SQL over a normal PostgreSQL connection, so the Java
version is a direct translation to JDBC (PgJDBC's CopyManager for COPY, or
batched PreparedStatements for INSERT). sql/examples/write_detections.sql
shows the same statements as raw SQL.

    set_id = create_detection_set(conn, "MyDoc", deployment="DEP01",
                                  effort_start=t0, effort_end=t1,
                                  kinds=[Kind(180473, "Clicks", "call")],
                                  software="PAMGuard", version="2.02.16")
    append_detections(conn, set_id, rows)          # as often as you like
    close_detection_set(conn, set_id, effort_end=t2)

Detections can be appended in batches while a survey runs; each batch is one
transaction. XML is not involved at any point, but the result can still be
exported as ASA/Tethys XML (nereus.export) when someone needs that format.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import psycopg
from psycopg.types.json import Jsonb

from .xmljson import from_python

# Columns a writer may fill; anything not given is NULL.
DETECTION_FIELDS = [
    "t_start", "t_end", "input_file", "count", "event", "unit_id", "channel",
    "species_tsn", "species_group", "calls", "subtype", "score", "confidence",
    "qa", "received_level_db", "freq_measurements_db", "snr_db", "min_freq_hz",
    "max_freq_hz", "peak_freq_hz", "peaks_hz", "duration_s", "sideband_hz",
    "tonal_offset_s", "tonal_hz", "tonal_db", "event_ref", "user_defined",
    "image", "audio", "comment",
]
_PARAM_FIELDS = {"subtype", "score", "confidence", "qa", "received_level_db",
                 "freq_measurements_db", "snr_db", "min_freq_hz", "max_freq_hz",
                 "peak_freq_hz", "peaks_hz", "duration_s", "sideband_hz",
                 "tonal_offset_s", "tonal_hz", "tonal_db", "event_ref", "user_defined"}
_ARRAY_FIELDS = {"freq_measurements_db", "peaks_hz", "sideband_hz",
                 "tonal_offset_s", "tonal_hz", "tonal_db"}
_COLS = ["set_id", "ord", "on_effort", *DETECTION_FIELDS, "has_parameters", "has_tonal"]


@dataclass
class Kind:
    """One Effort/Kind: what was looked for, and at what granularity."""
    species_tsn: int
    call: str | None = None
    granularity: str = "call"          # call | encounter | binned | grouped
    species_group: str | None = None
    bin_size_min: float | None = None
    encounter_gap_min: float | None = None


def _float_array(v) -> str | None:
    """Python list of numbers -> float8[] literal, formatted once in Python
    rather than per element by the driver (much faster for contours)."""
    if v is None:
        return None
    return "{" + ",".join(repr(float(x)) for x in v) + "}"


def _text_array(v) -> str | None:
    """Python list of strings -> text[] literal, e.g. ['Clicks'] -> '{"Clicks"}'."""
    if v is None:
        return None
    if isinstance(v, str):
        v = [v]
    return "{" + ",".join('"' + x.replace("\\", "\\\\").replace('"', '\\"') + '"'
                          for x in v) + "}"


def create_detection_set(conn: psycopg.Connection, doc_id: str, *, deployment: str | None,
                         effort_start: datetime, effort_end: datetime,
                         kinds: list[Kind], user_id: str = "",
                         method: str | None = None, software: str = "",
                         version: str | None = None,
                         algorithm_parameters: dict | None = None,
                         description: dict | None = None,
                         metadata_info: dict | None = None,
                         replace: bool = False) -> int:
    """Create the equivalent of a Detections document header. Returns set_id.

    algorithm_parameters, description and metadata_info are plain dicts,
    stored as searchable jsonb, e.g.
        algorithm_parameters={"Threshold": {"@units": "dB", "#text": 12},
                              "Classifier": "porpoise"}
        description={"Objectives": "...", "Method": "..."}
    (Keys starting "@" are XML attributes; see nereus/xmljson.py.)"""
    as_json = lambda d: None if d is None else Jsonb(from_python(d))
    with conn.transaction():
        if replace:
            conn.execute("DELETE FROM nereus.detection_set WHERE doc_id = %s", (doc_id,))
        dep_id = None
        if deployment:
            dep_id = conn.execute(
                "INSERT INTO nereus.deployment (deployment_id) VALUES (%s) "
                "ON CONFLICT (deployment_id) DO UPDATE SET deployment_id = EXCLUDED.deployment_id "
                "RETURNING id", (deployment,)).fetchone()[0]
        set_id = conn.execute("""
            INSERT INTO nereus.detection_set
                (doc_id, xml_namespace, deployment_ref, deployment_id, user_id,
                 algorithm_method, algorithm_software, algorithm_version,
                 algorithm_parameters, description, metadata_info, effort_start, effort_end)
            VALUES (%s, 'http://tethys.sdsu.edu/schema/1.0', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id""",
            (doc_id, deployment, dep_id, user_id, method, software, version,
             # "" exports as an empty <Parameters/>, which the schema expects
             Jsonb(from_python(algorithm_parameters) if algorithm_parameters else ""),
             as_json(description), as_json(metadata_info),
             effort_start, effort_end)).fetchone()[0]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO nereus.effort_kind (set_id, ord, species_tsn, species_group, call, "
                "granularity, bin_size_min, encounter_gap_min) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                [(set_id, i, k.species_tsn, k.species_group, k.call, k.granularity,
                  k.bin_size_min, k.encounter_gap_min) for i, k in enumerate(kinds)])
    return set_id


def _row(set_id: int, ord_: int, d: dict, on_effort: bool) -> tuple:
    vals = []
    for f in DETECTION_FIELDS:
        v = d.get(f)
        if f in _ARRAY_FIELDS:
            v = _float_array(v)
        elif f in ("calls", "event_ref"):
            v = _text_array(v)
        elif f == "user_defined" and v is not None:
            v = json.dumps(from_python(v))  # any dict; stored as searchable jsonb
        vals.append(v)
    has_params = any(d.get(f) is not None for f in _PARAM_FIELDS)
    has_tonal = d.get("tonal_hz") is not None
    return (set_id, ord_, on_effort, *vals, has_params, has_tonal)


def append_detections(conn: psycopg.Connection, set_id: int, detections: Iterable[dict],
                      on_effort: bool = True, method: str = "copy") -> int:
    """Append detections (dicts keyed by DETECTION_FIELDS) in one transaction.

    method="copy"   COPY FROM STDIN: fastest; PgJDBC's CopyManager does the same.
    method="insert" batched parameterised INSERT: what a plain JDBC
                    PreparedStatement.addBatch()/executeBatch() writer does.
    Returns the number of rows written."""
    with conn.transaction():
        # Lock the set so concurrent writers can't hand out the same ord values.
        if conn.execute("SELECT 1 FROM nereus.detection_set WHERE id = %s FOR UPDATE",
                        (set_id,)).fetchone() is None:
            raise KeyError(f"no detection set {set_id}")
        # Uses the (set_id, ord) primary key: one index probe, however big the set.
        start = conn.execute("SELECT coalesce(max(ord) + 1, 0) FROM nereus.detection "
                             "WHERE set_id = %s", (set_id,)).fetchone()[0]
        rows = [_row(set_id, start + i, d, on_effort) for i, d in enumerate(detections)]
        if method == "copy":
            with conn.cursor() as cur, cur.copy(
                    f"COPY nereus.detection ({', '.join(_COLS)}) FROM STDIN") as cp:
                for r in rows:
                    cp.write_row(r)
        elif method == "insert":
            ph = ", ".join(["%s"] * len(_COLS))
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO nereus.detection ({', '.join(_COLS)}) VALUES ({ph})", rows)
        else:
            raise ValueError(f"unknown method {method!r}")
    return len(rows)


def close_detection_set(conn: psycopg.Connection, set_id: int,
                        effort_end: datetime | None = None) -> None:
    """Optionally extend the effort end, and rebuild the daily summaries."""
    with conn.transaction():
        if effort_end is not None:
            conn.execute("UPDATE nereus.detection_set SET effort_end = %s WHERE id = %s",
                         (effort_end, set_id))
        conn.execute("SELECT nereus.refresh_summary(%s)", (set_id,))
