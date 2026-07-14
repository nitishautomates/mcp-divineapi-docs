#!/usr/bin/env python3
"""
Divine API - Documentation MCP Server (public, read-only)

A read-only reference server that answers "how do I use the DivineAPI REST API".
It does NOT call the live astrology APIs; it serves the already-public developer
docs (endpoints, parameters, response fields, auth rules, error semantics,
selectors, field formats, house systems) plus real captured example responses.

Because it exposes only public documentation, this server has NO authentication:
no API key, no auth token, no OAuth. To actually run astrology requests, use the
`divineapi` SDK or the data MCP servers at mcp.divineapi.com/{indian,western,horoscope}/mcp.

Data sources:
  - docs-pack.txt : compact endpoint reference, fetched live at startup from
    https://developers.divineapi.com/docs-pack.txt with a bundled snapshot fallback.
  - examples.json : real captured example responses (built by build_examples.py).

Documentation: https://developers.divineapi.com
"""
import json
import os
import re

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

HERE = os.path.dirname(os.path.abspath(__file__))

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
_MCP_HOST = os.environ.get("MCP_HOST", "mcp.divineapi.com")

_DOCS_PACK_URL = "https://developers.divineapi.com/docs-pack.txt"
_PACK_FILENAME = "docs-pack.txt"
_EXAMPLES_FILENAME = "examples.json"

# Public server: no auth machinery, but still pin the Host header in HTTP mode
# to defend against DNS-rebinding, mirroring the data MCPs (minus the auth args).
_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        _MCP_HOST,
        f"{_MCP_HOST}:*",
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
    ],
) if _TRANSPORT == "http" else None


# ──────────────────────────────────────────────
# Data loading (live-first, bundled-snapshot fallback)
# ──────────────────────────────────────────────

def _candidate_dirs():
    """Directories to search for the bundled data files, most-specific first."""
    dirs = [HERE, os.path.dirname(HERE), os.getcwd()]
    ordered = []
    for d in dirs:
        if d and d not in ordered:
            ordered.append(d)
    return ordered


def _find_data_file(name):
    for d in _candidate_dirs():
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


def _fetch_pack_text():
    """Fetch the published docs pack (10s timeout). On any failure, fall back to
    the bundled snapshot. Returns (text, source) where source is live|bundled|none."""
    try:
        resp = httpx.get(_DOCS_PACK_URL, timeout=10.0)
        resp.raise_for_status()
        text = resp.text
        if text and "## " in text:
            return text, "live"
    except Exception:
        pass
    path = _find_data_file(_PACK_FILENAME)
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read(), "bundled"
    return "", "none"


_SECTION_RE = re.compile(r"^## (.+?)\s*$")
_PATH_RE = re.compile(r"^/")
_HOST_RE = re.compile(r"\[([^\]]+)\]")


def _parse_pack(text):
    """Split the pack into the HEADER block (everything before the first '## '
    section) and a list of endpoint CARDS. Each card starts at a line matching
    '^/' (a path) and runs through its following indented lines up to the next
    path, section header, or blank line. Returns (header, cards, index, order)."""
    lines = text.splitlines()
    n = len(lines)

    header_lines = []
    i = 0
    while i < n and not _SECTION_RE.match(lines[i]):
        header_lines.append(lines[i])
        i += 1
    header = "\n".join(header_lines).strip("\n").rstrip()

    cards = []       # [{path, host, section, text}]
    index = {}       # path -> card text (first wins)
    order = []       # paths in file order
    current_section = ""

    while i < n:
        line = lines[i]
        m = _SECTION_RE.match(line)
        if m:
            current_section = m.group(1).strip()
            i += 1
            continue
        if _PATH_RE.match(line):
            path = line.split()[0].strip()
            host_m = _HOST_RE.search(line)
            host = host_m.group(1).strip() if host_m else ""
            block = [line]
            i += 1
            while i < n:
                nxt = lines[i]
                if nxt.strip() == "" or _PATH_RE.match(nxt) or _SECTION_RE.match(nxt):
                    break
                block.append(nxt)
                i += 1
            card_text = "\n".join(block).rstrip()
            cards.append({
                "path": path,
                "host": host,
                "section": current_section,
                "text": card_text,
            })
            if path not in index:
                index[path] = card_text
                order.append(path)
            continue
        i += 1

    return header, cards, index, order


