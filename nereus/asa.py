"""Shared helpers for reading and writing ASA/Tethys XML."""

from datetime import datetime, timezone

from lxml import etree

TETHYS_NS = "http://tethys.sdsu.edu/schema/1.0"


def local(tag) -> str:
    """Local name of an element tag, without namespace."""
    return etree.QName(tag).localname


def text(el) -> str | None:
    """Element text; '' for a present-but-empty element, None if absent."""
    if el is None:
        return None
    return el.text if el.text is not None else ""


def parse_time(s: str) -> datetime:
    """xs:dateTime -> aware datetime. Values without a zone are taken as UTC,
    which is the Tethys convention."""
    s = s.strip()
    if "." in s:
        # Python only keeps microseconds: trim extra fractional digits.
        head, frac = s.split(".", 1)
        digits = "".join(c for c in frac if c.isdigit())
        zone = frac[len(digits):]
        s = f"{head}.{digits[:6]}{zone}"
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def format_time(dt: datetime) -> str:
    """Aware datetime -> xs:dateTime in UTC with millisecond precision
    (microseconds when needed), matching what Tethys writes."""
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond % 1000:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def format_num(x) -> str:
    """Shortest round-trippable text for a number (15.0 -> '15')."""
    if isinstance(x, float) and x.is_integer() and abs(x) < 1e15:
        return str(int(x))
    return repr(x) if isinstance(x, float) else str(x)


def num_list(s: str | None) -> list[float] | None:
    """xs:list of doubles (space separated)."""
    if s is None:
        return None
    return [float(v) for v in s.split()]
