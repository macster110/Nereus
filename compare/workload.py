"""Synthetic PAMGuard-style detections for the upload test.

The same detections are written to both systems:
  * Nereus: through nereus.writer (SQL: COPY or batched INSERT), no XML.
  * Tethys: serialised to a Detections XML document and uploaded, which is
    the only way Tethys accepts data (what PAMGuard's Tethys module does now).

Mix: 80 % click detections with the usual per-click measurements, 20 %
whistle detections carrying a ~50-point contour, roughly like PAMGuard's
click detector and Whistle & Moan detector output.
"""

import random
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from nereus.asa import format_num, format_time
from nereus.export import IND, _detection
from nereus.ingest import DETECTION_COLS
from nereus.writer import Kind

TETHYS_NS = "http://tethys.sdsu.edu/schema/1.0"
CLICK_TSN, WHISTLE_TSN = 180473, 180404
KINDS = [Kind(CLICK_TSN, "Clicks", "call"), Kind(WHISTLE_TSN, "Whistles", "call")]


def detections(n: int, start: datetime, span: timedelta, seed: int) -> list[dict]:
    rng = random.Random(seed)
    secs = sorted(rng.uniform(0, span.total_seconds()) for _ in range(n))
    out = []
    for s in secs:
        t = start + timedelta(seconds=round(s, 3))
        if rng.random() < 0.8:
            out.append({
                "t_start": t, "t_end": t + timedelta(milliseconds=1),
                "channel": rng.randint(0, 3), "species_tsn": CLICK_TSN, "calls": ["Clicks"],
                "score": round(rng.random(), 4),
                "received_level_db": round(rng.uniform(100, 160), 1),
                "snr_db": round(rng.uniform(3, 30), 1),
                "min_freq_hz": float(rng.randint(100_000, 125_000)),
                "max_freq_hz": float(rng.randint(130_000, 160_000)),
                "duration_s": round(rng.uniform(0.00005, 0.0002), 6),
            })
        else:
            npts = rng.randint(30, 70)
            f0 = rng.uniform(5000, 15000)
            out.append({
                "t_start": t, "t_end": t + timedelta(seconds=npts * 0.002),
                "channel": rng.randint(0, 3), "species_tsn": WHISTLE_TSN, "calls": ["Whistles"],
                "tonal_offset_s": [round(i * 0.002, 3) for i in range(npts)],
                "tonal_hz": [round(f0 + 40 * i + rng.uniform(-50, 50), 1) for i in range(npts)],
            })
    return out


def _export_row(d: dict) -> dict:
    """Detection dict -> the row shape nereus.export._detection formats."""
    r = dict.fromkeys(DETECTION_COLS)
    r.update(d)
    for k in ("freq_measurements_db", "peaks_hz", "sideband_hz",
              "tonal_offset_s", "tonal_hz", "tonal_db"):
        if r.get(k) is not None:
            r[k] = " ".join(format_num(float(x)) for x in r[k])
    r["has_tonal"] = d.get("tonal_hz") is not None
    r["has_parameters"] = r["has_tonal"] or any(
        d.get(k) is not None for k in ("score", "received_level_db", "snr_db",
                                       "min_freq_hz", "max_freq_hz", "duration_s"))
    return r


def tethys_xml(doc_id: str, deployment: str, effort_start: datetime, effort_end: datetime,
               dets: list[dict]) -> str:
    """A complete Detections document for Tethys, in schema order."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n', f'<Detections xmlns="{TETHYS_NS}">\n',
           f"{IND}<Id>{escape(doc_id)}</Id>\n",
           f"{IND}<DataSource>\n{IND * 2}<DeploymentId>{escape(deployment)}</DeploymentId>\n"
           f"{IND}</DataSource>\n",
           f"{IND}<Algorithm>\n{IND * 2}<Method>Click detector; Whistle and Moan detector</Method>\n"
           f"{IND * 2}<Software>PAMGuard</Software>\n{IND * 2}<Version>2.02.16</Version>\n"
           f"{IND * 2}<Parameters/>\n{IND}</Algorithm>\n",
           f"{IND}<UserId>nereus-benchmark</UserId>\n",
           f"{IND}<Effort>\n{IND * 2}<Start>{format_time(effort_start)}</Start>\n"
           f"{IND * 2}<End>{format_time(effort_end)}</End>\n"]
    for k in KINDS:
        out.append(f"{IND * 2}<Kind>\n{IND * 3}<SpeciesId>{k.species_tsn}</SpeciesId>\n"
                   f"{IND * 3}<Call>{k.call}</Call>\n{IND * 3}<Granularity>{k.granularity}</Granularity>\n"
                   f"{IND * 2}</Kind>\n")
    out.append(f"{IND}</Effort>\n{IND}<OnEffort>\n")
    out.extend(_detection(_export_row(d)) for d in dets)
    out.append(f"{IND}</OnEffort>\n</Detections>\n")
    return "".join(out)


T0 = datetime(2024, 3, 1, tzinfo=timezone.utc)
