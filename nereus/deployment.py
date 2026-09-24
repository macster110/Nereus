"""ASA/Tethys Deployment documents <-> SQL tables, losslessly.

A Deployment document is spread over:

    project, site, instrument     shared rows (Tethys holds them as strings)
    deployment                    the document's single-valued fields
    channel (+ channel_sampling, channel_gain, channel_duty_cycle)
    recording_quality             QualityAssurance/Quality periods
    track, track_point            Data/Tracks
    deployment_sensor (+ sensor)  Sensors/{Audio,Depth,Sensor}, and the
                                  hydrophone/preamp/sensor assets they name

Descriptive blocks (Description, MetadataInfo, contacts, EventTrigger,
TrackEffort, sensor Properties) are searchable jsonb, as for Detections.

As with Detections, an element the importer does not know raises
UnmappedElement, so nothing is dropped silently. Re-importing a deployment
(replace=True) keeps its id, so detections already linked to it stay linked.
"""

import copy

import psycopg
from lxml import etree
from psycopg.types.json import Jsonb

from .asa import format_num, format_time, local, parse_time, text
from .export import IND, _block, _el, _opt, root_open
from .ingest import UnmappedElement
from .xmljson import block_to_xml, convert

# ------------------------------------------------------------------ reading
T = text                                   # string, '' for an empty element
F = lambda c: float(c.text)
I = lambda c: int(c.text)
DT = lambda c: parse_time(c.text)


def _kids(el):
    return [c for c in el if isinstance(c.tag, str)]


def _read(el, spec: dict, where: str) -> dict:
    """Children of `el` by name -> {key: conv(child)}. Keys ending in '[]'
    collect repeated children into a list. Unknown children are an error."""
    out = {}
    for c in _kids(el):
        n = local(c.tag)
        if n not in spec:
            raise UnmappedElement(f"{where}/{n}")
        key, conv = spec[n]
        if key.endswith("[]"):
            out.setdefault(key[:-2], []).append(conv(c))
        else:
            out[key] = conv(c)
    return out


def _only(el, name: str, where: str) -> list:
    kids = _kids(el)
    for c in kids:
        if local(c.tag) != name:
            raise UnmappedElement(f"{where}/{local(c.tag)}")
    return kids


class _Doc:
    """Per-document state: namespace, and blocks that need their original XML."""

    def __init__(self, root):
        self.ns = etree.QName(root).namespace
        self.exact = {}

    def js(self, key: str):
        """Converter storing a block as jsonb (keeping the XML if JSON can't hold it)."""
        def conv(c):
            value, xml = convert(c, self.ns)
            if xml is not None:
                self.exact[key] = xml
            return value
        return conv

    def contact(self, where: str, name: str):
        """ContactGroup (Person or ResponsibleParty) -> {name: value}."""
        return lambda c: {name: self.js(f"{where}/{name}")(c)}


def _regimens(spec: dict, where: str):
    return lambda el: [_read(r, spec, f"{where}/Regimen") for r in _only(el, "Regimen", where)]


def _channel(D: _Doc, c, i: int) -> dict:
    w = "SamplingDetails/Channel"
    return _read(c, {
        "ChannelNumber": ("channel_number", I),
        "SensorNumber": ("sensor_number", I),
        "Start": ("t_start", DT),
        "End": ("t_end", DT),
        "EventTrigger": ("event_trigger", D.js(f"Channel/{i}/EventTrigger")),
        "Sampling": ("sampling", _regimens(
            {"TimeStamp": ("t", DT), "SampleRate_kHz": ("rate", F), "SampleBits": ("bits", I)},
            f"{w}/Sampling")),
        "Gain": ("gain", _regimens(
            {"TimeStamp": ("t", DT), "Gain_dB": ("db", F), "Gain_rel": ("rel", F)},
            f"{w}/Gain")),
        "DutyCycle": ("duty", _regimens(
            {"TimeStamp": ("t", DT),
             # Offfset_s (sic) is how the schema spells it
             "RecordingDuration_s": ("dur", lambda x: (float(x.text), x.get("Offfset_s"))),
             "RecordingInterval_s": ("interval", F)},
            f"{w}/DutyCycle")),
    }, w)


