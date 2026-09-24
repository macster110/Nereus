"""Stream an ASA/Tethys Detections XML document into PostgreSQL.

The document is read with iterparse so memory stays flat however many
detections it holds, and detections are bulk-loaded with COPY.

Any element the importer does not know how to store raises UnmappedElement:
the import fails loudly rather than silently dropping data, which is what
makes the XML -> SQL -> XML round trip trustworthy.
"""

import json
from contextlib import ExitStack

from lxml import etree
import psycopg

from psycopg.types.json import Jsonb

from .asa import local, text, parse_time, num_list
from .xmljson import convert, original_xml


class UnmappedElement(ValueError):
    pass


def pg_float_array(s: str | None) -> str | None:
    """xs:list of doubles -> PostgreSQL float8[] literal, e.g. '1 2.5' -> '{1,2.5}'.
    Postgres parses (and validates) the numbers during COPY; this avoids
    converting every contour point to a Python float and back, which
    dominated import time for whistle-contour documents."""
    if s is None:
        return None
    return "{" + ",".join(s.split()) + "}"


DETECTION_COLS = [
    "set_id", "ord", "kind_ord", "deployment_id", "on_effort", "t_start", "t_end", "input_file", "count",
    "event", "unit_id", "channel", "species_tsn", "species_group", "calls",
    "has_parameters", "subtype", "score", "confidence", "qa",
    "received_level_db", "freq_measurements_db", "snr_db", "min_freq_hz",
    "max_freq_hz", "peak_freq_hz", "peaks_hz", "duration_s", "sideband_hz",
    "tonal_offset_s", "tonal_hz", "tonal_db", "has_tonal", "event_ref",
    "user_defined", "user_defined_xml", "image", "audio", "comment",
]

# Detection/Parameters scalar children -> (column, converter)
_PARAM_SCALARS = {
    "Subtype": ("subtype", str),
    "Score": ("score", float),
    "Confidence": ("confidence", float),
    "QualityAssurance": ("qa", str),
    "ReceivedLevel_dB": ("received_level_db", float),
    "FrequencyMeasurements_dB": ("freq_measurements_db", pg_float_array),
    "SNR_dB": ("snr_db", float),
    "MinFreq_Hz": ("min_freq_hz", float),
    "MaxFreq_Hz": ("max_freq_hz", float),
    "PeakFreq_Hz": ("peak_freq_hz", float),
    "Peaks_Hz": ("peaks_hz", pg_float_array),
    "Duration_s": ("duration_s", float),
    "Sideband_Hz": ("sideband_hz", pg_float_array),
}
_DET_SCALARS = {
    "Input_file": ("input_file", str),
    "Start": ("t_start", parse_time),
    "End": ("t_end", parse_time),
    "Count": ("count", int),
    "Event": ("event", str),
    "UnitId": ("unit_id", int),
    "Channel": ("channel", int),
    "Image": ("image", str),
    "Audio": ("audio", str),
    "Comment": ("comment", str),
}


class KindMatcher:
    """Which Effort/Kind a detection answers, so its granularity is explicit.

    A kind matches on species (and Group, when the kind gives one); when a
    species has several kinds, the detection's Call narrows it down (a kind
    naming one of its calls beats a kind with no call), then Subtype.
    Exactly one match gives that kind's ord; anything else (no match, or
    still ambiguous) gives None."""

    def __init__(self, kinds: list[dict]):
        self.kinds = kinds  # dicts with keys tsn, group, call, subtype
        self.cache = {}

    def __call__(self, tsn, group, calls, subtype) -> int | None:
        key = (tsn, group, tuple(calls or ()), subtype)
        if key not in self.cache:
            self.cache[key] = self._match(*key)
        return self.cache[key]

    def _match(self, tsn, group, calls, subtype):
        cands = [i for i, k in enumerate(self.kinds) if k["tsn"] == tsn
                 and (k["group"] is None or k["group"] == group)]
        if len(cands) > 1:
            cands = ([i for i in cands if self.kinds[i]["call"] in calls]
                     or [i for i in cands if self.kinds[i]["call"] is None])
        if len(cands) > 1:
            cands = [i for i in cands if self.kinds[i]["subtype"] == subtype] or cands
        return cands[0] if len(cands) == 1 else None


