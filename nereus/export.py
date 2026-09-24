"""Rebuild an ASA/Tethys Detections XML document from PostgreSQL.

Elements are written in the schema's sequence order (taken from the Nilus
JAXB propOrder). Detections are streamed from a server-side cursor, so
exporting a very large set does not load it into memory.
"""

import json
from xml.sax.saxutils import escape, quoteattr

import psycopg

from .asa import format_time, format_num
from .xmljson import block_to_xml

XSI = "http://www.w3.org/2001/XMLSchema-instance"
IND = "   "  # Tethys indents with three spaces


def _el(name: str, value, depth: int, attrs: dict | None = None) -> str:
    a = "".join(f" {k}={quoteattr(v)}" for k, v in (attrs or {}).items() if v is not None)
    if value is None or value == "":
        return f"{IND * depth}<{name}{a}></{name}>\n" if value == "" else ""
    return f"{IND * depth}<{name}{a}>{escape(str(value))}</{name}>\n"


def _opt(name, value, depth, conv=str):
    return "" if value is None else _el(name, conv(value) if value != "" else "", depth)


def _list(values) -> str:
    return " ".join(format_num(v) for v in values)


def _frag(xml: str | None, depth: int) -> str:
    return "" if xml is None else f"{IND * depth}{xml}\n"


def _block(name: str, value, exact: str | None, ns: str | None, depth: int) -> str:
    """A jsonb block as XML; the original XML wins when one was kept."""
    if exact is not None:
        return _frag(exact, depth)
    if value is None:
        return ""
    return _frag(block_to_xml(name, value, ns), depth)


def _species(tsn, group, depth):
    return _el("SpeciesId", tsn, depth, {"Group": group})


# Float arrays are formatted by PostgreSQL (space separated, shortest exact
# form) rather than converted to Python floats and back: much faster for
# whistle contours, which can hold thousands of points per detection.
_ARRAYS = ["freq_measurements_db", "peaks_hz", "sideband_hz",
           "tonal_offset_s", "tonal_hz", "tonal_db"]
# (d.* first: with duplicate names in a dict row, the later column wins.)
_DETECTION_SELECT = "SELECT " + ", ".join(
    ["d.*"] + [f"array_to_string({c}, ' ') AS {c}" for c in _ARRAYS]) + " FROM nereus.detection d"


def _detection(r: dict, ns: str | None = None) -> str:
    d = 3
    out = [f"{IND * 2}<Detection>\n",
           _opt("Input_file", r["input_file"], d),
           _el("Start", format_time(r["t_start"]), d),
           _opt("End", r["t_end"], d, format_time),
           _opt("Count", r["count"], d),
           _opt("Event", r["event"], d),
           _opt("UnitId", r["unit_id"], d),
           _opt("Channel", r["channel"], d),
           _species(r["species_tsn"], r["species_group"], d)]
    for c in r["calls"] or []:
        out.append(_el("Call", c, d))
    if r["has_parameters"]:
        p = d + 1
        out.append(f"{IND * d}<Parameters>\n")
        out += [_opt("Subtype", r["subtype"], p),
                _opt("Score", r["score"], p, format_num),
                _opt("Confidence", r["confidence"], p, format_num),
                _opt("QualityAssurance", r["qa"], p),
                _opt("ReceivedLevel_dB", r["received_level_db"], p, format_num),
                _opt("FrequencyMeasurements_dB", r["freq_measurements_db"], p),
                _opt("SNR_dB", r["snr_db"], p, format_num),
                _opt("MinFreq_Hz", r["min_freq_hz"], p, format_num),
                _opt("MaxFreq_Hz", r["max_freq_hz"], p, format_num),
                _opt("PeakFreq_Hz", r["peak_freq_hz"], p, format_num),
                _opt("Peaks_Hz", r["peaks_hz"], p),
                _opt("Duration_s", r["duration_s"], p, format_num),
                _opt("Sideband_Hz", r["sideband_hz"], p)]
        if r["has_tonal"]:
            out += [f"{IND * p}<Tonal>\n",
                    _opt("Offset_s", r["tonal_offset_s"], p + 1),
                    _opt("Hz", r["tonal_hz"], p + 1),
                    _opt("dB", r["tonal_db"], p + 1),
                    f"{IND * p}</Tonal>\n"]
        for e in r["event_ref"] or []:
            out.append(_el("EventRef", e, p))
        out.append(_block("UserDefined", r["user_defined"], r["user_defined_xml"], ns, p))
        out.append(f"{IND * d}</Parameters>\n")
    out += [_opt("Image", r["image"], d),
            _opt("Audio", r["audio"], d),
            _opt("Comment", r["comment"], d),
            f"{IND * 2}</Detection>\n"]
    return "".join(out)


def root_open(name: str, ns: str | None, attrs) -> str:
    """XML declaration and root start tag, with the root attributes kept at import."""
    if isinstance(attrs, str):
        attrs = json.loads(attrs)
    root_attr = []
    need_xsi = False
    for k, v in (attrs or {}).items():
        if k.startswith(f"{{{XSI}}}"):
            need_xsi = True
            k = "xsi:" + k.split("}", 1)[1]
        root_attr.append(f" {k}={quoteattr(v)}")
    nsdecl = (f' xmlns="{ns}"' if ns else "") + (f' xmlns:xsi="{XSI}"' if need_xsi else "")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<{name}{"".join(root_attr)}{nsdecl}>\n'


