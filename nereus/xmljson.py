"""XML blocks <-> searchable JSON (stored as PostgreSQL jsonb).

Blocks that don't deserve their own tables (algorithm parameters, UserDefined,
metadata, contacts, bespoke data) are stored as JSON, so SQL can search them:

    SELECT doc_id FROM nereus.detection_set
    WHERE algorithm_parameters @> '{"Classifier": {"@name": "porpoise"}}';

    SELECT (user_defined->>'ICI_ms') FROM nereus.detection WHERE ...;

The mapping is plain and readable:

    <Threshold units="dB">12.5</Threshold>   {"Threshold": {"@units": "dB", "#text": 12.5}}
    <MinICI_ms>2</MinICI_ms>                  {"MinICI_ms": 2}
    <Call>A</Call><Call>B</Call>              {"Call": ["A", "B"]}
    <Parameters/>                             {"Parameters": ""}

plus three bookkeeping keys that make it exact, which queries can ignore:

    "#order"  child element names in document order (jsonb doesn't keep key
              order, and XML order can matter)
    "#ns"     namespace, only when it differs from the parent's (PAMGuard
              writes some settings with xmlns="")
    "#text"   text of an element that also has attributes or children

Text becomes a JSON number only when that reproduces it exactly ("2" -> 2,
but "2.0", "007" and "1e5" stay strings), so nothing is reformatted.

Anything this can't represent exactly (text between child elements) is
detected when converting, and the importer then also keeps the original XML.
"""

import copy
import re
from xml.sax.saxutils import escape, quoteattr

from lxml import etree

from .asa import format_num

# Element order the Tethys schema requires inside fixed-structure blocks. Used
# when JSON written directly (not imported) has no "#order".
SCHEMA_ORDER = {
    "Description": ["Objectives", "Abstract", "Method"],
    "QualityAssurance": ["Description", "ResponsibleParty"],
    "MetadataInfo": ["Contact", "Date", "UpdateFrequency"],
    "Contact": ["individualName", "organizationName", "positionName", "contactInfo"],
    "ResponsibleParty": ["individualName", "organizationName", "positionName", "contactInfo"],
    "contactInfo": ["phone", "address", "onlineResource", "hoursOfService", "contactInstructions"],
    "phone": ["voice", "facsimile"],
    "address": ["deliveryPoint", "city", "administrativeArea", "postalCode", "country",
                "electronicMailAddress"],
    "BespokeData": ["Abstract", "Data", "UserDefined"],
    "Data": ["URI", "Comment"],
    "SupportSoftware": ["Software", "Version", "Parameters"],
}

_NUM = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?$")


class NotRepresentable(ValueError):
    """The XML has a feature this JSON form can't hold exactly."""


def _scalar(s: str):
    """Text -> JSON number when that is exact, otherwise the string unchanged."""
    if _NUM.match(s):
        f = float(s)
        if format_num(f) == s:
            return int(f) if f.is_integer() and "." not in s and "e" not in s.lower() else f
    return s


def _text(v) -> str:
    if isinstance(v, bool):  # JSON true/false written directly by a client
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return format_num(float(v)) if isinstance(v, float) else str(v)
    return str(v)


def _is_blank(s: str | None) -> bool:
    return s is None or not s.strip()


# ------------------------------------------------------------ XML -> JSON
def to_json(el, parent_ns: str | None = None):
    """JSON value for element `el` (its name is the key, held by the caller)."""
    ns = etree.QName(el).namespace
    children = [c for c in el if isinstance(c.tag, str)]
    for c in el:
        if not _is_blank(c.tail):
            raise NotRepresentable(f"text between child elements of <{etree.QName(el).localname}>")
    text = None if _is_blank(el.text) else el.text
    if not children and not el.attrib and ns == parent_ns:
        return "" if text is None else _scalar(text)

    out = {}
    if ns != parent_ns:
        out["#ns"] = ns or ""
    for k, v in el.attrib.items():
        out["@" + k] = _scalar(v)  # namespaced attributes keep Clark notation {uri}name
    if text is not None:
        out["#text"] = _scalar(text)
    order, repeated = [], set()
    for c in children:
        name = etree.QName(c).localname  # can't clash with "@..."/"#..." keys
        v = to_json(c, ns)
        order.append(name)
        if name not in out:
            out[name] = v
        elif name in repeated:
            out[name].append(v)
        else:  # second occurrence: repeated elements become a list
            out[name] = [out[name], v]
            repeated.add(name)
    if len(set(order)) > 1:
        out["#order"] = order
    return out


def original_xml(el) -> str:
    """The element exactly as written. (deepcopy keeps only the namespace
    declarations it uses; not etree.cleanup_namespaces, which also deletes
    xmlns="" undeclarations.)"""
    el = copy.deepcopy(el)
    el.tail = None
    return etree.tostring(el, encoding="unicode")