class Source:
    """Where a detection set's detections came from. Resolves each
    detection's deployment (from DataSource, or from UnitId via the
    ensemble) and its Effort/Kind."""

    def __init__(self, cur, deployment_ref: str | None, ensemble_ref: str | None,
                 kinds: list[dict]):
        self.kind = KindMatcher(kinds)
        self.single = deployment_ref is not None
        self.deployment_id = self.ensemble_id = None
        self.units = {}
        if deployment_ref is not None:
            row = cur.execute("SELECT id FROM nereus.deployment WHERE deployment_id = %s",
                              (deployment_ref,)).fetchone()
            self.deployment_id = row[0] if row else None
        if ensemble_ref is not None:
            row = cur.execute("SELECT id FROM nereus.ensemble WHERE ensemble_id = %s",
                              (ensemble_ref,)).fetchone()
            if row:
                self.ensemble_id = row[0]
                self.units = dict(cur.execute(
                    "SELECT unit_id, deployment_id FROM nereus.ensemble_unit "
                    "WHERE ensemble_id = %s", (self.ensemble_id,)).fetchall())

    @classmethod
    def of_set(cls, cur, set_id: int) -> "Source":
        """The Source of an existing detection set (used by the writer)."""
        row = cur.execute("SELECT deployment_ref, ensemble_ref FROM nereus.effort "
                          "WHERE set_id = %s", (set_id,)).fetchone()
        if row is None:
            raise KeyError(f"no detection set {set_id}")
        kinds = [dict(zip(("tsn", "group", "call", "subtype"), k)) for k in cur.execute(
            "SELECT species_tsn, species_group, call, subtype FROM nereus.effort_kind "
            "WHERE set_id = %s ORDER BY ord", (set_id,)).fetchall()]
        return cls(cur, row[0], row[1], kinds)

    def deployment(self, unit_id) -> int | None:
        return self.deployment_id if self.single else self.units.get(unit_id)


def detection_row(el, set_id: int, ord_: int, on_effort: bool, src: Source) -> tuple:
    r = dict.fromkeys(DETECTION_COLS)
    r.update(set_id=set_id, ord=ord_, on_effort=on_effort,
             has_parameters=False, has_tonal=False)
    calls = []
    for c in el:
        if not isinstance(c.tag, str):
            continue  # comments / processing instructions
        name = local(c.tag)
        if name in _DET_SCALARS:
            col, conv = _DET_SCALARS[name]
            r[col] = conv(text(c))
        elif name == "SpeciesId":
            r["species_tsn"] = int(c.text)
            r["species_group"] = c.get("Group")
        elif name == "Call":
            calls.append(text(c))
        elif name == "Parameters":
            r["has_parameters"] = True
            refs = []
            for p in c:
                if not isinstance(p.tag, str):
                    continue
                pn = local(p.tag)
                if pn in _PARAM_SCALARS:
                    col, conv = _PARAM_SCALARS[pn]
                    r[col] = conv(text(p))
                elif pn == "Tonal":
                    r["has_tonal"] = True
                    for t in p:
                        tn = local(t.tag)
                        key = {"Offset_s": "tonal_offset_s", "Hz": "tonal_hz",
                               "dB": "tonal_db"}.get(tn)
                        if key is None:
                            raise UnmappedElement(f"Detection/Parameters/Tonal/{tn}")
                        r[key] = pg_float_array(text(t))
                elif pn == "EventRef":
                    refs.append(text(p))
                elif pn == "UserDefined":
                    ud, xml = convert(p, etree.QName(c).namespace)
                    r["user_defined"] = None if ud is None else json.dumps(ud)
                    r["user_defined_xml"] = xml
                else:
                    raise UnmappedElement(f"Detection/Parameters/{pn}")
            r["event_ref"] = refs or None
        else:
            raise UnmappedElement(f"Detection/{name}")
    r["calls"] = calls or None
    r["kind_ord"] = src.kind(r["species_tsn"], r["species_group"], calls, r["subtype"])
    r["deployment_id"] = src.deployment(r["unit_id"])
    return tuple(r[k] for k in DETECTION_COLS)


def _species(el):
    return int(el.text), el.get("Group")


