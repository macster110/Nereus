"""The benchmark questions, each asked of Nereus and of Tethys.

Every question has:
  * nereus(conn, p)   SQL, rows fetched into a pandas DataFrame
  * xquery(p)         hand-written XQuery in the index-friendly style Tethys
                      itself generates (predicates on collection paths), returning
                      compact <r .../> rows that are parsed into a DataFrame
  * json(p)           optional: the select/return JSON the R and MATLAB
                      clients send, so the real client path is measured too
  * key               how results are counted for the parity check

Both sides do the same end-to-end work: run the query, move the result to the
client, and build a DataFrame. Row counts must match or the report flags it.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from typing import Callable

import pandas as pd
from lxml import etree

# ------------------------------------------------------------------ helpers


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def xs_dt(dt: datetime) -> str:
    return f'xs:dateTime("{iso(dt)}")'


def rows_from_xml(xml: bytes, cols: list[str]) -> pd.DataFrame:
    """Parse <r a="" b=""/> rows (any namespace) into a DataFrame."""
    data = {c: [] for c in cols}
    for _, el in etree.iterparse(BytesIO(xml), events=("end",), huge_tree=True):
        if etree.QName(el).localname == "r":
            for c in cols:
                data[c].append(el.get(c))
            el.clear()
    return pd.DataFrame(data)


def count_elements(xml: bytes, name: str) -> pd.DataFrame:
    """For the JSON (client) path: one DataFrame row per <name> element, with
    its leaf children as columns, similar to what the clients build."""
    rows = []
    for _, el in etree.iterparse(BytesIO(xml), events=("end",), huge_tree=True):
        if etree.QName(el).localname == name:
            rows.append({etree.QName(c).localname: c.text for c in el
                         if isinstance(c.tag, str) and len(c) == 0})
            el.clear()
    return pd.DataFrame(rows)


def sql_df(conn, sql: str, params=None) -> pd.DataFrame:
    sql_df.last = (sql, params)  # for sql_result_bytes
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def sql_result_bytes(conn, sql: str, params=None) -> int:
    """Size of a query's result as a text COPY stream: about the bytes psycopg
    receives (it fetches results as text), for comparison with Tethys's XML.
    Not timed."""
    n = 0
    with conn.cursor() as cur:
        with cur.copy(f"COPY ({sql}) TO STDOUT (FORMAT text)", params) as cp:
            for block in cp:
                n += len(block)
    conn.commit()
    return n


def lon_clause(var: str, lo: float, hi: float) -> str:
    """Tethys allows east longitudes 0-360 or -180..180; test both encodings."""
    alt = f" or ({var} >= {lo + 360} and {var} <= {hi + 360})" if lo < 0 else ""
    return f"(({var} >= {lo} and {var} <= {hi}){alt})"


# What the R client sends by default (speciesIO Latin in, Latin out). Tethys 3.2
# can't take raw TSNs through this route: an empty "species" raises KeyError
# 'query', and explicit nulls generate a predicate that matches nothing.
R_SPECIES = {"query": {"op": "lib:completename2tsn", "operands": ["%s"], "optype": "function"},
             "return": {"op": "lib:tsn2completename", "operands": ["%s"], "optype": "function"}}


@dataclass
class Question:
    key: str
    title: str
    nereus: Callable
    xquery: Callable | None = None
    xquery_cols: list[str] = field(default_factory=list)
    json: Callable | None = None
    json_element: str = "Detection"
    tethys_post: Callable | None = None  # client-side work after the fetch
    # Optional extra parity check: sum of this column on each side must match.
    check_nereus: str | None = None
    check_tethys: str | None = None


# ------------------------------------------------------------ the questions

Q = []

# Q1 --------------------------------------------------------------------------
def species_month(key: str, title: str, sfx: str) -> Question:
    """Every detection of one species in one month. Run for the most-detected
    species (large result) and for a rarely detected one (small result)."""
    tsn, ms, me = f"tsn{sfx}", f"month_start{sfx}", f"month_end{sfx}"
    return Question(
        key=key, title=title,
        nereus=lambda c, p: sql_df(c, """
            SELECT s.doc_id, e.deployment_ref, d.t_start, d.t_end
            FROM nereus.detection d JOIN nereus.detection_set s ON s.id = d.set_id
            JOIN nereus.effort e ON e.set_id = d.set_id
            WHERE d.on_effort AND d.species_tsn = %s AND d.t_start >= %s AND d.t_start < %s""",
            (p[tsn], p[ms], p[me])),
        xquery=lambda p: f"""
