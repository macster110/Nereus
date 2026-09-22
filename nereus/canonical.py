"""Decide whether two ASA XML documents carry the same information.

Byte-for-byte equality is the wrong test: indentation, `<a/>` vs `<a></a>`,
namespace prefixes, `15` vs `15.0` and `...00.000Z` vs `...00Z` are all the
same data. This compares the documents' information content instead:

  * element names (namespace-qualified), order, and nesting
  * attribute names and values
  * text, where timestamps are compared as instants and numbers as doubles
  * whitespace-only text is ignored (the schema has no mixed content)

Both files are streamed in lockstep, so memory use stays flat even for
documents of several hundred megabytes.
"""

import re
from itertools import zip_longest

from lxml import etree

from .asa import parse_time

_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _value(s: str | None):
    s = (s or "").strip()
    if _TIME.match(s):
        return ("time", parse_time(s))
    if _NUM.match(s):
        return ("num", float(s))
    parts = s.split()
    if len(parts) > 1 and all(_NUM.match(p) for p in parts):
        return ("list", tuple(float(p) for p in parts))
    return ("str", s)


def _events(path):
    """Canonical event stream: ('start', tag, attrs) and ('end', tag, text)."""
    for ev, el in etree.iterparse(str(path), events=("start", "end"),
                                  remove_comments=True, remove_pis=True,
                                  huge_tree=True):
        if ev == "start":
            yield ("start", el.tag,
                   tuple(sorted((k, _value(v)) for k, v in el.attrib.items())))
        else:
            yield ("end", el.tag, _value(el.text))
            # Free memory: this element is finished, and so are earlier siblings.
            el.clear()
            parent = el.getparent()
            if parent is not None:
                while el.getprevious() is not None:
                    del parent[0]


def equivalent(path_a, path_b, max_diffs: int = 20) -> list[str]:
    """List of human-readable differences between two files (empty = same)."""
    out = []
    stack: list[str] = []
    for a, b in zip_longest(_events(path_a), _events(path_b)):
        if a is None or b is None:
            out.append(f"/{'/'.join(stack)}: "
                       f"{'second' if a is None else 'first'} document ends early")
            break
        kind, tag, val = a
        if kind == "start":
            stack.append(etree.QName(tag).localname)
        here = "/" + "/".join(stack)
        if a[:2] != b[:2]:
            out.append(f"{here}: {a[0]} <{etree.QName(a[1]).localname}> != "
                       f"{b[0]} <{etree.QName(b[1]).localname}>")
            break  # structure diverged; later differences are noise
        if val != b[2]:
            what = "attributes" if kind == "start" else "text"
            out.append(f"{here}: {what} {val} != {b[2]}")
            if len(out) >= max_diffs:
                break
        if kind == "end":
            stack.pop()
    return out