def _quality_assurance(D: _Doc, c) -> dict:
    """QualityAssurance: Description/ResponsibleParty as jsonb, Quality periods as rows."""
    periods = [x for x in _kids(c) if local(x.tag) == "Quality"]
    rest = copy.deepcopy(c)
    for x in _kids(rest):
        if local(x.tag) == "Quality":
            rest.remove(x)
        elif local(x.tag) not in ("Description", "ResponsibleParty"):
            raise UnmappedElement(f"QualityAssurance/{local(x.tag)}")
    w = "QualityAssurance/Quality"
    return {
        "json": D.js("QualityAssurance")(rest),
        "quality": [_read(q, {
            "Start": ("t_start", DT), "End": ("t_end", DT), "Category": ("category", T),
            "FrequencyRange": ("freq", lambda f: _read(
                f, {"Low_Hz": ("low", F), "High_Hz": ("high", F)}, f"{w}/FrequencyRange")),
            "Channel": ("channels[]", I),
            "Comment": ("comment", T),
        }, w) for q in periods],
    }


def _point(p) -> dict:
    return _read(p, {
        "TimeStamp": ("t", DT), "Longitude": ("lon", F), "Latitude": ("lat", F),
        "Heading_DegN": ("heading_degn", F),
        "CourseOverGround_DegN": ("cog", lambda x: (float(x.text), x.get("north"))),
        "Speed": ("speed", F), "SpeedOverGround": ("sog", F),
        "Pitch_deg": ("pitch_deg", F), "Roll_deg": ("roll_deg", F),
        "Elevation_m": ("elevation_m", F), "GroundElevation_m": ("ground_elevation_m", F),
    }, "Data/Tracks/Track/Point")


def _data(D: _Doc, c) -> dict:
    return _read(c, {
        "Audio": ("audio", lambda a: _read(a, {
            "URI": ("uri", T), "FurtherInformationURL": ("info_url", T),
            "ServiceExpectation": ("service", T), "Processed": ("processed", T),
            "Raw": ("raw", T)}, "Data/Audio")),
        "Tracks": ("tracks", lambda t: _read(t, {
            "SpeedUnit": ("speed_unit", T),
            "Track": ("track[]", lambda tr: _read(tr, {
                "TrackId": ("track_id", F), "Point": ("point[]", _point)}, "Data/Tracks/Track")),
            "TrackEffort": ("effort", D.js("TrackEffort")),
            "URI": ("uri[]", T),
            "FurtherInformationURL": ("info_url", T),
            "ServiceExpectation": ("service", T)}, "Data/Tracks")),
    }, "Data")


def _details(D: _Doc, c, where: str) -> dict:
    return _read(c, {
        "Longitude": ("lon", F), "Latitude": ("lat", F),
        "ElevationInstrument_m": ("elevation_instrument_m", F),
        "DepthInstrument_m": ("depth_instrument_m", F),
        "Elevation_m": ("elevation_m", F),
        "TimeStamp": ("t", DT), "AudioTimeStamp": ("t_audio", DT),
        "Vessel": ("vessel", T),
        "Person": ("contact", D.contact(where, "Person")),
        "ResponsibleParty": ("contact", D.contact(where, "ResponsibleParty")),
    }, where)


def _sensors(D: _Doc, c) -> dict:
    out = {"reference_point": None, "rows": []}
    count = {"Audio": 0, "Depth": 0, "Sensor": 0}
    for x in _kids(c):
        n = local(x.tag)
        if n == "ReferencePoint":
            out["reference_point"] = text(x)
            continue
        if n not in count:
            raise UnmappedElement(f"Sensors/{n}")
        i = count[n]
        count[n] += 1
        spec = {"Number": ("number", I), "SensorId": ("sensor_ref", T),
                "Geometry": ("geometry", lambda g: _read(
                    g, {"x_m": ("x", F), "y_m": ("y", F), "z_m": ("z", F)}, f"Sensors/{n}/Geometry")),
                "Name": ("name", T), "Description": ("description", T)}
        if n == "Audio":
            spec.update(HydrophoneId=("hydrophone_ref", T), PreampId=("preamp_ref", T))
        elif n == "Sensor":
            spec.update(Type=("type", T), Properties=("properties", D.js(f"Sensors/Sensor/{i}/Properties")))
        r = _read(x, spec, f"Sensors/{n}")
        r.update(element=n, ord=i)
        out["rows"].append(r)
    return out


