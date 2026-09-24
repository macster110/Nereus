"""Round-trip tests: ASA XML -> PostgreSQL -> ASA XML must be lossless.

Needs the local database (scripts/pg.sh init). Each test runs against the
real schema; documents are removed again afterwards.
"""

import io
import os
from pathlib import Path

import psycopg
import pytest

from nereus.canonical import equivalent
from nereus.cli import DSN, roundtrip
from nereus.export import export
from nereus.ingest import UnmappedElement, ingest, load_schema

HERE = Path(__file__).parent
EXAMPLES = sorted((HERE.parent / "data" / "examples").glob("*.xml"))
KITCHEN = HERE / "fixtures" / "kitchen_sink.xml"


@pytest.fixture
def conn():
    with psycopg.connect(DSN) as c:
        yield c
        c.execute("DELETE FROM nereus.detection_set WHERE doc_id LIKE 'TEST_%' "
                  "OR doc_id IN ('CSM01A_automatic_UBW_jst', 'SOCAL_U_01_automatic_UBW_jst')")
        c.commit()


@pytest.mark.parametrize("path", EXAMPLES + [KITCHEN], ids=lambda p: p.name)
def test_lossless(conn, path):
    info, diffs = roundtrip(conn, path)
    assert info["detections"] > 0
    assert diffs == []


def test_export_does_not_swallow_later_writes(conn):
    """Regression: export's streaming cursors used to leave a transaction open,
    so imports after an export were silently lost when the connection closed."""
    roundtrip(conn, EXAMPLES[0])
    roundtrip(conn, EXAMPLES[1])
    with psycopg.connect(DSN) as other:  # a separate session sees only committed data
        n = other.execute("SELECT count(*) FROM nereus.detection_set WHERE doc_id = ANY(%s)",
                          ([p.stem for p in EXAMPLES],)).fetchone()[0]
    assert n == 2


def test_example_counts(conn):
    """Detection and effort counts in SQL match the XML."""
    expected = {"CSM01A_automatic_UBW_jst": (569, 1), "SOCAL_U_01_automatic_UBW_jst": (334, 2)}
    for p in EXAMPLES:
        ingest(conn, str(p), replace=True)
    rows = conn.execute("""
        SELECT s.doc_id,
               (SELECT count(*) FROM nereus.detection d WHERE d.set_id = s.id),
               (SELECT count(*) FROM nereus.effort_kind k WHERE k.set_id = s.id)
        FROM nereus.detection_set s WHERE s.doc_id = ANY(%s)""", (list(expected),)).fetchall()
    assert {r[0]: (r[1], r[2]) for r in rows} == expected


def test_kitchen_sink_columns(conn):
    """Spot-check that values land in typed, queryable columns."""
    ingest(conn, str(KITCHEN), replace=True)
    r = conn.execute("""
        SELECT d.calls, d.score, d.peaks_hz, d.tonal_hz, d.event_ref, d.t_start,
               (d.user_defined->>'ClickTrainId')::int = 1234
        FROM nereus.detection d JOIN nereus.detection_set s ON s.id = d.set_id
        WHERE s.doc_id = 'TEST_kitchen_sink' AND d.ord = 0""").fetchone()
    calls, score, peaks, tonal_hz, refs, t_start, has_ud = r
    assert calls == ["Clicks", "Buzz"]
    assert score == pytest.approx(0.93)
    assert peaks == [129000, 133000]
    assert tonal_hz == [130000, 131000, 132000]
    assert refs == ["E0", "E2"]
    assert t_start.microsecond == 345678
    assert has_ud

    # Timezone offsets are normalised to UTC instants.
    t = conn.execute("""SELECT d.t_start AT TIME ZONE 'UTC' FROM nereus.detection d
        JOIN nereus.detection_set s ON s.id = d.set_id
        WHERE s.doc_id = 'TEST_kitchen_sink' AND d.ord = 1""").fetchone()[0]
    assert (t.hour, t.minute) == (12, 0)