def _effort(cur, s: dict, e: dict) -> str:
    d = 2
    out = [f"{IND}<Effort>\n",
           _el("Start", format_time(e["t_start"]), d),
           _el("End", format_time(e["t_end"]), d)]
    cur.execute("SELECT t, duration_s, offset_s, interval_s FROM nereus.analysis_gap_periodic "
                "WHERE set_id = %s ORDER BY ord", (s["id"],))
    periodic = cur.fetchall()
    cur.execute("SELECT t_start, t_end, reason FROM nereus.analysis_gap_aperiodic "
                "WHERE set_id = %s ORDER BY ord", (s["id"],))
    aperiodic = cur.fetchall()
    if periodic or aperiodic:
        out.append(f"{IND * d}<AnalysisGaps>\n")
        if periodic:
            out.append(f"{IND * 3}<Periodic>\n")
            for g in periodic:
                t, dur, off, interval = g["t"], g["duration_s"], g["offset_s"], g["interval_s"]
                out += [f"{IND * 4}<Regimen>\n",
                        _el("TimeStamp", format_time(t), 5),
                        _el("AnalysisDuration_s", format_num(dur), 5,
                            {"Offfset_s": None if off is None else format_num(off)}),
                        _el("AnalysisInterval_s", format_num(interval), 5),
                        f"{IND * 4}</Regimen>\n"]
            out.append(f"{IND * 3}</Periodic>\n")
        for g in aperiodic:
            a, b, reason = g["t_start"], g["t_end"], g["reason"]
            out += [f"{IND * 3}<Aperiodic>\n", _el("Start", format_time(a), 4),
                    _el("End", format_time(b), 4), _opt("Reason", reason, 4),
                    f"{IND * 3}</Aperiodic>\n"]
        out.append(f"{IND * d}</AnalysisGaps>\n")
    out.append(_opt("IntensityReference_uPa", e["intensity_ref_upa"], d, format_num))

    cur.execute("SELECT * FROM nereus.effort_kind WHERE set_id = %s ORDER BY ord", (s["id"],))
    for k in cur.fetchall():
        out += [f"{IND * d}<Kind>\n", _species(k["species_tsn"], k["species_group"], 3),
                _opt("Call", k["call"], 3)]
        if k["has_parameters"]:
            out += [f"{IND * 3}<Parameters>\n", _opt("Subtype", k["subtype"], 4),
                    _opt("FrequencyMeasurements_Hz", k["freq_measurements_hz"], 4, _list),
                    f"{IND * 3}</Parameters>\n"]
        out += [_el("Granularity", k["granularity"], 3, {
                    "BinSize_min": None if k["bin_size_min"] is None else format_num(k["bin_size_min"]),
                    "FirstBinStart": None if k["first_bin_start"] is None else format_time(k["first_bin_start"]),
                    "EncounterGap_min": None if k["encounter_gap_min"] is None else format_num(k["encounter_gap_min"]),
                }),
                f"{IND * d}</Kind>\n"]
    out.append(f"{IND}</Effort>\n")
    return "".join(out)


def export(conn: psycopg.Connection, doc_id: str, out) -> int:
    """Write the Detections document `doc_id` to text stream `out`.
    Returns the number of detections written.

    Runs in its own transaction block: the server-side cursors used for
    streaming need one, and without this they would leave an implicit
    transaction open that swallowed the caller's later writes."""
    with conn.transaction():
        return _export(conn, doc_id, out)


def _export(conn: psycopg.Connection, doc_id: str, out) -> int:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute("SELECT * FROM nereus.detection_set WHERE doc_id = %s", (doc_id,))
        s = cur.fetchone()
        if s is None:
            raise KeyError(doc_id)

        cur.execute("SELECT * FROM nereus.effort WHERE set_id = %s", (s["id"],))
        e = cur.fetchone()

        ns = s["xml_namespace"]
        out.write(root_open("Detections", ns, s["root_attrs"]))
        out.write(_el("Id", s["doc_id"], 1))
        exact = s["exact_xml"] or {}
        blk = lambda name, col, depth, key=None: _block(name, s[col], exact.get(key or name), ns, depth)
        out.write(blk("Description", "description", 1))
        out.write(f"{IND}<DataSource>\n")
        out.write(_opt("EnsembleId", e["ensemble_ref"], 2))
        out.write(_opt("DeploymentId", e["deployment_ref"], 2))
        out.write(f"{IND}</DataSource>\n")
        out.write(f"{IND}<Algorithm>\n")
        out.write(_opt("Method", s["algorithm_method"], 2))
        out.write(_opt("Software", s["algorithm_software"], 2))
        out.write(_opt("Version", s["algorithm_version"], 2))
        out.write(blk("Parameters", "algorithm_parameters", 2, "Algorithm/Parameters"))
        if "Algorithm/SupportSoftware" in exact:
            for f in exact["Algorithm/SupportSoftware"]:
                out.write(_frag(f, 2))
        else:
            for v in s["algorithm_support"] or []:
                out.write(_block("SupportSoftware", v, None, ns, 2))
        out.write(f"{IND}</Algorithm>\n")
        out.write(blk("QualityAssurance", "quality_assurance", 1))
        out.write(_opt("UserId", s["user_id"], 1))
        out.write(_effort(cur, s, e))
        set_id = s["id"]

    n = 0
    groups = [("OnEffort", True)] + ([("OffEffort", False)] if s["has_offeffort"] else [])
    for group, flag in groups:
        out.write(f"{IND}<{group}>\n")
        with conn.cursor(name=f"exp_{set_id}_{group}",
                         row_factory=psycopg.rows.dict_row) as cur:
            cur.itersize = 20000
            cur.execute(_DETECTION_SELECT + " WHERE set_id = %s AND on_effort = %s "
                        "ORDER BY ord", (set_id, flag))
            for r in cur:
                out.write(_detection(r, ns))
                n += 1
        out.write(f"{IND}</{group}>\n")

    out.write(blk("BespokeData", "bespoke_data", 1))
    out.write(blk("MetadataInfo", "metadata_info", 1))
    out.write("</Detections>\n")
    return n
