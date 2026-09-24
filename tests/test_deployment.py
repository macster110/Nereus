"""Deployment and Ensemble documents: lossless round trip, shared
project/site/instrument rows, and links from detections to deployments
(whichever document arrives first).

Needs the local database (scripts/pg.sh init). Set NEREUS_DEMO to a Tethys
demodb source-docs directory to round-trip every demo Deployment as well.
"""

import io
import os
from pathlib import Path

import psycopg
import pytest

from nereus.cli import DSN, roundtrip
from nereus.documents import ingest_any
from nereus.ingest import UnmappedElement, load_schema

HERE = Path(__file__).parent
DEP = HERE / "fixtures" / "deployment_kitchen_sink.xml"
DETS = HERE / "fixtures" / "kitchen_sink.xml"
XSD = os.environ.get("NEREUS_XSD")
DEMO = os.environ.get("NEREUS_DEMO")

ENSEMBLE = """<?xml version="1.0" encoding="UTF-8"?>
<Ensemble xmlns="http://tethys.sdsu.edu/schema/1.0">
   <Id>TEST_ENS</Id>
   <Unit><UnitId>7</UnitId><DeploymentId>TEST_DEP_01</DeploymentId></Unit>
   <Unit><UnitId>8</UnitId><DeploymentId>TEST_DEP_02</DeploymentId></Unit>
   <ZeroPosition><Longitude>200.55</Longitude><Latitude>21.27</Latitude></ZeroPosition>
</Ensemble>
"""


def variant(src: Path, tmp_path: Path, name: str, *swaps) -> Path:
    """A copy of a fixture with text replaced."""
    text = src.read_text(encoding="utf-8")
    for a, b in swaps:
        assert a in text
        text = text.replace(a, b)
    out = tmp_path / name
    out.write_text(text, encoding="utf-8")
    return out


def second_deployment(tmp_path):
    return variant(DEP, tmp_path, "dep2.xml", ("<Id>TEST_DEP_01</Id>", "<Id>TEST_DEP_02</Id>"),
                   ("<DeploymentNumber>7</DeploymentNumber>", "<DeploymentNumber>8</DeploymentNumber>"))


@pytest.fixture
def conn():
    with psycopg.connect(DSN) as c:
        yield c
        c.rollback()
        c.execute("DELETE FROM nereus.detection_set WHERE doc_id LIKE 'TEST_%'")
        c.execute("DELETE FROM nereus.ensemble WHERE ensemble_id LIKE 'TEST_%'")
        c.execute("DELETE FROM nereus.deployment WHERE deployment_id LIKE 'TEST_%'")
        c.execute("DELETE FROM nereus.site WHERE project_id IN "
                  "(SELECT id FROM nereus.project WHERE name LIKE 'TEST_%')")
        c.execute("DELETE FROM nereus.project WHERE name LIKE 'TEST_%'")
        c.execute("DELETE FROM nereus.instrument WHERE instrument_id LIKE 'TEST-%'")
        c.execute("DELETE FROM nereus.sensor WHERE serial LIKE 'TEST-%'")
        c.commit()


def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()


def test_lossless(conn):
    info, diffs = roundtrip(conn, DEP)
    assert info["kind"] == "Deployment" and info["count"] == 2
    assert diffs == []


@pytest.mark.skipif(not XSD, reason="set NEREUS_XSD to Tethys's lib/schema/tethys.xsd")
def test_valid_against_xsd(conn):
    info, diffs = roundtrip(conn, DEP, load_schema(XSD))
    assert diffs == []