def test_json_blocks_are_searchable(conn):
    """Free-form blocks are jsonb that SQL can search, not opaque XML."""
    ingest(conn, str(KITCHEN), replace=True)
    row = conn.execute("""
        SELECT s.algorithm_parameters @> '{"Classifier": {"@name": "porpoise"}}',
               (s.algorithm_parameters->'Threshold'->>'#text')::float,
               s.algorithm_parameters->'settings'->'MODULE'->>'#ns',
               s.description->>'Abstract',
               s.metadata_info->'Contact'->>'individualName',
               jsonb_array_length(s.algorithm_support),
               s.bespoke_data->'Data'->>'URI',
               s.exact_xml,
               (SELECT count(*) FROM nereus.detection d
                WHERE d.set_id = s.id AND d.user_defined @> '{"ICI_ms": {"@mode": "median"}}'),
               (SELECT count(*) FROM nereus.detection d
                WHERE d.set_id = s.id AND d.user_defined_xml IS NOT NULL)
        FROM nereus.detection_set s WHERE s.doc_id = 'TEST_kitchen_sink'""").fetchone()
    assert row == (True, 12.5, "", "Synthetic test document.", "Test Person", 2,
                   "s3://example-bucket/test/pamguard_binary.zip", None, 1, 0)


def test_block_json_cannot_hold_keeps_original_xml(conn, tmp_path):
    """Text between child elements has no JSON form: the original XML is kept
    as well, and the export is still lossless."""
    xml = KITCHEN.read_text(encoding="utf-8").replace(
        "<ClickTrainId>1234</ClickTrainId>", "<ClickTrainId>1234</ClickTrainId>stray text")
    src = tmp_path / "mixed.xml"
    src.write_text(xml, encoding="utf-8")
    info, diffs = roundtrip(conn, src)
    assert diffs == []
    ud_json, ud_xml = conn.execute(
        "SELECT user_defined, user_defined_xml FROM nereus.detection "
        "WHERE set_id = %s AND ord = 0", (info["set_id"],)).fetchone()
    assert ud_json is None and "stray text" in ud_xml


def test_summary_counts_minutes(conn):
    """Detection-positive minutes: the 00:00:12-00:01:30 detection touches 2 minutes,
    and the detection crossing midnight is split across two days."""
    ingest(conn, str(KITCHEN), replace=True)
    rows = conn.execute("""
        SELECT species_tsn, call, day::text, dp_minutes, n_detections
        FROM nereus.summary_daily m JOIN nereus.detection_set s ON s.id = m.set_id
        WHERE s.doc_id = 'TEST_kitchen_sink' ORDER BY 1, 2, 3""").fetchall()
    assert rows == [
        (180404, "", "2021-06-02", 1, 1),
        (180404, "", "2021-06-03", 1, 1),
        (180404, "Moan", "2021-06-01", 1, 1),
        (180473, "Clicks", "2021-06-01", 2, 1),
    ]


def test_unknown_element_rejected_and_rolled_back(conn):
    """An element the importer can't store must fail the whole import."""
    xml = KITCHEN.read_text(encoding="utf-8").replace("<Image>", "<Mystery>x</Mystery><Image>")
    with pytest.raises(UnmappedElement, match="Detection/Mystery"):
        ingest(conn, io.BytesIO(xml.encode()), replace=True)
    n = conn.execute("SELECT count(*) FROM nereus.detection_set "
                     "WHERE doc_id = 'TEST_kitchen_sink'").fetchone()[0]
    assert n == 0


def test_diff_detects_changes(conn, tmp_path):
    """The comparison itself must catch real changes (guards against a
    comparator that always says 'equal')."""
    ingest(conn, str(KITCHEN), replace=True)
    buf = io.StringIO()
    export(conn, "TEST_kitchen_sink", buf)
    changed = buf.getvalue().replace("<Score>0.93</Score>", "<Score>0.94</Score>")
    out = tmp_path / "changed.xml"
    out.write_text(changed, encoding="utf-8")
    diffs = equivalent(KITCHEN, out)
    assert any("Score" in d for d in diffs)


XSD = os.environ.get("NEREUS_XSD")


@pytest.mark.skipif(not XSD, reason="set NEREUS_XSD to Tethys's lib/schema/tethys.xsd")
@pytest.mark.parametrize("path", EXAMPLES + [KITCHEN], ids=lambda p: p.name)
def test_valid_against_xsd(conn, path):
    """Input validates on the way in, and the exported document validates too."""
    info, diffs = roundtrip(conn, path, load_schema(XSD))
    assert diffs == []