def convert(el, parent_ns: str | None) -> tuple[object, str | None]:
    """(JSON value, original XML or None). The XML is only returned when the
    JSON can't reproduce the element exactly; the JSON is then a best effort
    for searching (or None when it can't be built at all)."""
    try:
        return block_to_json(el, parent_ns), None
    except NotRepresentable:
        try:
            value = to_json(el, parent_ns)
        except NotRepresentable:
            value = None
        return value, original_xml(el)


def block_to_json(el, parent_ns: str | None):
    """Convert an element and check the conversion is exact. Returns the JSON
    value, or raises NotRepresentable."""
    value = to_json(el, parent_ns)
    back = etree.fromstring(block_to_xml(etree.QName(el).localname, value, parent_ns))
    if not same_element(el, back):
        raise NotRepresentable(f"<{etree.QName(el).localname}> does not survive XML -> JSON -> XML")
    return value


# ------------------------------------------------------------ JSON -> XML
# Written by hand rather than with lxml: lxml silently drops xmlns="" on a
# no-namespace element built under a default namespace, which would move
# PAMGuard's settings into the Tethys namespace on the next read.
_PREFIXES = {"http://www.w3.org/2001/XMLSchema-instance": "xsi",
             "http://www.w3.org/XML/1998/namespace": "xml"}
_ROOT = object()  # "no parent": always declare the namespace


def block_to_xml(name: str, value, ns: str | None) -> str:
    """Standalone XML for element `name` (namespace `ns` unless the value says otherwise)."""
    out: list[str] = []
    _write(name, value, ns, _ROOT, out)
    return "".join(out)


def _write(name, value, ns, parent_ns, out):
    if isinstance(value, dict) and "#ns" in value:
        ns = value["#ns"] or None
    decls = {}
    if ns != parent_ns:
        decls["xmlns"] = ns or ""
    attrs, extra = [], {}
    if isinstance(value, dict):
        for k, v in value.items():
            if not k.startswith("@"):
                continue
            k = k[1:]
            if k.startswith("{"):
                uri, local = k[1:].split("}", 1)
                prefix = _PREFIXES.get(uri) or extra.setdefault(uri, f"a{len(extra)}")
                if prefix != "xml":
                    decls[f"xmlns:{prefix}"] = uri
                k = f"{prefix}:{local}"
            attrs.append(f" {k}={quoteattr(_text(v))}")
    head = name + "".join(f" {k}={quoteattr(v)}" for k, v in decls.items()) + "".join(attrs)

    if not isinstance(value, dict):
        text = "" if value in ("", None) else escape(_text(value))
        out.append(f"<{head}>{text}</{name}>" if text else f"<{head}/>")
        return
    out.append(f"<{head}>")
    if "#text" in value:
        out.append(escape(_text(value["#text"])))
    keys = [k for k in value if not k.startswith(("@", "#"))]
    expand = lambda ks: [k for k in ks for _ in (value[k] if isinstance(value[k], list) else [value[k]])]
    known = SCHEMA_ORDER.get(name)
    if known and set(keys) <= set(known):
        # A fixed-structure block: the schema decides the order, whatever the writer did.
        order = expand(sorted(keys, key=known.index))
    else:
        order = value.get("#order") or expand(keys)
    taken = {}
    for k in order:
        v = value[k]
        if isinstance(v, list):
            i = taken.get(k, 0)
            taken[k] = i + 1
            v = v[i]
        _write(k, v, ns, ns, out)
    out.append(f"</{name}>")


def from_python(value):
    """Plain Python dicts/lists (e.g. from a writer) -> the stored JSON form:
    adds "#order" from the dict's own key order so export keeps it."""
    if isinstance(value, dict):
        out = {k: from_python(v) for k, v in value.items()}
        names = [k for k in value if not k.startswith(("@", "#"))]
        if len(names) > 1 and "#order" not in value:
            out["#order"] = [k for k in names
                             for _ in (value[k] if isinstance(value[k], list) else [value[k]])]
        return out
    if isinstance(value, list):
        return [from_python(v) for v in value]
    return value


# ------------------------------------------------------------ comparison
def _norm(s):
    s = (s or "").strip()
    if _NUM.match(s):
        return float(s)
    return s


def same_element(a, b) -> bool:
    """Same tag, attributes, (non-blank) text and children, recursively."""
    if a.tag != b.tag:
        return False
    if {k: _norm(v) for k, v in a.attrib.items()} != {k: _norm(v) for k, v in b.attrib.items()}:
        return False
    if _norm(a.text) != _norm(b.text):
        return False
    ac = [c for c in a if isinstance(c.tag, str)]
    bc = [c for c in b if isinstance(c.tag, str)]
    return len(ac) == len(bc) and all(same_element(x, y) for x, y in zip(ac, bc))