def parse_deployment(root) -> dict:
    """A parsed Deployment document (root element) as nested dicts."""
    if local(root.tag) != "Deployment":
        raise ValueError(f"Expected a Deployment document, got {local(root.tag)}")
    D = _Doc(root)
    d = _read(root, {
        "Id": ("deployment_id", lambda c: c.text.strip()),
        "Description": ("description", D.js("Description")),
        "Project": ("project", T),
        "DeploymentNumber": ("deployment_number", I),
        "DeploymentAlias": ("alias", T),
        "Site": ("site", T),
        "SiteAliases": ("site_aliases", lambda c: [text(s) for s in _only(c, "Site", "SiteAliases")]),
        "Cruise": ("cruise", T),
        "Platform": ("platform", T),
        "Region": ("region", T),
        "Instrument": ("instrument", lambda c: _read(c, {
            "Type": ("type", T), "InstrumentId": ("id", T),
            "GeometryType": ("geometry_type", T)}, "Instrument")),
        "SamplingDetails": ("channels", lambda c: [
            _channel(D, x, i) for i, x in enumerate(_only(c, "Channel", "SamplingDetails"))]),
        "QualityAssurance": ("qa", lambda c: _quality_assurance(D, c)),
        "Data": ("data", lambda c: _data(D, c)),
        "DeploymentDetails": ("deploy", lambda c: _details(D, c, "DeploymentDetails")),
        "RecoveryDetails": ("recover", lambda c: _details(D, c, "RecoveryDetails")),
        "Sensors": ("sensors", lambda c: _sensors(D, c)),
        "MetadataInfo": ("metadata_info", D.js("MetadataInfo")),
    }, "Deployment")
    d["xml_namespace"] = D.ns
    d["root_attrs"] = dict(root.attrib) or None
    d["exact_xml"] = D.exact or None
    return d


# ------------------------------------------------------------------ storing
def _j(v):
    return None if v is None else Jsonb(v)


def _upsert(cur, table: str, key: dict) -> int:
    cols = list(key)
    cur.execute(
        f"INSERT INTO nereus.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
        f"ON CONFLICT ({', '.join(cols)}) DO UPDATE SET {cols[0]} = EXCLUDED.{cols[0]} RETURNING id",
        list(key.values()))
    return cur.fetchone()[0]


def _deployment_row(cur, d: dict) -> dict:
    ins = d.get("instrument") or {}
    data = d.get("data") or {}
    audio = data.get("audio") or {}
    tracks = data.get("tracks")
    qa = d.get("qa")
    sensors = d.get("sensors") or {}
    project_id = _upsert(cur, "project", {"name": d["project"]})
    row = {
        "deployment_id": d["deployment_id"],
        "xml_namespace": d["xml_namespace"],
        "root_attrs": _j(d["root_attrs"]),
        "project_id": project_id,
        "site_id": None if d.get("site") is None else
                   _upsert(cur, "site", {"project_id": project_id, "name": d["site"]}),
        "instrument_id": None if not ins else
                         _upsert(cur, "instrument", {"type": ins["type"], "instrument_id": ins["id"]}),
        "deployment_number": d["deployment_number"],
        "alias": d.get("alias"),
        "site_aliases": d.get("site_aliases"),
        "cruise": d.get("cruise"),
        "platform": d["platform"],
        "region": d.get("region"),
        "geometry_type": ins.get("geometry_type"),
        "description": _j(d.get("description")),
        "quality_assurance": None if qa is None else Jsonb(qa["json"]),
        "metadata_info": _j(d.get("metadata_info")),
        "exact_xml": _j(d["exact_xml"]),
        "audio_uri": audio.get("uri"),
        "audio_info_url": audio.get("info_url"),
        "audio_service": audio.get("service"),
        "audio_processed": audio.get("processed"),
        "audio_raw": audio.get("raw"),
        "has_tracks": tracks is not None,
        "track_speed_unit": (tracks or {}).get("speed_unit"),
        "track_effort": _j((tracks or {}).get("effort")),
        "track_uris": (tracks or {}).get("uri"),
        "track_info_url": (tracks or {}).get("info_url"),
        "track_service": (tracks or {}).get("service"),
        "sensor_reference_point": sensors.get("reference_point"),
    }
    for prefix, key in (("deploy", "deploy"), ("recover", "recover")):
        det = d.get(key) or {}
        row.update({
            f"{prefix}_lon": det.get("lon"),
            f"{prefix}_lat": det.get("lat"),
            f"{prefix}_elevation_instrument_m": det.get("elevation_instrument_m"),
            f"{prefix}_depth_instrument_m": det.get("depth_instrument_m"),
            f"{prefix}_elevation_m": det.get("elevation_m"),
            f"t_{prefix}": det.get("t"),
            f"t_{prefix}_audio": det.get("t_audio"),
            f"{prefix}_vessel": det.get("vessel"),
            f"{prefix}_contact": _j(det.get("contact")),
        })
    return row


