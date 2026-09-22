"""A stand-in for a Tethys server, for testing the comparison harness on
machines where Tethys can't run (macOS, Linux).

It serves XML documents from folders through the same REST endpoints the
harness uses, and runs XQuery with Saxon (saxonche). The JSON client route is
not implemented (returns 400), which also exercises the harness's error path.
Uploads (/<collection>/import) and DELETE are supported for the upload test.

Its timings mean nothing about Tethys. It only checks that the harness works
and that the XQuery is correct.

    python -m compare.mock_tethys --detections DIR --deployments DIR [--port 9779]
"""

import argparse
import re
import email.parser
import email.policy
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from saxonche import PySaxonProcessor

TETHYS_NS = "http://tethys.sdsu.edu/schema/1.0"


class Store:
    def __init__(self, dirs: dict[str, Path]):
        self.dirs = {k: v.resolve() for k, v in dirs.items()}
        self.docs = {k: {p.stem: p for p in sorted(v.glob("*.xml"))} for k, v in self.dirs.items()}
        self.lock = threading.Lock()
        self.proc = PySaxonProcessor(license=False)

    def xquery(self, query: str) -> bytes:
        # Map Tethys collection names onto folders Saxon can read.
        for name, d in self.dirs.items():
            uri = d.as_uri() + "?select=*.xml"
            query = query.replace(f'collection("{name}")', f'collection("{uri}")')
        with self.lock:  # one Saxon evaluation at a time keeps things simple
            xq = self.proc.new_xquery_processor()
            xq.set_query_content(query)
            out = xq.run_query_to_string()
            if out is None:
                raise RuntimeError(xq.error_message or "XQuery failed")
        return out.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    store: Store = None
    cache = "on"

    def _send(self, code: int, body: bytes | str, ctype="text/xml"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        parts = [p for p in u.path.split("/") if p]
        if parts[:2] == ["Tethys", "ping"]:
            return self._send(200, "<Tethys><ping>alive</ping></Tethys>")
        if parts[:2] == ["Tethys", "version"]:
            return self._send(200, "<Tethys><version>mock-3.2</version></Tethys>")
        if parts[:2] == ["Tethys", "cache"]:
            return self._send(200, f"<Tethys><cache>{Handler.cache}</cache></Tethys>")
        if len(parts) == 1 and parts[0] in self.store.docs:
            docs = self.store.docs[parts[0]]
            if "DocId" in qs:
                p = docs.get(qs["DocId"][0])
                if p is None:
                    return self._send(404, "Unable to find document")
                return self._send(200, p.read_bytes())
            items = "".join(f'<li><a href="?DocId={urllib.parse.quote(n)}&amp;format=HTML">{n}</a> '
                            f'or raw <a href="?DocId={urllib.parse.quote(n)}&amp;format=XML">XML</a></li>'
                            for n in docs)
            return self._send(200, f"<html><body><ol>{items}</ol></body></html>", "text/html")
        self._send(404, "not found")

    def do_PUT(self):
        parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        if parts[:2] == ["Tethys", "cache"] and len(parts) == 3:
            if parts[2] in ("on", "off"):
                Handler.cache = parts[2]
            return self._send(200, f"<Tethys><cache>{parts[2]}</cache></Tethys>")
        self._send(404, "not found")

    def do_DELETE(self):
        u = urllib.parse.urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        docs = self.store.docs.get(parts[0] if parts else "")
        if docs is None:
            return self._send(404, "not found")
        for name in urllib.parse.parse_qs(u.query).get("DocId", [""])[0].split(";"):
            p = docs.pop(name, None)
            if p is not None:
                p.unlink()
        return self._send(200, "<Deleted/>")

    def _import(self, collection: str, body: bytes):
        """POST /<collection>/import: multipart with a specification part and a file part."""
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
        spec, files = None, {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "specification":
                spec = part.get_content()
            else:
                files[name] = part.get_payload(decode=True)
        docname = re.search(r"<docname>([^<]+)</docname>", spec or "").group(1)
        (data,) = files.values()
        dest = self.store.dirs[collection] / f"{docname}.xml"
        dest.write_bytes(data)
        self.store.docs[collection][docname] = dest
        return self._send(200, f"<Import><Document name='{docname}'/></Import>")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 2 and parts[1] == "import" and parts[0] in self.store.docs:
            return self._import(parts[0], body)
        if path != "/XQuery":
            return self._send(404, "not found")
        form = urllib.parse.parse_qs(body.decode("utf-8"))
        if "XQuery" not in form:
            return self._send(400, "mock Tethys: only raw XQuery is supported, not JSON")
        query = form["XQuery"][0]
        # Tethys queries are XQuery 1.0 for Berkeley DB XML; Saxon is fine with them.
        try:
            return self._send(200, self.store.xquery(query))
        except Exception as e:
            return self._send(400, f"XQuery error: {e}\n-- Query --\n{query}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detections", required=True)
    ap.add_argument("--deployments", required=True)
    ap.add_argument("--port", type=int, default=9779)
    a = ap.parse_args()
    Handler.store = Store({"Detections": Path(a.detections), "Deployments": Path(a.deployments)})
    counts = {k: len(v) for k, v in Handler.store.docs.items()}
    print(f"mock Tethys on http://localhost:{a.port} serving {counts}")
    ThreadingHTTPServer(("localhost", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