<Result>{{
for $d in collection("Detections")/Detections[OnEffort/Detection[SpeciesId = {p[tsn]}
        and Start >= {xs_dt(p[ms])} and Start < {xs_dt(p[me])}]]
for $x in $d/OnEffort/Detection[SpeciesId = {p[tsn]}
        and Start >= {xs_dt(p[ms])} and Start < {xs_dt(p[me])}]
return <r doc="{{$d/Id}}" dep="{{$d/DataSource/DeploymentId}}" s="{{$x/Start}}" e="{{$x/End}}"/>
}}</Result>""",
        xquery_cols=["doc", "dep", "s", "e"],
        json=lambda p: {
            "select": [
                {"op": "=", "operands": ["Detections/OnEffort/Detection/SpeciesId", p[f"latin{sfx}"]], "optype": "binary"},
                {"op": ">=", "operands": ["Detections/OnEffort/Detection/Start", iso(p[ms])], "optype": "binary"},
                {"op": "<", "operands": ["Detections/OnEffort/Detection/Start", iso(p[me])], "optype": "binary"},
            ],
            "return": ["Detections/Id", "Detections/DataSource/DeploymentId",
                       "Detections/OnEffort/Detection/Start", "Detections/OnEffort/Detection/End"],
            "enclose": 1, "namespaces": 0, "species": R_SPECIES},
    )


Q.append(species_month("q1a_common_species_month",
                       "Most-detected species, its busiest month: every detection", ""))
Q.append(species_month("q1b_rare_species_month",
                       "Rarely detected species, its busiest month: every detection", "_rare"))

# Q2 --------------------------------------------------------------------------
Q.append(Question(
    key="q2_deployment",
    title="Everything detected on the busiest deployment (species, start, end)",
    nereus=lambda c, p: sql_df(c, """
        SELECT s.doc_id, d.species_tsn, d.t_start, d.t_end
        FROM nereus.detection d JOIN nereus.detection_set s ON s.id = d.set_id
        JOIN nereus.effort e ON e.set_id = d.set_id
        WHERE d.on_effort AND e.deployment_ref = %s""", (p["deployment"],)),
    xquery=lambda p: f"""
<Result>{{
for $d in collection("Detections")/Detections[DataSource/DeploymentId = "{p['deployment']}"]
for $x in $d/OnEffort/Detection
return <r doc="{{$d/Id}}" sp="{{$x/SpeciesId}}" s="{{$x/Start}}" e="{{$x/End}}"/>
}}</Result>""",
    xquery_cols=["doc", "sp", "s", "e"],
    json=lambda p: {
        "select": [{"op": "=", "operands": ["Detections/DataSource/DeploymentId", p["deployment"]], "optype": "binary"}],
        "return": ["Detections/Id", "Detections/OnEffort/Detection/SpeciesId",
                   "Detections/OnEffort/Detection/Start", "Detections/OnEffort/Detection/End"],
        "enclose": 1, "namespaces": 0, "species": R_SPECIES},
    # No Detection-level condition, so Tethys returns flat SpeciesId/Start/End
    # lists per document rather than <Detection> elements: count the Starts.
    json_element="Start",
))

# Q3 --------------------------------------------------------------------------
Q.append(Question(
    key="q3_spatial_year",
    title="One species, one year, deployments inside a lat/lon box (joins Deployments)",
    nereus=lambda c, p: sql_df(c, """
        SELECT s.doc_id, dep.deployment_id, d.t_start, d.t_end
        FROM nereus.detection d
        JOIN nereus.detection_set s ON s.id = d.set_id
        JOIN nereus.deployment dep ON dep.id = d.deployment_id
        WHERE d.on_effort AND d.species_tsn = %s
          AND d.t_start >= %s AND d.t_start < %s
          -- planar lon/lat box, the same comparison Tethys makes
          AND dep.deploy_location::geometry && ST_MakeEnvelope(%s, %s, %s, %s, 4326)""",
        (p["tsn"], p["year_start"], p["year_end"],
         p["lon_lo"], p["lat_lo"], p["lon_hi"], p["lat_hi"])),
    xquery=lambda p: f"""
<Result>{{
for $dep in collection("Deployments")/Deployment[DeploymentDetails[
        Latitude >= {p['lat_lo']} and Latitude <= {p['lat_hi']}
        and {lon_clause('Longitude', p['lon_lo'], p['lon_hi'])}]]
for $d in collection("Detections")/Detections[DataSource/DeploymentId = $dep/Id]
for $x in $d/OnEffort/Detection[SpeciesId = {p['tsn']}
        and Start >= {xs_dt(p['year_start'])} and Start < {xs_dt(p['year_end'])}]
return <r doc="{{$d/Id}}" dep="{{$dep/Id}}" s="{{$x/Start}}" e="{{$x/End}}"/>
}}</Result>""",
    xquery_cols=["doc", "dep", "s", "e"],
))

# Q4 --------------------------------------------------------------------------
Q.append(Question(
    key="q4_effort",
    title="Effort catalogue: which analyses looked for the species (getDetectionEffort)",
    nereus=lambda c, p: sql_df(c, """
        SELECT s.doc_id, e.deployment_ref, e.t_start AS effort_start, e.t_end AS effort_end,
               k.call, k.granularity
        FROM nereus.effort_kind k JOIN nereus.detection_set s ON s.id = k.set_id
        JOIN nereus.effort e ON e.set_id = k.set_id
        WHERE k.species_tsn = %s""", (p["tsn"],)),
    xquery=lambda p: f"""
