"""Minimal Deployment import: identity, position and time span.

Phase 0 only needs enough of a Deployment document to place its detections
on a map (Id, Project, Site, DeploymentDetails position and times). Full
lossless Deployment storage (channels, sampling, sensors, tracks) is Phase 1.
"""

from lxml import etree
import psycopg

from .asa import local, parse_time


def _child(el, *names):
    for n in names:
        if el is None:
            return None
        el = next((c for c in el if isinstance(c.tag, str) and local(c.tag) == n), None)
    return el


def _float(el):
    return None if el is None or not (el.text or "").strip() else float(el.text)


def normalize_lon(lon: float | None) -> float | None:
    """Tethys allows 0-360 east longitudes; PostGIS wants -180..180."""
    if lon is None:
        return None
    return lon - 360 if lon > 180 else lon


def parse_deployment(source) -> dict:
    root = etree.parse(source).getroot()
    if local(root.tag) != "Deployment":
        raise ValueError(f"Expected a Deployment document, got {local(root.tag)}")
    dd, rd = _child(root, "DeploymentDetails"), _child(root, "RecoveryDetails")
    ts = lambda el: None if el is None or not el.text else parse_time(el.text)
    return {
        "deployment_id": _child(root, "Id").text.strip(),
        "project": getattr(_child(root, "Project"), "text", None),
        "site": getattr(_child(root, "Site"), "text", None),
        "lat": _float(_child(dd, "Latitude")),
        "lon": normalize_lon(_float(_child(dd, "Longitude"))),
        "t_deploy": ts(_child(dd, "TimeStamp")),
        "t_recover": ts(_child(rd, "TimeStamp")),
    }


def upsert_deployment(conn: psycopg.Connection, d: dict) -> None:
    conn.execute("""
        INSERT INTO nereus.deployment (deployment_id, project, site, location, t_deploy, t_recover)
        VALUES (%(deployment_id)s, %(project)s, %(site)s,
                CASE WHEN %(lat)s::float8 IS NULL OR %(lon)s::float8 IS NULL THEN NULL
                     ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography END,
                %(t_deploy)s, %(t_recover)s)
        ON CONFLICT (deployment_id) DO UPDATE SET
            project = EXCLUDED.project, site = EXCLUDED.site, location = EXCLUDED.location,
            t_deploy = EXCLUDED.t_deploy, t_recover = EXCLUDED.t_recover""", d)