def _asset(cur, kind: str, serial: str | None) -> int | None:
    if not serial:
        return None
    return _upsert(cur, "sensor", {"kind": kind, "serial": serial})


def store_deployment(conn: psycopg.Connection, d: dict, replace: bool = False) -> int:
    """Store a parsed Deployment (parse_deployment). Returns deployment.id."""
    with conn.transaction(), conn.cursor() as cur:
        old = cur.execute("SELECT id FROM nereus.deployment WHERE deployment_id = %s",
                          (d["deployment_id"],)).fetchone()
        if old and not replace:
            raise ValueError(f"Deployment {d['deployment_id']} already exists (use replace)")
        row = _deployment_row(cur, d)
        cols = list(row)
        cur.execute(
            f"INSERT INTO nereus.deployment ({', '.join(cols)}) "
            f"VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT (deployment_id) DO UPDATE SET "
            + ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[1:]) + ", ingested_at = now() "
            "RETURNING id", [row[c] for c in cols])
        dep = cur.fetchone()[0]
        if old:
            for t in ("channel", "recording_quality", "track", "deployment_sensor"):
                cur.execute(f"DELETE FROM nereus.{t} WHERE deployment_id = %s", (dep,))

        chans, samp, gain, duty = [], [], [], []
        for i, ch in enumerate(d.get("channels") or []):
            chans.append((dep, i, ch["channel_number"], ch["sensor_number"], ch["t_start"],
                          ch["t_end"], _j(ch.get("event_trigger")), "gain" in ch, "duty" in ch))
            samp += [(dep, i, j, r["t"], r["rate"], r["bits"]) for j, r in enumerate(ch["sampling"])]
            gain += [(dep, i, j, r["t"], r.get("db"), r.get("rel"))
                     for j, r in enumerate(ch.get("gain") or [])]
            duty += [(dep, i, j, r["t"], r["dur"][0],
                      None if r["dur"][1] is None else float(r["dur"][1]), r["interval"])
                     for j, r in enumerate(ch.get("duty") or [])]
        cur.executemany("INSERT INTO nereus.channel (deployment_id, ord, channel_number, "
                        "sensor_number, t_start, t_end, event_trigger, has_gain, has_duty_cycle) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", chans)
        cur.executemany("INSERT INTO nereus.channel_sampling VALUES (%s,%s,%s,%s,%s,%s)", samp)
        cur.executemany("INSERT INTO nereus.channel_gain VALUES (%s,%s,%s,%s,%s,%s)", gain)
        cur.executemany("INSERT INTO nereus.channel_duty_cycle VALUES (%s,%s,%s,%s,%s,%s,%s)", duty)

        cur.executemany(
            "INSERT INTO nereus.recording_quality VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(dep, i, q["t_start"], q["t_end"], q["category"],
              (q.get("freq") or {}).get("low"), (q.get("freq") or {}).get("high"),
              q.get("channels"), q.get("comment"))
             for i, q in enumerate((d.get("qa") or {}).get("quality") or [])])

        tracks = ((d.get("data") or {}).get("tracks") or {}).get("track") or []
        cur.executemany("INSERT INTO nereus.track VALUES (%s,%s,%s)",
                        [(dep, i, t.get("track_id")) for i, t in enumerate(tracks)])
        with cur.copy("COPY nereus.track_point (deployment_id, track_ord, ord, t, lon, lat, "
                      "heading_degn, cog_degn, cog_north, speed, sog, pitch_deg, roll_deg, "
                      "elevation_m, ground_elevation_m) FROM STDIN") as cp:
            for i, t in enumerate(tracks):
                for j, p in enumerate(t.get("point") or []):
                    cog, north = p.get("cog", (None, None))
                    cp.write_row((dep, i, j, p["t"], p.get("lon"), p.get("lat"),
                                  p.get("heading_degn"), cog, north, p.get("speed"), p.get("sog"),
                                  p.get("pitch_deg"), p.get("roll_deg"), p.get("elevation_m"),
                                  p.get("ground_elevation_m")))

        rows = []
        for s in (d.get("sensors") or {}).get("rows", []):
            g = s.get("geometry") or {}
            asset_kind = {"Depth": "depth", "Sensor": "other"}.get(s["element"])
            rows.append((dep, s["element"], s["ord"], s["number"], s["sensor_ref"],
                         g.get("x"), g.get("y"), g.get("z"), s.get("name"), s.get("description"),
                         s.get("hydrophone_ref"), s.get("preamp_ref"), s.get("type"),
                         _j(s.get("properties")),
                         _asset(cur, "hydrophone", s.get("hydrophone_ref")),
                         _asset(cur, "preamplifier", s.get("preamp_ref")),
                         _asset(cur, asset_kind, s["sensor_ref"]) if asset_kind else None))
        cur.executemany("INSERT INTO nereus.deployment_sensor VALUES "
                        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)

        cur.execute("SELECT nereus.link_deployment(%s)", (dep,))
    return dep