def test_values_land_in_columns(conn):
    ingest_any(conn, DEP, replace=True)
    r = one(conn, """
        SELECT deploy_lon, round(ST_X(deploy_location::geometry)::numeric, 6), site_aliases,
               platform, geometry_type, description->>'Abstract', deploy_contact->'Person'->>'email',
               recover_contact->'ResponsibleParty'->>'organizationName',
               quality_assurance->'ResponsibleParty'->>'@id', track_uris, exact_xml
        FROM nereus.deployment WHERE deployment_id = 'TEST_DEP_01'""")
    assert r == (200.5, -159.5, ["NB", "Site 3"], "glider", "rigid", "Synthetic.",
                 "test@example.org", "Test Lab", "qa1",
                 ["s3://example-bucket/test/track1.csv", "s3://example-bucket/test/track2.csv"], None)
    counts = one(conn, """
        SELECT (SELECT count(*) FROM nereus.channel c WHERE c.deployment_id = d.id),
               (SELECT count(*) FROM nereus.channel_sampling c WHERE c.deployment_id = d.id),
               (SELECT count(*) FROM nereus.channel_gain c WHERE c.deployment_id = d.id),
               (SELECT count(*) FROM nereus.channel_duty_cycle c WHERE c.deployment_id = d.id),
               (SELECT count(*) FROM nereus.recording_quality c WHERE c.deployment_id = d.id),
               (SELECT count(*) FROM nereus.track_point c WHERE c.deployment_id = d.id),
               (SELECT count(*) FROM nereus.deployment_sensor c WHERE c.deployment_id = d.id)
        FROM nereus.deployment d WHERE deployment_id = 'TEST_DEP_01'""")
    assert counts == (2, 3, 2, 1, 2, 3, 4)
    assert one(conn, """SELECT properties->>'Accuracy_C' FROM nereus.deployment_sensor s
        JOIN nereus.deployment d ON d.id = s.deployment_id
        WHERE d.deployment_id = 'TEST_DEP_01' AND element = 'Sensor'""") == ("0.01",)


def test_recording_effort_subtracts_unusable_periods(conn):
    ingest_any(conn, DEP, replace=True)
    rows = conn.execute("""
        SELECT e.channel_number, e.usable::text, e.duty_cycled FROM nereus.recording_effort e
        JOIN nereus.deployment d ON d.id = e.deployment_id
        WHERE d.deployment_id = 'TEST_DEP_01' ORDER BY 1""").fetchall()
    assert [(c, u.count("["), dc) for c, u, dc in rows] == [(1, 2, True), (2, 2, False)]


def test_project_site_instrument_and_sensors_are_shared(conn, tmp_path):
    ingest_any(conn, DEP, replace=True)
    ingest_any(conn, second_deployment(tmp_path), replace=True)
    assert one(conn, """
        SELECT count(DISTINCT project_id), count(DISTINCT site_id), count(DISTINCT instrument_id)
        FROM nereus.deployment WHERE deployment_id IN ('TEST_DEP_01', 'TEST_DEP_02')""") == (1, 1, 1)
    # the same hydrophone serial is one asset used by both deployments
    assert one(conn, """
        SELECT count(DISTINCT s.deployment_id) FROM nereus.deployment_sensor s
        JOIN nereus.sensor h ON h.id = s.hydrophone_id WHERE h.serial = 'TEST-HYD-001'""") == (2,)


def _links(conn):
    return one(conn, """
        SELECT dep.deployment_id,
               count(*) FILTER (WHERE d.deployment_id = dep.id),
               count(*),
               (SELECT count(*) FROM nereus.summary_daily m
                WHERE m.set_id = e.set_id AND m.deployment_id = dep.id)
        FROM nereus.detection_set s JOIN nereus.effort e ON e.set_id = s.id
        JOIN nereus.detection d ON d.set_id = s.id
        LEFT JOIN nereus.deployment dep ON dep.id = e.deployment_id
        WHERE s.doc_id = 'TEST_kitchen_sink' GROUP BY 1, e.set_id, dep.id""")


def test_detections_before_deployment_are_linked_on_arrival(conn):
    ingest_any(conn, DETS, replace=True)
    assert _links(conn)[:3] == (None, 0, 4)
    ingest_any(conn, DEP, replace=True)
    assert _links(conn) == ("TEST_DEP_01", 4, 4, 4)


def test_deployment_before_detections_links_at_import(conn):
    ingest_any(conn, DEP, replace=True)
    ingest_any(conn, DETS, replace=True)
    assert _links(conn) == ("TEST_DEP_01", 4, 4, 4)