def _parse_effort(el) -> dict:
    eff = {"kinds": [], "periodic": [], "aperiodic": [], "intensity": None}
    for c in el:
        n = local(c.tag)
        if n == "Start":
            eff["start"] = parse_time(c.text)
        elif n == "End":
            eff["end"] = parse_time(c.text)
        elif n == "IntensityReference_uPa":
            eff["intensity"] = float(c.text)
        elif n == "AnalysisGaps":
            for g in c:
                gn = local(g.tag)
                if gn == "Periodic":
                    for reg in g:
                        vals = {local(x.tag): x for x in reg}
                        dur = vals["AnalysisDuration_s"]
                        off = dur.get("Offfset_s")  # sic: spelled this way in the schema
                        eff["periodic"].append((
                            parse_time(vals["TimeStamp"].text), float(dur.text),
                            float(off) if off is not None else None,
                            float(vals["AnalysisInterval_s"].text)))
                elif gn == "Aperiodic":
                    vals = {local(x.tag): x for x in g}
                    eff["aperiodic"].append((
                        parse_time(vals["Start"].text), parse_time(vals["End"].text),
                        text(vals.get("Reason"))))
                else:
                    raise UnmappedElement(f"Effort/AnalysisGaps/{gn}")
        elif n == "Kind":
            k = {"call": None, "subtype": None, "freq": None, "has_params": False,
                 "bin": None, "first": None, "gap": None}
            for kc in c:
                kn = local(kc.tag)
                if kn == "SpeciesId":
                    k["tsn"], k["group"] = _species(kc)
                elif kn == "Call":
                    k["call"] = text(kc)
                elif kn == "Granularity":
                    k["granularity"] = kc.text.strip()
                    if kc.get("BinSize_min") is not None:
                        k["bin"] = float(kc.get("BinSize_min"))
                    if kc.get("FirstBinStart") is not None:
                        k["first"] = parse_time(kc.get("FirstBinStart"))
                    if kc.get("EncounterGap_min") is not None:
                        k["gap"] = float(kc.get("EncounterGap_min"))
                elif kn == "Parameters":
                    k["has_params"] = True
                    for p in kc:
                        pn = local(p.tag)
                        if pn == "Subtype":
                            k["subtype"] = text(p)
                        elif pn == "FrequencyMeasurements_Hz":
                            k["freq"] = num_list(text(p))
                        else:
                            raise UnmappedElement(f"Effort/Kind/Parameters/{pn}")
                else:
                    raise UnmappedElement(f"Effort/Kind/{kn}")
            eff["kinds"].append(k)
        else:
            raise UnmappedElement(f"Effort/{n}")
    return eff


class _Header:
    """Document-level fields gathered before and after the detections."""

    # element name -> detection_set jsonb column
    BLOCKS = {"Description": "description", "QualityAssurance": "quality_assurance",
              "BespokeData": "bespoke_data", "MetadataInfo": "metadata_info"}

    def __init__(self):
        self.v = {"description": None, "quality_assurance": None,
                  "bespoke_data": None, "metadata_info": None,
                  "algorithm_method": None, "algorithm_software": None,
                  "algorithm_version": None, "algorithm_parameters": None,
                  "algorithm_support": None, "user_id": None}
        self.source = {"deployment_ref": None, "ensemble_ref": None}
        self.exact_xml = {}  # blocks JSON can't hold exactly: name -> original XML
        self.effort = None

    def _json(self, key: str, el):
        value, xml = convert(el, etree.QName(el.getparent()).namespace)
        if xml is not None:
            self.exact_xml[key] = xml
        return value

    def take(self, el):
        n = local(el.tag)
        if n == "Id":
            self.v["doc_id"] = el.text.strip()
        elif n in self.BLOCKS:
            self.v[self.BLOCKS[n]] = self._json(n, el)
        elif n == "UserId":
            self.v["user_id"] = text(el)
        elif n == "DataSource":
            for c in el:
                cn = local(c.tag)
                if cn == "DeploymentId":
                    self.source["deployment_ref"] = text(c)
                elif cn == "EnsembleId":
                    self.source["ensemble_ref"] = text(c)
                else:
                    raise UnmappedElement(f"DataSource/{cn}")
        elif n == "Algorithm":
            support = []
            for c in el:
                cn = local(c.tag)
                if cn == "Method":
                    self.v["algorithm_method"] = text(c)
                elif cn == "Software":
                    self.v["algorithm_software"] = text(c)
                elif cn == "Version":
                    self.v["algorithm_version"] = text(c)
                elif cn == "Parameters":
                    self.v["algorithm_parameters"] = self._json("Algorithm/Parameters", c)
                elif cn == "SupportSoftware":
                    support.append(c)
                else:
                    raise UnmappedElement(f"Algorithm/{cn}")
            if support:
                values, xmls = zip(*(convert(c, etree.QName(el).namespace) for c in support))
                self.v["algorithm_support"] = list(values)
                if any(x is not None for x in xmls):  # keep all of them together
                    self.exact_xml["Algorithm/SupportSoftware"] = [original_xml(c) for c in support]
        elif n == "Effort":
            self.effort = _parse_effort(el)
        else:
            raise UnmappedElement(f"Detections/{n}")

    def jsonb(self, key):
        v = self.v[key]
        return None if v is None else Jsonb(v)