def ingest_deployment(conn: psycopg.Connection, source, replace: bool = False,
                      schema: etree.XMLSchema | None = None) -> dict:
    """Import one Deployment document (path or file object). With `schema`,
    it is validated first and nothing is stored if it is invalid."""
    parser = etree.XMLParser(schema=schema, remove_comments=True, huge_tree=True)
    d = parse_deployment(etree.parse(source, parser).getroot())
    dep = store_deployment(conn, d, replace=replace)
    return {"id": dep, "doc_id": d["deployment_id"], "channels": len(d.get("channels") or [])}


# ------------------------------------------------------------------ writing
def _open_close(name: str, value, exact: str | None, ns: str | None, depth: int):
    """Start and end tags of a jsonb block whose repeated children come from
    a table (e.g. QualityAssurance, whose Quality periods are rows)."""
    xml = exact if exact is not None else block_to_xml(name, "" if value is None else value, ns)
    close = f"</{name}>"
    head = xml[:-2] + ">" if xml.endswith("/>") else xml[:-len(close)]
    return f"{IND * depth}{head}\n", f"{IND * depth}{close}\n"


def _contact(value, exact: dict, where: str, ns, depth: int) -> str:
    if value is None:
        return ""
    (name, v), = value.items()
    return _block(name, v, exact.get(f"{where}/{name}"), ns, depth)


def _write_details(r: dict, prefix: str, name: str, exact: dict, ns) -> str:
    d = 2
    num = lambda k, col: _opt(k, r[col], d, format_num)
    return "".join([
        f"{IND}<{name}>\n",
        num("Longitude", f"{prefix}_lon"),
        num("Latitude", f"{prefix}_lat"),
        num("ElevationInstrument_m", f"{prefix}_elevation_instrument_m"),
        num("DepthInstrument_m", f"{prefix}_depth_instrument_m"),
        num("Elevation_m", f"{prefix}_elevation_m"),
        _opt("TimeStamp", r[f"t_{prefix}"], d, format_time),
        _opt("AudioTimeStamp", r[f"t_{prefix}_audio"], d, format_time),
        _opt("Vessel", r[f"{prefix}_vessel"], d),
        _contact(r[f"{prefix}_contact"], exact, name, ns, d),
        f"{IND}</{name}>\n"])