def test_reimport_keeps_links(conn):
    ingest_any(conn, DEP, replace=True)
    ingest_any(conn, DETS, replace=True)
    before = one(conn, "SELECT id FROM nereus.deployment WHERE deployment_id = 'TEST_DEP_01'")
    ingest_any(conn, DEP, replace=True)
    assert one(conn, "SELECT id FROM nereus.deployment WHERE deployment_id = 'TEST_DEP_01'") == before
    assert _links(conn) == ("TEST_DEP_01", 4, 4, 4)


def test_ensemble_links_detections_to_unit_deployments(conn, tmp_path):
    """With an Ensemble source, each detection's UnitId picks the deployment."""
    ens_dets = variant(DETS, tmp_path, "ens.xml",
                       ("<DeploymentId>TEST_DEP_01</DeploymentId>", "<EnsembleId>TEST_ENS</EnsembleId>"))
    ens = tmp_path / "ensemble.xml"
    ens.write_text(ENSEMBLE, encoding="utf-8")
    ingest_any(conn, DEP, replace=True)
    ingest_any(conn, second_deployment(tmp_path), replace=True)
    ingest_any(conn, ens_dets, replace=True)       # before the ensemble: no links yet
    q = """SELECT d.ord, dep.deployment_id FROM nereus.detection d
           JOIN nereus.detection_set s ON s.id = d.set_id
           LEFT JOIN nereus.deployment dep ON dep.id = d.deployment_id
           WHERE s.doc_id = 'TEST_kitchen_sink' ORDER BY d.ord"""
    assert [r[1] for r in conn.execute(q)] == [None] * 4
    info, diffs = roundtrip(conn, ens)             # the ensemble arrives
    assert diffs == [] and info["count"] == 2
    # only detection 0 names a unit (7 -> TEST_DEP_01)
    assert [r[1] for r in conn.execute(q)] == ["TEST_DEP_01", None, None, None]
    ingest_any(conn, ens_dets, replace=True)       # and after it: linked at import
    assert [r[1] for r in conn.execute(q)] == ["TEST_DEP_01", None, None, None]


def test_detections_know_their_effort_kind(conn):
    """kitchen_sink kinds: 0 NBHF clicks (1-minute bins), 1 Moan (encounter),
    2 species 180404 with no call (call); the last detection is a species
    with no kind at all."""
    ingest_any(conn, DETS, replace=True)
    rows = conn.execute("""
        SELECT d.ord, d.kind_ord, g.granularity, g.bin_size_s
        FROM nereus.detection d JOIN nereus.detection_set s ON s.id = d.set_id
        JOIN nereus.detection_granularity g ON g.set_id = d.set_id AND g.ord = d.ord
        WHERE s.doc_id = 'TEST_kitchen_sink' ORDER BY d.ord""").fetchall()
    assert rows == [(0, 0, "binned", 60.0), (1, 1, "encounter", None),
                    (2, 2, "call", None), (3, None, None, None)]


def test_unknown_element_rejected(conn):
    xml = DEP.read_text(encoding="utf-8").replace("<Name>Port hydrophone</Name>",
                                                  "<Mystery>x</Mystery>")
    with pytest.raises(UnmappedElement, match="Sensors/Audio/Mystery"):
        ingest_any(conn, io.BytesIO(xml.encode()), replace=True)
    assert one(conn, "SELECT count(*) FROM nereus.deployment "
                     "WHERE deployment_id = 'TEST_DEP_01'") == (0,)


@pytest.mark.skipif(not DEMO, reason="set NEREUS_DEMO to a Tethys demodb source-docs directory")
def test_demo_deployments(conn):
    """Every Deployment and Ensemble in the Tethys demo database round-trips.
    (The demo Ensembles use UnitId 0, which the XSD's positiveInteger
    forbids, so they are not validated.)"""
    schema = load_schema(XSD) if XSD else None
    bad = {}
    for sub, xsd in (("Deployments", schema), ("Ensembles", None)):
        for f in sorted(Path(DEMO, sub).glob("*.xml")):
            _, diffs = roundtrip(conn, f, xsd)
            if diffs:
                bad[f.name] = diffs
    assert bad == {}
