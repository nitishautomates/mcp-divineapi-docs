#!/usr/bin/env python3
"""
Regenerate examples.json for the DivineAPI docs MCP.

Reads the published Postman collection (the "with examples" file, whose captured
responses are real API output) and writes a compact map:

    { normalized_path: <first response body, parsed then re-dumped, truncated> }

normalized_path is the URL path after ".com" with the query string stripped, and
matches the path keys used in docs-pack.txt. This is the data behind the
get_example tool. Run it whenever the collection is refreshed.

Usage:
    python3 build_examples.py [path/to/collection.json]

The output is written next to this script (examples.json) and, if the package
directory exists alongside it, a synced copy is written there too so the
installed package ships the same data.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_COLLECTION = (
    "/Users/nitishguglani/computer-use/"
    "DivineAPI_Collection_with_examples_Final Version.json"
)
OUT = os.path.join(HERE, "examples.json")
PKG_OUT = os.path.join(HERE, "divineapi_docs_mcp", "examples.json")
MAX_CHARS = 4000


def norm_path(raw):
    """Collection URL -> bare endpoint path (strip host + query). Mirrors the
    pack compiler's _norm_path so example keys line up with docs-pack paths."""
    raw = (raw or "").split("?")[0]
    if ".com/" in raw:
        return "/" + raw.split(".com/", 1)[1].rstrip("/")
    return raw.rstrip("/")


def walk(items):
    """Yield every leaf request in the Postman item tree."""
    for x in items:
        if "item" in x:
            yield from walk(x["item"])
        elif "request" in x:
            yield x


def compact_body(body):
    """Parse a captured response body then re-dump it (canonical 2-space JSON),
    truncated to about MAX_CHARS. Non-JSON bodies are kept verbatim."""
    if not body:
        return ""
    try:
        parsed = json.loads(body)
        text = json.dumps(parsed, ensure_ascii=False, indent=2)
    except Exception:
        text = body
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rstrip() + "\n... [truncated]"
    return text


def build(collection_path):
    with open(collection_path, encoding="utf-8") as f:
        coll = json.load(f)

    out = {}
    for x in walk(coll.get("item", [])):
        u = (x.get("request") or {}).get("url", {})
        raw = u.get("raw", "") if isinstance(u, dict) else u
        path = norm_path(raw)
        if not path or path in out:
            continue
        resp = x.get("response") or []
        if not resp:
            continue
        body = compact_body(resp[0].get("body", ""))
        if body:
            out[path] = body

    payload = json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(payload)
    written = [OUT]
    if os.path.isdir(os.path.dirname(PKG_OUT)):
        with open(PKG_OUT, "w", encoding="utf-8") as f:
            f.write(payload)
        written.append(PKG_OUT)
    for w in written:
        print("wrote {} examples to {}".format(len(out), w))


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_COLLECTION
    if not os.path.exists(src):
        sys.exit("collection not found: {}".format(src))
    build(src)