def _insert_header(cur, h: _Header, root) -> tuple[int, Source]:
    v = dict(h.v)
    for k in ("description", "quality_assurance", "bespoke_data", "metadata_info",
              "algorithm_parameters", "algorithm_support"):
        v[k] = h.jsonb(k)
    v["exact_xml"] = Jsonb(h.exact_xml) if h.exact_xml else None
    v["xml_namespace"] = etree.QName(root.tag).namespace
    v["root_attrs"] = json.dumps(dict(root.attrib)) if root.attrib else None
    cols = list(v)
    cur.execute(
        f"INSERT INTO nereus.detection_set ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
        [v[c] for c in cols])
    set_id = cur.fetchone()[0]

    ref = h.source
    src = Source(cur, ref["deployment_ref"], ref["ensemble_ref"], h.effort["kinds"])
    cur.execute(
        "INSERT INTO nereus.effort (set_id, deployment_ref, ensemble_ref, deployment_id, "
        "ensemble_id, t_start, t_end, intensity_ref_upa) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (set_id, ref["deployment_ref"], ref["ensemble_ref"], src.deployment_id,
         src.ensemble_id, h.effort["start"], h.effort["end"], h.effort["intensity"]))
    for i, k in enumerate(h.effort["kinds"]):
        cur.execute(
            "INSERT INTO nereus.effort_kind (set_id, ord, species_tsn, species_group, call, "
            "subtype, freq_measurements_hz, has_parameters, granularity, bin_size_min, "
            "first_bin_start, encounter_gap_min) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (set_id, i, k["tsn"], k["group"], k["call"], k["subtype"], k["freq"],
             k["has_params"], k["granularity"], k["bin"], k["first"], k["gap"]))
    for i, g in enumerate(h.effort["periodic"]):
        cur.execute("INSERT INTO nereus.analysis_gap_periodic VALUES (%s,%s,%s,%s,%s,%s)",
                    (set_id, i, *g))
    for i, g in enumerate(h.effort["aperiodic"]):
        cur.execute("INSERT INTO nereus.analysis_gap_aperiodic VALUES (%s,%s,%s,%s,%s)",
                    (set_id, i, *g))
    return set_id, src


def load_schema(path) -> etree.XMLSchema:
    """Load an XSD such as Tethys's lib/schema/tethys.xsd for validating imports."""
    return etree.XMLSchema(etree.parse(str(path)))


def ingest(conn: psycopg.Connection, source, replace: bool = False,
           schema: etree.XMLSchema | None = None) -> dict:
    """Import one Detections document (path or file object).
    Its DataSource is linked to the Deployment or Ensemble when that is
    already imported; otherwise the link is made when it arrives.
    With `schema`, the document is validated against the XSD while it streams
    in; an invalid document raises etree.XMLSyntaxError and nothing is stored.
    Returns {'set_id', 'doc_id', 'detections'}. Runs in one transaction."""
    h = _Header()
    set_id = None
    root = None
    n = 0
    has_off = False
    with conn.transaction(), conn.cursor() as cur, ExitStack() as stack:
        copier = None
        group = None  # 'OnEffort' / 'OffEffort' while inside one
        for event, el in etree.iterparse(source, events=("start", "end"),
                                         remove_comments=True, huge_tree=True,
                                         schema=schema):
            if event == "start":
                if root is None:
                    root = el
                    if local(el.tag) != "Detections":
                        raise ValueError(f"Expected a Detections document, got {local(el.tag)}")
                elif el.getparent() is root and local(el.tag) in ("OnEffort", "OffEffort"):
                    group = local(el.tag)
                    if set_id is None:
                        if replace and "doc_id" in h.v:
                            cur.execute("DELETE FROM nereus.detection_set WHERE doc_id = %s",
                                        (h.v["doc_id"],))
                        set_id, src = _insert_header(cur, h, root)
                        copier = stack.enter_context(cur.copy(
                            f"COPY nereus.detection ({', '.join(DETECTION_COLS)}) FROM STDIN"))
                    if group == "OffEffort":
                        has_off = True
                continue

            parent = el.getparent()
            if group and parent is not None and local(parent.tag) == group \
                    and parent.getparent() is root:
                if local(el.tag) != "Detection":
                    raise UnmappedElement(f"{group}/{local(el.tag)}")
                copier.write_row(detection_row(el, set_id, n, group == "OnEffort", src))
                n += 1
                el.clear()
                while el.getprevious() is not None:  # free parsed siblings
                    del parent[0]
            elif parent is root:
                name = local(el.tag)
                if name in ("OnEffort", "OffEffort"):
                    group = None
                else:
                    # Header blocks, plus trailing BespokeData/MetadataInfo
                    # which are written by the UPDATE below.
                    h.take(el)
                el.clear()

        stack.close()  # finish the COPY before running further statements
        if set_id is None:  # document with no OnEffort element: still store the header
            set_id, _ = _insert_header(cur, h, root)

        cur.execute(
            "UPDATE nereus.detection_set SET bespoke_data = %s, metadata_info = %s, "
            "exact_xml = %s, has_offeffort = %s WHERE id = %s",
            (h.jsonb("bespoke_data"), h.jsonb("metadata_info"),
             Jsonb(h.exact_xml) if h.exact_xml else None,
             has_off, set_id))
        cur.execute("SELECT nereus.refresh_summary(%s)", (set_id,))
    return {"set_id": set_id, "doc_id": h.v.get("doc_id"), "detections": n}
