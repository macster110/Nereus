"""ASA/Tethys Ensemble documents <-> SQL: deployments used together.

An Ensemble numbers its deployments (UnitId). Detections whose DataSource
is the ensemble say which unit each detection came from, and importing an
Ensemble links those detections to the unit's deployment.
"""

import psycopg
from psycopg.types.json import Jsonb
from lxml import etree

from .asa import format_num, local
from .deployment import F, I, T, _kids, _read
from .export import IND, _el, _opt, root_open
from .ingest import UnmappedElement


def parse_ensemble(root) -> dict:
    if local(root.tag) != "Ensemble":
        raise ValueError(f"Expected an Ensemble document, got {local(root.tag)}")
    d = {"units": [], "zero": None}
    for c in _kids(root):
        n = local(c.tag)
        if n == "Id":
            d["ensemble_id"] = c.text.strip()
        elif n == "Unit":
            d["units"].append(_read(c, {"UnitId": ("unit_id", I),
                                        "DeploymentId": ("deployment_ref", T)}, "Unit"))
        elif n == "ZeroPosition":
            d["zero"] = _read(c, {"Longitude": ("lon", F), "Latitude": ("lat", F),
                                  "ElevationInstrument_m": ("elev", F)}, "ZeroPosition")
        else:
            raise UnmappedElement(f"Ensemble/{n}")
    d["xml_namespace"] = etree.QName(root).namespace
    d["root_attrs"] = dict(root.attrib) or None
    return d


def ingest_ensemble(conn: psycopg.Connection, source, replace: bool = False,
                    schema: etree.XMLSchema | None = None) -> dict:
    parser = etree.XMLParser(schema=schema, remove_comments=True)
    d = parse_ensemble(etree.parse(source, parser).getroot())
    z = d["zero"] or {}
    with conn.transaction(), conn.cursor() as cur:
        old = cur.execute("SELECT id FROM nereus.ensemble WHERE ensemble_id = %s",
                          (d["ensemble_id"],)).fetchone()
        if old and not replace:
            raise ValueError(f"Ensemble {d['ensemble_id']} already exists (use replace)")
        eid = cur.execute("""
            INSERT INTO nereus.ensemble (ensemble_id, xml_namespace, root_attrs,
                                         zero_lon, zero_lat, zero_elevation_instrument_m)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (ensemble_id) DO UPDATE SET
                xml_namespace = EXCLUDED.xml_namespace, root_attrs = EXCLUDED.root_attrs,
                zero_lon = EXCLUDED.zero_lon, zero_lat = EXCLUDED.zero_lat,
                zero_elevation_instrument_m = EXCLUDED.zero_elevation_instrument_m
            RETURNING id""",
            (d["ensemble_id"], d["xml_namespace"],
             None if d["root_attrs"] is None else Jsonb(d["root_attrs"]),
             z.get("lon"), z.get("lat"), z.get("elev"))).fetchone()[0]
        cur.execute("DELETE FROM nereus.ensemble_unit WHERE ensemble_id = %s", (eid,))
        cur.executemany(
            "INSERT INTO nereus.ensemble_unit (ensemble_id, ord, unit_id, deployment_ref) "
            "VALUES (%s, %s, %s, %s)",
            [(eid, i, u["unit_id"], u["deployment_ref"]) for i, u in enumerate(d["units"])])
        cur.execute("SELECT nereus.link_ensemble(%s)", (eid,))
    return {"id": eid, "doc_id": d["ensemble_id"], "units": len(d["units"])}


def export_ensemble(conn: psycopg.Connection, ensemble_id: str, out) -> int:
    with conn.transaction(), conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        e = cur.execute("SELECT * FROM nereus.ensemble WHERE ensemble_id = %s",
                        (ensemble_id,)).fetchone()
        if e is None:
            raise KeyError(ensemble_id)
        units = cur.execute("SELECT * FROM nereus.ensemble_unit WHERE ensemble_id = %s "
                            "ORDER BY ord", (e["id"],)).fetchall()
    out.write(root_open("Ensemble", e["xml_namespace"], e["root_attrs"]))
    out.write(_el("Id", e["ensemble_id"], 1))
    for u in units:
        out.write(f"{IND}<Unit>\n" + _el("UnitId", u["unit_id"], 2)
                  + _el("DeploymentId", u["deployment_ref"], 2) + f"{IND}</Unit>\n")
    if e["zero_lon"] is not None:
        out.write(f"{IND}<ZeroPosition>\n"
                  + _el("Longitude", format_num(e["zero_lon"]), 2)
                  + _el("Latitude", format_num(e["zero_lat"]), 2)
                  + _opt("ElevationInstrument_m", e["zero_elevation_instrument_m"], 2, format_num)
                  + f"{IND}</ZeroPosition>\n")
    out.write("</Ensemble>\n")
    return len(units)