<Result>{{
for $d in collection("Detections")/Detections[Effort/Kind[SpeciesId = {p['tsn']}]]
for $k in $d/Effort/Kind[SpeciesId = {p['tsn']}]
return <r doc="{{$d/Id}}" dep="{{$d/DataSource/DeploymentId}}" s="{{$d/Effort/Start}}"
          e="{{$d/Effort/End}}" call="{{$k/Call}}" g="{{$k/Granularity}}"/>
}}</Result>""",
    xquery_cols=["doc", "dep", "s", "e", "call", "g"],
    json=lambda p: {
        "select": [{"op": "=", "operands": ["Detections/Effort/Kind/SpeciesId", p["latin"]], "optype": "binary"}],
        "return": ["Detections/Id", "Detections/DataSource/DeploymentId",
                   "Detections/Effort/Start", "Detections/Effort/End", "Detections/Effort/Kind"],
        "enclose": 1, "namespaces": 0, "species": R_SPECIES},
    json_element="Kind",
))

# Q5 --------------------------------------------------------------------------
def _daily_from_starts(df: pd.DataFrame) -> pd.DataFrame:
    """What a Tethys user does after fetching start times: count per day."""
    if df.empty:
        return pd.DataFrame(columns=["dep", "day", "n"])
    day = pd.to_datetime(df["s"], utc=True, format="ISO8601").dt.floor("D")
    return df.assign(day=day).groupby(["dep", "day"]).size().reset_index(name="n")


Q.append(Question(
    key="q5_daily_presence",
    title="Daily presence of the species per deployment, all years (days with detections)",
    nereus=lambda c, p: sql_df(c, """
        SELECT e.deployment_ref AS dep, date_trunc('day', d.t_start, 'UTC') AS day, count(*) AS n
        FROM nereus.detection d JOIN nereus.effort e ON e.set_id = d.set_id
        WHERE d.on_effort AND d.species_tsn = %s
        GROUP BY 1, 2""", (p["tsn"],)),
    xquery=lambda p: f"""
<Result>{{
for $d in collection("Detections")/Detections[OnEffort/Detection[SpeciesId = {p['tsn']}]]
let $dep := string($d/DataSource/DeploymentId)
for $x in $d/OnEffort/Detection[SpeciesId = {p['tsn']}]
return <r dep="{{$dep}}" s="{{$x/Start}}"/>
}}</Result>""",
    xquery_cols=["dep", "s"],
    tethys_post=_daily_from_starts,
))

# Q6 --------------------------------------------------------------------------
Q.append(Question(
    key="q6_contours",
    title="All whistle contours (Tonal Offset_s/Hz) from the largest contour document",
    nereus=lambda c, p: sql_df(c, """
        SELECT d.ord, d.t_start, d.tonal_offset_s, d.tonal_hz
        FROM nereus.detection d JOIN nereus.detection_set s ON s.id = d.set_id
        WHERE s.doc_id = %s AND d.has_tonal""", (p["contour_doc"],)),
    xquery=lambda p: f"""
<Result>{{
for $x in collection("Detections")/Detections[Id = "{p['contour_doc']}"]/OnEffort/Detection[Parameters/Tonal]
return <r s="{{$x/Start}}" t="{{$x/Parameters/Tonal/Offset_s}}" f="{{$x/Parameters/Tonal/Hz}}"/>
}}</Result>""",
    xquery_cols=["s", "t", "f"],
))

# Q7 --------------------------------------------------------------------------
Q.append(Question(
    key="q7_catalogue",
    title="Map catalogue: every detection document with position, effort span and detection count",
    nereus=lambda c, p: sql_df(c, """
        SELECT s.doc_id, e.deployment_ref, ST_Y(dep.deploy_location::geometry) AS lat,
               ST_X(dep.deploy_location::geometry) AS lon, e.t_start AS effort_start,
               e.t_end AS effort_end,
               (SELECT count(*) FROM nereus.detection d WHERE d.set_id = s.id AND d.on_effort) AS n
        FROM nereus.detection_set s JOIN nereus.effort e ON e.set_id = s.id
        LEFT JOIN nereus.deployment dep ON dep.id = e.deployment_id"""),
    xquery=lambda p: """
<Result>{
for $d in collection("Detections")/Detections
let $dep := collection("Deployments")/Deployment[Id = $d/DataSource/DeploymentId][1]
return <r doc="{$d/Id}" dep="{$d/DataSource/DeploymentId}"
          lat="{$dep/DeploymentDetails/Latitude}" lon="{$dep/DeploymentDetails/Longitude}"
          s="{$d/Effort/Start}" e="{$d/Effort/End}" n="{count($d/OnEffort/Detection)}"/>
}</Result>""",
    xquery_cols=["doc", "dep", "lat", "lon", "s", "e", "n"],
    check_nereus="n", check_tethys="n",
))

QUESTIONS = {q.key: q for q in Q}