def _load_examples():
    """Load the bundled example-response map (normalized_path -> body text)."""
    path = _find_data_file(_EXAMPLES_FILENAME)
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _slug(p):
    """Version-agnostic slug: strip '/vN/' version segments so an example captured
    at /api/v2/x still answers for /api/v3/x. Mirrors the pack compiler."""
    return re.sub(r"/v\d+/", "/", (p or "").rstrip("/"))


# Load everything once at import time.
_PACK_TEXT, _PACK_SOURCE = _fetch_pack_text()
_HEADER, _CARDS, _INDEX, _ORDER = _parse_pack(_PACK_TEXT)
_EXAMPLES = _load_examples()
_EXAMPLES_BY_SLUG = {}
for _k, _v in _EXAMPLES.items():
    _EXAMPLES_BY_SLUG.setdefault(_slug(_k), _v)


# ──────────────────────────────────────────────
# Core logic (plain functions; the tool wrappers below just call these)
# ──────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _search_docs(query, limit=8):
    tokens = list(dict.fromkeys(_TOKEN_RE.findall((query or "").lower())))
    if not tokens:
        return 'Provide a search query, e.g. "auspicious timings" or "natal chart".'
    scored = []
    for idx, card in enumerate(_CARDS):
        text_l = card["text"].lower()
        hits = sum(1 for t in tokens if t in text_l)
        if hits > 0:
            scored.append((hits, idx, card))
    if not scored:
        return (
            "No cards matched: {}. "
            "Try list_endpoints() to browse or broaden the query.".format(" ".join(tokens))
        )
    scored.sort(key=lambda s: (-s[0], s[1]))
    try:
        lim = max(1, int(limit))
    except (TypeError, ValueError):
        lim = 8
    top = scored[:lim]
    head = "{} of {} matching cards (query tokens: {}):".format(
        len(top), len(scored), ", ".join(tokens)
    )
    return head + "\n\n" + "\n\n".join(card["text"] for _, _, card in top)


def _get_endpoint(path):
    q = (path or "").strip()
    if not q:
        return "Provide an endpoint path, e.g. /indian-api/v1/auspicious-timings"
    if q in _INDEX:
        return _INDEX[q]
    q_slash = q if q.startswith("/") else "/" + q
    if q_slash in _INDEX:
        return _INDEX[q_slash]

    seg = q.rstrip("/").split("/")[-1].lower()
    candidates = [
        p for p in _ORDER
        if seg and seg in p.rstrip("/").split("/")[-1].lower()
    ]
    if not candidates:
        candidates = [p for p in _ORDER if q.lower() in p.lower()]

    if len(candidates) == 1:
        return _INDEX[candidates[0]]
    if candidates:
        shown = candidates[:25]
        listing = "\n".join("  " + p for p in shown)
        more = "" if len(candidates) <= 25 else "\n  ... (+{} more)".format(len(candidates) - 25)
        return "No exact match for '{}'. Did you mean:\n{}{}".format(q, listing, more)
    return (
        "No endpoint matches '{}'. Use list_endpoints() to browse or "
        "search_docs() to search by keyword.".format(q)
    )