def export_deployment(conn: psycopg.Connection, deployment_id: str, out) -> int:
    """Write Deployment `deployment_id` as ASA/Tethys XML to text stream
    `out`. Returns the number of channels written."""
    with conn.transaction(), conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        r = cur.execute("""
            SELECT d.*, p.name AS project, s.name AS site,
                   i.type AS instrument_type, i.instrument_id AS instrument_ref
            FROM nereus.deployment d
            JOIN nereus.project p ON p.id = d.project_id
            LEFT JOIN nereus.site s ON s.id = d.site_id
            LEFT JOIN nereus.instrument i ON i.id = d.instrument_id
            WHERE d.deployment_id = %s""", (deployment_id,)).fetchone()
        if r is None:
            raise KeyError(deployment_id)
        dep, ns, exact = r["id"], r["xml_namespace"], r["exact_xml"] or {}
        q = lambda sql: cur.execute(sql, (dep,)).fetchall()
        channels = q("SELECT * FROM nereus.channel WHERE deployment_id = %s ORDER BY ord")
        regs = {}
        for t in ("channel_sampling", "channel_gain", "channel_duty_cycle"):
            for g in q(f"SELECT * FROM nereus.{t} WHERE deployment_id = %s ORDER BY channel_ord, ord"):
                regs.setdefault((t, g["channel_ord"]), []).append(g)
        quality = q("SELECT * FROM nereus.recording_quality WHERE deployment_id = %s ORDER BY ord")
        tracks = q("SELECT * FROM nereus.track WHERE deployment_id = %s ORDER BY ord")
        points = {}
        for p in q("SELECT * FROM nereus.track_point WHERE deployment_id = %s ORDER BY track_ord, ord"):
            points.setdefault(p["track_ord"], []).append(p)
        sensors = q("SELECT * FROM nereus.deployment_sensor WHERE deployment_id = %s "
                    "ORDER BY array_position(ARRAY['Audio','Depth','Sensor'], element), ord")

    w = out.write
    w(root_open("Deployment", ns, r["root_attrs"]))
    w(_el("Id", r["deployment_id"], 1))
    w(_block("Description", r["description"], exact.get("Description"), ns, 1))
    w(_el("Project", r["project"], 1))
    w(_el("DeploymentNumber", r["deployment_number"], 1))
    w(_opt("DeploymentAlias", r["alias"], 1))
    w(_opt("Site", r["site"], 1))
    if r["site_aliases"] is not None:
        w(f"{IND}<SiteAliases>\n" + "".join(_el("Site", a, 2) for a in r["site_aliases"])
          + f"{IND}</SiteAliases>\n")
    w(_opt("Cruise", r["cruise"], 1))
    w(_el("Platform", r["platform"], 1))
    w(_opt("Region", r["region"], 1))
    w(f"{IND}<Instrument>\n" + _el("Type", r["instrument_type"] or "", 2)
      + _el("InstrumentId", r["instrument_ref"] or "", 2)
      + _opt("GeometryType", r["geometry_type"], 2) + f"{IND}</Instrument>\n")

    w(f"{IND}<SamplingDetails>\n")
    for c in channels:
        i = c["ord"]
        w(f"{IND * 2}<Channel>\n")
        w(_el("ChannelNumber", c["channel_number"], 3))
        w(_el("SensorNumber", c["sensor_number"], 3))
        w(_el("Start", format_time(c["t_start"]), 3))
        w(_el("End", format_time(c["t_end"]), 3))
        w(_block("EventTrigger", c["event_trigger"], exact.get(f"Channel/{i}/EventTrigger"), ns, 3))
        w(f"{IND * 3}<Sampling>\n")
        for g in regs.get(("channel_sampling", i), []):
            w(f"{IND * 4}<Regimen>\n" + _el("TimeStamp", format_time(g["t"]), 5)
              + _el("SampleRate_kHz", format_num(g["sample_rate_khz"]), 5)
              + _el("SampleBits", g["sample_bits"], 5) + f"{IND * 4}</Regimen>\n")
        w(f"{IND * 3}</Sampling>\n")
        if c["has_gain"]:
            w(f"{IND * 3}<Gain>\n")
            for g in regs.get(("channel_gain", i), []):
                w(f"{IND * 4}<Regimen>\n" + _el("TimeStamp", format_time(g["t"]), 5)
                  + _opt("Gain_dB", g["gain_db"], 5, format_num)
                  + _opt("Gain_rel", g["gain_rel"], 5, format_num) + f"{IND * 4}</Regimen>\n")
            w(f"{IND * 3}</Gain>\n")
        if c["has_duty_cycle"]:
            w(f"{IND * 3}<DutyCycle>\n")
            for g in regs.get(("channel_duty_cycle", i), []):
                off = None if g["offset_s"] is None else format_num(g["offset_s"])
                w(f"{IND * 4}<Regimen>\n" + _el("TimeStamp", format_time(g["t"]), 5)
                  + _el("RecordingDuration_s", format_num(g["duration_s"]), 5, {"Offfset_s": off})
                  + _el("RecordingInterval_s", format_num(g["interval_s"]), 5)
                  + f"{IND * 4}</Regimen>\n")
            w(f"{IND * 3}</DutyCycle>\n")
        w(f"{IND * 2}</Channel>\n")
    w(f"{IND}</SamplingDetails>\n")

    if r["quality_assurance"] is not None:
        head, tail = _open_close("QualityAssurance", r["quality_assurance"],
                                 exact.get("QualityAssurance"), ns, 1)
        w(head)
        for g in quality:
            w(f"{IND * 2}<Quality>\n" + _el("Start", format_time(g["t_start"]), 3)
              + _el("End", format_time(g["t_end"]), 3) + _el("Category", g["category"], 3))
            if g["low_hz"] is not None:
                w(f"{IND * 3}<FrequencyRange>\n" + _el("Low_Hz", format_num(g["low_hz"]), 4)
                  + _el("High_Hz", format_num(g["high_hz"]), 4) + f"{IND * 3}</FrequencyRange>\n")
            w("".join(_el("Channel", ch, 3) for ch in g["channels"] or []))
            w(_opt("Comment", g["comment"], 3) + f"{IND * 2}</Quality>\n")
        w(tail)

    w(f"{IND}<Data>\n{IND * 2}<Audio>\n")
    w(_el("URI", r["audio_uri"] or "", 3))
    w(_opt("FurtherInformationURL", r["audio_info_url"], 3))
    w(_opt("ServiceExpectation", r["audio_service"], 3))
    w(_opt("Processed", r["audio_processed"], 3))
    w(_opt("Raw", r["audio_raw"], 3))
    w(f"{IND * 2}</Audio>\n")
    if r["has_tracks"]:
        w(f"{IND * 2}<Tracks>\n")
        w(_opt("SpeedUnit", r["track_speed_unit"], 3))
        for t in tracks:
            w(f"{IND * 3}<Track>\n" + _opt("TrackId", t["track_id"], 4, format_num))
            for p in points.get(t["ord"], []):
                n = lambda k, col: _opt(k, p[col], 5, format_num)
                w(f"{IND * 4}<Point>\n" + _el("TimeStamp", format_time(p["t"]), 5)
                  + n("Longitude", "lon") + n("Latitude", "lat")
                  + n("Heading_DegN", "heading_degn")
                  + ("" if p["cog_degn"] is None else _el(
                      "CourseOverGround_DegN", format_num(p["cog_degn"]), 5, {"north": p["cog_north"]}))
                  + n("Speed", "speed") + n("SpeedOverGround", "sog")
                  + n("Pitch_deg", "pitch_deg") + n("Roll_deg", "roll_deg")
                  + n("Elevation_m", "elevation_m") + n("GroundElevation_m", "ground_elevation_m")
                  + f"{IND * 4}</Point>\n")
            w(f"{IND * 3}</Track>\n")
        w(_block("TrackEffort", r["track_effort"], exact.get("TrackEffort"), ns, 3))
        w("".join(_el("URI", u, 3) for u in r["track_uris"] or []))
        w(_opt("FurtherInformationURL", r["track_info_url"], 3))
        w(_opt("ServiceExpectation", r["track_service"], 3))
        w(f"{IND * 2}</Tracks>\n")
    w(f"{IND}</Data>\n")

    w(_write_details(r, "deploy", "DeploymentDetails", exact, ns))
    if r["t_recover"] is not None:
        w(_write_details(r, "recover", "RecoveryDetails", exact, ns))

    w(f"{IND}<Sensors>\n")
    w(_opt("ReferencePoint", r["sensor_reference_point"], 2))
    for s in sensors:
        el, i = s["element"], s["ord"]
        w(f"{IND * 2}<{el}>\n" + _el("Number", s["number"], 3) + _el("SensorId", s["sensor_ref"], 3))
        if s["x_m"] is not None:
            w(f"{IND * 3}<Geometry>\n" + _el("x_m", format_num(s["x_m"]), 4)
              + _el("y_m", format_num(s["y_m"]), 4) + _el("z_m", format_num(s["z_m"]), 4)
              + f"{IND * 3}</Geometry>\n")
        w(_opt("Name", s["name"], 3) + _opt("Description", s["description"], 3))
        w(_opt("HydrophoneId", s["hydrophone_ref"], 3) + _opt("PreampId", s["preamp_ref"], 3))
        w(_opt("Type", s["type"], 3))
        w(_block("Properties", s["properties"], exact.get(f"Sensors/Sensor/{i}/Properties"), ns, 3))
        w(f"{IND * 2}</{el}>\n")
    w(f"{IND}</Sensors>\n")

    w(_block("MetadataInfo", r["metadata_info"], exact.get("MetadataInfo"), ns, 1))
    w("</Deployment>\n")
    return len(channels)
