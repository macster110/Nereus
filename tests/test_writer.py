"""Native SQL writer: detections go in with SQL and come out as SQL rows,
or as valid ASA XML when that format is wanted."""

import io
import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from lxml import etree

from nereus.cli import DSN
from nereus.export import export
from nereus.ingest import ingest, load_schema
from nereus.writer import Kind, append_detections, close_detection_set, create_detection_set

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
XSD = os.environ.get("NEREUS_XSD")


def dets(n, start=0):
    for i in range(start, start + n):
        t = T0 + timedelta(seconds=i * 7.5)
        d = {"t_start": t, "t_end": t + timedelta(milliseconds=1), "channel": i % 4,
             "species_tsn": 180473, "calls": "Clicks", "score": 0.5 + (i % 50) / 100,
             "received_level_db": 120.5, "min_freq_hz": 115000.0, "max_freq_hz": 145000.0}
        if i % 10 == 0:  # some whistle-style contours too
            d.update(species_tsn=180404, calls="Whistle", tonal_offset_s=[0, 0.01, 0.02],
                     tonal_hz=[9000, 9500.5, 10000])
        yield d


@pytest.fixture
def conn():
    with psycopg.connect(DSN) as c:
        yield c
        c.execute("DELETE FROM nereus.detection_set WHERE doc_id LIKE 'TESTW_%'")
        c.execute("DELETE FROM nereus.deployment WHERE deployment_id LIKE 'TESTW_%'")
        c.commit()


def new_set(conn, name="TESTW_set"):
    return create_detection_set(
        conn, name, deployment="TESTW_DEP", effort_start=T0, effort_end=T0 + timedelta(days=1),
        kinds=[Kind(180473, "Clicks"), Kind(180404, "Whistle")],
        software="PAMGuard", version="2.02.16", replace=True)


@pytest.mark.parametrize("method", ["copy", "insert"])
def test_append_batches(conn, method):
    sid = new_set(conn)
    assert append_detections(conn, sid, dets(500), method=method) == 500
    assert append_detections(conn, sid, dets(250, 500), method=method) == 250
    close_detection_set(conn, sid)
    n, lo, hi, tonal = conn.execute(
        "SELECT count(*), min(ord), max(ord), count(*) FILTER (WHERE has_tonal) "
        "FROM nereus.detection WHERE set_id = %s", (sid,)).fetchone()
    assert (n, lo, hi, tonal) == (750, 0, 749, 75)
    hz = conn.execute("SELECT tonal_hz FROM nereus.detection WHERE set_id = %s AND ord = 0",
                      (sid,)).fetchone()[0]
    assert hz == [9000, 9500.5, 10000]
    assert conn.execute("SELECT sum(n_detections) FROM nereus.summary_daily WHERE set_id = %s",
                        (sid,)).fetchone()[0] == 750


def test_sql_written_set_exports_as_asa_xml(conn, tmp_path):
    """A set written with SQL exports as XML, which imports back unchanged
    (and validates against the Tethys XSD when NEREUS_XSD is set)."""
    sid = new_set(conn)
    append_detections(conn, sid, dets(100))
    close_detection_set(conn, sid)
    out = tmp_path / "w.xml"
    with open(out, "w", encoding="utf-8") as f:
        assert export(conn, "TESTW_set", f) == 100
    if XSD:
        schema = load_schema(XSD)
        assert schema.validate(etree.parse(str(out))), schema.error_log
    before = conn.execute("SELECT t_start, species_tsn, score, tonal_hz FROM nereus.detection "
                          "WHERE set_id = %s ORDER BY ord", (sid,)).fetchall()
    info = ingest(conn, str(out), replace=True)
    after = conn.execute("SELECT t_start, species_tsn, score, tonal_hz FROM nereus.detection "
                         "WHERE set_id = %s ORDER BY ord", (info["set_id"],)).fetchall()
    assert before == after


def test_writer_dicts_become_json_and_valid_xml(conn, tmp_path):
    """Parameters, metadata and UserDefined go in as plain dicts, are searchable
    jsonb, and export as schema-valid XML even if keys come in the wrong order."""
    sid = create_detection_set(
        conn, "TESTW_json", deployment="TESTW_DEP", effort_start=T0,
        effort_end=T0 + timedelta(days=1), kinds=[Kind(180473, "Clicks")],
        software="PAMGuard", replace=True,
        algorithm_parameters={"Threshold": {"@units": "dB", "#text": 12},
                              "Classifier": {"Name": "porpoise", "MinICI_ms": 2}},
        description={"Method": "Click detector", "Objectives": "Porpoise presence"},  # wrong order
        metadata_info={"UpdateFrequency": "as-needed", "Date": "2026-06-01T00:00:00Z",
                       "Contact": {"organizationName": "SMRU", "individualName": "J. M."}})
    append_detections(conn, sid, [{**d, "user_defined": {"TrainId": i, "ICI_ms": 35.2}}
                                  for i, d in enumerate(dets(5))])
    close_detection_set(conn, sid)
    assert conn.execute(
        "SELECT count(*) FROM nereus.detection WHERE set_id = %s AND (user_defined->>'TrainId')::int >= 3",
        (sid,)).fetchone()[0] == 2
    assert conn.execute(
        "SELECT algorithm_parameters->'Classifier'->>'Name' FROM nereus.detection_set WHERE id = %s",
        (sid,)).fetchone()[0] == "porpoise"
    out = tmp_path / "json.xml"
    with open(out, "w", encoding="utf-8") as f:
        export(conn, "TESTW_json", f)
    text = out.read_text()
    assert text.index("<Objectives>") < text.index("<Method>")
    assert "<TrainId>3</TrainId>" in text
    if XSD:
        schema = load_schema(XSD)
        assert schema.validate(etree.parse(str(out))), schema.error_log


def test_append_to_missing_set_fails(conn):
    with pytest.raises(KeyError):
        append_detections(conn, 999_999_999, dets(1))


def test_export_buffer_counts(conn):
    sid = new_set(conn, "TESTW_small")
    append_detections(conn, sid, dets(3))
    buf = io.StringIO()
    assert export(conn, "TESTW_small", buf) == 3
    assert buf.getvalue().count("<Detection>") == 3