def _list_endpoints(category=""):
    cat = (category or "").strip().lower()
    rows = []
    for card in _CARDS:
        if cat and cat not in card["section"].lower():
            continue
        rows.append("{}  [{}]".format(card["path"], card["host"]) if card["host"] else card["path"])
    if not rows:
        sections = list(dict.fromkeys(c["section"] for c in _CARDS if c["section"]))
        return (
            "No endpoints in a category matching '{}'.\n"
            "Available categories: {}".format(category, ", ".join(sections))
        )
    head = "{} endpoints{}:".format(
        len(rows), " in categories matching '{}'".format(category) if cat else ""
    )
    return head + "\n" + "\n".join(rows)


def _get_playbook():
    if not _HEADER:
        return "Playbook unavailable (docs pack could not be loaded)."
    return _HEADER


def _get_example(path):
    q = (path or "").strip()
    if not q:
        return "Provide an endpoint path, e.g. /indian-api/v1/auspicious-timings"
    q_slash = q if q.startswith("/") else "/" + q
    body = (
        _EXAMPLES.get(q_slash.rstrip("/"))
        or _EXAMPLES.get(q.rstrip("/"))
        or _EXAMPLES_BY_SLUG.get(_slug(q_slash))
        or _EXAMPLES_BY_SLUG.get(_slug(q))
    )
    if body:
        return body
    return (
        "No captured example for '{}'. See the card's returns: line via "
        'get_endpoint("{}") for the response field names, or the full body in '
        "openapi.yaml / the Postman collection.".format(q, q)
    )


# ──────────────────────────────────────────────
# Server initialization
# ──────────────────────────────────────────────

_SERVER_INSTRUCTIONS = """DivineAPI Docs: a READ-ONLY reference for the DivineAPI REST API. It answers how to use the API (endpoints, parameters, response fields, auth rules, error semantics, selectors, field formats, and house systems), sourced from the live docs pack at developers.divineapi.com.

Tools:
  search_docs(query, limit)  - keyword search across every endpoint reference card
  get_endpoint(path)         - the full reference card for one exact endpoint path
  list_endpoints(category)   - browse endpoint paths, optionally filtered by category
  get_playbook()             - the global rules: auth, error semantics, selectors, field formats, house systems, and what not to do
  get_example(path)          - a real captured example response body for an endpoint

This server does NOT call the astrology APIs, but access is gated: send your DivineAPI key + token as X-Divine-Api-Key + X-Divine-Auth-Token headers (or Authorization: Bearer <api_key>:<auth_token>), the same credentials the data MCPs use. To actually run a request, use the divineapi SDK (pip install divineapi / npm install divineapi / composer require divineapi/divineapi) or the data MCP servers at https://mcp.divineapi.com/indian/mcp, https://mcp.divineapi.com/western/mcp, and https://mcp.divineapi.com/horoscope/mcp."""

mcp = FastMCP(
    "divineapi_docs_mcp",
    instructions=_SERVER_INSTRUCTIONS,
    stateless_http=(_TRANSPORT == "http"),
    transport_security=_transport_security,
)


# ──────────────────────────────────────────────
# Tools (5 public, read-only)
# ──────────────────────────────────────────────

@mcp.tool()
def search_docs(query: str, limit: int = 8) -> str:
    """Search the DivineAPI docs by keyword and return the matching endpoint cards.

    Case-insensitive token scan over every endpoint reference card. Cards that
    match more of the query's tokens rank first. Returns up to `limit` whole
    cards (path, summary, params, returns). Use this when you know roughly what
    you want ("auspicious timings", "natal wheel", "love compatibility") but not
    the exact path.
    """
    return _search_docs(query, limit)


@mcp.tool()
def get_endpoint(path: str) -> str:
    """Return the full reference card for one endpoint path.

    Give an exact path such as /indian-api/v1/auspicious-timings. If the path is
    not an exact match, the closest single endpoint (substring on the last path
    segment) is returned, otherwise a short "did you mean..." list of candidates.
    The card lists the host, summary, params (with * for required and example
    values), and the top-level response fields (returns: line).
    """
    return _get_endpoint(path)


