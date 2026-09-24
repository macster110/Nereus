"""Thin HTTP client for a Tethys 3.2 server, using only its public REST API.

Endpoints used (see Server/src/REST/Resources.py in Tethys):
  GET  /Tethys/ping, /Tethys/version, /Tethys/cache
  PUT  /Tethys/cache/on|off|clear      XQuery result cache control
  GET  /<Collection>                    HTML list of document names
  GET  /<Collection>?DocId=NAME         one document as XML
  POST /XQuery  XQuery=...              run an XQuery
  POST /XQuery  JSON=...                the select/return JSON the R and MATLAB clients send
  POST /<Collection>/import             upload a document (upload test)
  DELETE /<Collection>?DocId=a;b        remove the upload-test documents afterwards
"""

import json
import re
import time
from pathlib import Path

import requests

TETHYS_NS = "http://tethys.sdsu.edu/schema/1.0"
PROLOG = f'declare default element namespace "{TETHYS_NS}";\n'


class TethysError(RuntimeError):
    pass


class Tethys:
    def __init__(self, url: str = "http://localhost:9779", timeout: float = 1800):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.http = requests.Session()
        self.bytes_received = 0

    # ----------------------------------------------------------------- status
    def ping(self) -> bool:
        try:
            r = self.http.get(f"{self.url}/Tethys/ping", timeout=10)
            return r.ok and "alive" in r.text
        except requests.RequestException:
            return False

    def version(self) -> str:
        r = self.http.get(f"{self.url}/Tethys/version", timeout=10)
        m = re.search(r"<version>([^<]*)</version>", r.text)
        return m.group(1) if m else r.text.strip()

    def cache_enabled(self) -> bool | None:
        r = self.http.get(f"{self.url}/Tethys/cache", timeout=30)
        if not r.ok:
            return None
        return ">on<" in r.text or "true" in r.text.lower()

    def cache(self, op: str) -> None:
        """op: on | off | clear"""
        r = self.http.put(f"{self.url}/Tethys/cache/{op}", timeout=300)
        if not r.ok:
            raise TethysError(f"PUT /Tethys/cache/{op}: {r.status_code} {r.text[:300]}")

    # -------------------------------------------------------------- documents
    def list_documents(self, collection: str) -> list[str]:
        """Document names in a collection, parsed from the HTML listing."""
        r = self.http.get(f"{self.url}/{collection}", timeout=self.timeout)
        r.raise_for_status()
        names = re.findall(r"DocId=([^&\"']+)&(?:amp;)?format=XML", r.text)
        return sorted(set(requests.utils.unquote(n) for n in names))

    def get_document(self, collection: str, doc: str, dest: Path) -> int:
        """Stream one document to `dest`. Returns bytes written."""
        with self.http.get(f"{self.url}/{collection}", params={"DocId": doc},
                           stream=True, timeout=self.timeout) as r:
            if r.status_code != 200:
                raise TethysError(f"GET {collection}?DocId={doc}: {r.status_code} {r.text[:300]}")
            n = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    n += len(chunk)
        self.bytes_received += n
        return n

    # ---------------------------------------------------------------- queries
    def _post_xquery(self, data: dict) -> bytes:
        r = self.http.post(f"{self.url}/XQuery", data=data, timeout=self.timeout)
        if r.status_code != 200:
            raise TethysError(f"POST /XQuery: {r.status_code}\n{r.text[:2000]}")
        self.bytes_received += len(r.content)
        return r.content

    def xquery(self, query: str) -> bytes:
        """Run a raw XQuery (the Tethys-namespace prolog is prepended)."""
        return self._post_xquery({"XQuery": PROLOG + query})

    def json_query(self, spec: dict, plan: int = 0) -> bytes:
        """Run the select/return JSON query used by the R and MATLAB clients.
        plan=2 returns the XQuery Tethys generates instead of running it."""
        return self._post_xquery({"JSON": json.dumps(spec), "plan": str(plan)})

    def latin_names(self, tsns: list[int]) -> dict[int, str]:
        """ITIS TSN -> Latin completename, from Tethys's own ITIS tables."""
        names = {}
        for tsn in tsns:
            xml = self._post_xquery({"XQuery":
                'import module namespace lib="http://tethys.sdsu.edu/XQueryFns" at "Tethys.xq";\n'
                + PROLOG + f'<r>{{lib:tsn2completename(<SpeciesId>{int(tsn)}</SpeciesId>)/text()}}</r>'})
            m = re.search(rb"<r[^>]*>([^<]+)</r>", xml)
            if m:
                names[tsn] = m.group(1).decode().strip()
        return names

    # -------------------------------------------------------------- uploading
    def import_xml(self, collection: str, path: Path, overwrite: bool = True) -> str:
        """Upload one XML document through /<collection>/import, the route the
        Tethys web client and Java uploader use. The document name is the file
        stem. Tethys reports some failures inside a 200 response, so callers
        should confirm the upload by querying for it afterwards."""
        spec = (f"<import><docname>{path.stem}</docname>"
                f"<overwrite>{'true' if overwrite else 'false'}</overwrite>"
                f"<sources><source><type>file</type><name>{path.stem}</name>"
                f"<file>{path.name}</file></source></sources></import>")
        with open(path, "rb") as fh:
            r = self.http.post(f"{self.url}/{collection}/import",
                               data={"specification": spec},
                               files={path.name: (path.name, fh, "application/xml")},
                               timeout=self.timeout)
        self.bytes_received += len(r.content)
        if r.status_code != 200:
            raise TethysError(f"import {path.name}: {r.status_code} {r.text[:1000]}")
        return r.text

    def delete_documents(self, collection: str, docs: list[str]) -> None:
        r = self.http.delete(f"{self.url}/{collection}", params={"DocId": ";".join(docs)},
                             timeout=self.timeout)
        if r.status_code != 200:
            raise TethysError(f"DELETE {collection}: {r.status_code} {r.text[:300]}")


def timed(fn, *args, **kwargs):
    t = time.perf_counter()
    r = fn(*args, **kwargs)
    return time.perf_counter() - t, r