@mcp.tool()
def list_endpoints(category: str = "") -> str:
    """List endpoint paths with their hosts, optionally filtered by category.

    With no argument, lists every endpoint as "path  [host]" lines. Pass a
    category substring (case-insensitive) to filter, e.g. "Indian", "Western",
    "Numerology", "PDF", "Horoscope", "Lifestyle", or "Calculators".
    """
    return _list_endpoints(category)


@mcp.tool()
def get_playbook() -> str:
    """Return the global DivineAPI usage rules (the docs-pack header).

    Covers authentication (Bearer token + api_key form field), error semantics
    per host, the standard birth params, horoscope selectors, field formats,
    house systems, SDK installs, the MCP server URLs, and a "what not to do"
    list of common mistakes. Read this before constructing any request.
    """
    return _get_playbook()


@mcp.tool()
def get_example(path: str) -> str:
    """Return a real captured example response body for an endpoint path.

    Uses the bundled examples (with a version-agnostic slug fallback, so an
    example captured at /api/v2/x also answers for /api/v3/x). If no example was
    captured for the path, points you to the card's returns: line instead.
    """
    return _get_example(path)


# ──────────────────────────────────────────────
# HTTP app (module-level, for `uvicorn server:app`)
# ──────────────────────────────────────────────

import hmac


class _DivineAuthMiddleware:
    """Gate the server behind the DivineAPI key + token.

    Active only when both DIVINE_API_KEY and DIVINE_AUTH_TOKEN are set in the
    environment; with neither set the server stays open (unchanged local/stdio
    behavior). A request is allowed if it presents matching credentials as either
    the X-Divine-Api-Key + X-Divine-Auth-Token headers OR
    Authorization: Bearer <api_key>:<auth_token> (same shapes the data MCPs
    accept). Pure-ASGI so MCP streaming is not buffered; OPTIONS preflight passes
    through for CORS. Comparisons are constant-time."""

    def __init__(self, app, api_key, auth_token):
        self.app = app
        self.api_key = api_key
        self.auth_token = auth_token

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        if self._ok(headers):
            await self.app(scope, receive, send)
            return
        body = (b'{"error":"unauthorized: send X-Divine-Api-Key + '
                b'X-Divine-Auth-Token, or Authorization: Bearer '
                b'<api_key>:<auth_token>"}')
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"www-authenticate", b"Bearer")]})
        await send({"type": "http.response.body", "body": body})

    def _ok(self, headers):
        key = headers.get("x-divine-api-key")
        tok = headers.get("x-divine-auth-token")
        if not (key and tok):
            auth = headers.get("authorization", "")
            if auth[:7].lower() == "bearer " and ":" in auth[7:]:
                key, tok = auth[7:].strip().split(":", 1)
        if not (key and tok):
            return False
        return (hmac.compare_digest(key, self.api_key)
                and hmac.compare_digest(tok, self.auth_token))


def create_http_app():
    """Create the ASGI app for production HTTP deployment with uvicorn.

    When DIVINE_API_KEY + DIVINE_AUTH_TOKEN are set in the environment the server
    is gated behind those credentials (see _DivineAuthMiddleware); with neither
    set it stays open. CORS is enabled for browser-based MCP clients."""
    from starlette.middleware.cors import CORSMiddleware

    application = mcp.streamable_http_app()
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["mcp-protocol-version", "mcp-session-id", "Content-Type",
                       "Authorization", "X-Divine-Api-Key", "X-Divine-Auth-Token"],
        expose_headers=["mcp-session-id"],
    )
    api_key = os.environ.get("DIVINE_API_KEY", "")
    auth_token = os.environ.get("DIVINE_AUTH_TOKEN", "")
    if api_key and auth_token:
        return _DivineAuthMiddleware(application, api_key, auth_token)
    return application


# Module-level ASGI app for uvicorn (only created in HTTP mode).
app = create_http_app() if _TRANSPORT == "http" else None


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    if _TRANSPORT == "http":
        mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
    else:
        mcp.run(transport="stdio")
