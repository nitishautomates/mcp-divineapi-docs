#!/usr/bin/env python3
"""
Divine API - Documentation MCP Server (read-only reference)

A read-only reference server that answers "how do I use the DivineAPI REST API".
It does NOT call the live astrology APIs; it serves the already-public developer
docs (endpoints, parameters, response fields, auth rules, error semantics,
selectors, field formats, house systems) plus real captured example responses.

Access is gated the same way the data MCP servers are, so this server connects on
claude.ai web via the "Add custom connector" OAuth flow, and on Claude Desktop /
Cursor / other clients via direct credentials. Two authentication paths are
supported in HTTP mode:
  - OAuth (claude.ai web): the DivineOAuthProvider maps the DivineAPI key + token
    to an issued JWT, discovered under https://mcp.divineapi.com/docs/...
  - Direct credentials: X-Divine-Api-Key + X-Divine-Auth-Token headers, or
    Authorization: Bearer <api_key>:<auth_token> (a colon combo for clients that
    cannot send custom headers). A pre-auth middleware mints the JWT the MCP auth
    layer expects, so both paths converge.

To actually run astrology requests, use the `divineapi` SDK or the data MCP
servers at mcp.divineapi.com/{indian,western,horoscope}/mcp.

Data sources:
  - docs-pack.txt : compact endpoint reference, fetched live at startup from
    https://developers.divineapi.com/docs-pack.txt with a bundled snapshot fallback.
  - examples.json : real captured example responses (built by build_examples.py).

Documentation: https://developers.divineapi.com
"""
import json
import os
import re
import secrets
import time

import httpx
import jwt
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

HERE = os.path.dirname(os.path.abspath(__file__))

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")
_MCP_HOST = os.environ.get("MCP_HOST", "mcp.divineapi.com")
_JWT_SECRET = os.environ.get("MCP_JWT_SECRET", secrets.token_hex(32))

_DOCS_PACK_URL = "https://developers.divineapi.com/docs-pack.txt"
_PACK_FILENAME = "docs-pack.txt"
_EXAMPLES_FILENAME = "examples.json"

# Pin the Host header in HTTP mode to defend against DNS-rebinding, mirroring the
# data MCPs.
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
# Live credential validation (active DivineAPI key + token)
# ──────────────────────────────────────────────

# A cheap DivineAPI endpoint used only to confirm a key + token are active.
_VALIDATE_URL = "https://astroapi-5.divineapi.com/api/v5/daily-horoscope"
_CREDS_CACHE_TTL = 600.0  # seconds to remember a validation result
_CREDS_CACHE: dict[tuple[str, str], tuple[float, bool]] = {}


async def _validate_divine_creds(api_key: str, auth_token: str) -> bool:
    """Return True if the DivineAPI key + token are active, False if definitively invalid.

    Checks against a cheap live endpoint (daily-horoscope on astroapi-5) and
    caches the result per (api_key, auth_token) for _CREDS_CACHE_TTL seconds so
    repeat MCP requests do not re-hit DivineAPI. DivineAPI legacy hosts answer
    HTTP 200 with success==3 for an auth failure, so valid == HTTP 200 AND
    success != 3 (a success of 1, or even a non-auth param error, means the
    credentials themselves were accepted).

    Fail-CLOSED on a definitive rejection (success==3, or HTTP 401/403). Fail-OPEN
    on a network error, timeout, or unexpected status (return True, uncached) so a
    transient DivineAPI outage does not lock out every client. Credential values
    are never logged.
    """
    if not api_key or not auth_token:
        return False
    now = time.time()
    ckey = (api_key, auth_token)
    cached = _CREDS_CACHE.get(ckey)
    if cached and cached[0] > now:
        return cached[1]

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _VALIDATE_URL,
                files={
                    "api_key": (None, api_key),
                    "sign": (None, "aries"),
                    "h_day": (None, "today"),
                    "tzone": (None, "5.5"),
                },
                headers={"Authorization": f"Bearer {auth_token}"},
                timeout=8.0,
            )
    except Exception:
        # Network error / timeout: fail-OPEN and do not cache, so the next request
        # re-checks once DivineAPI is reachable again.
        return True

    if resp.status_code in (401, 403):
        valid = False
    elif resp.status_code == 200:
        try:
            data = resp.json()
        except Exception:
            data = {}
        valid = not (isinstance(data, dict) and str(data.get("success")) == "3")
    else:
        # Unexpected status (5xx, 429, ...): treat as a transient blip, fail-OPEN
        # and do not cache.
        return True

    _CREDS_CACHE[ckey] = (now + _CREDS_CACHE_TTL, valid)
    return valid


# ──────────────────────────────────────────────
# OAuth Provider -maps OAuth Client ID/Secret to Divine API credentials
# ──────────────────────────────────────────────


class DivineOAuthProvider(OAuthAuthorizationServerProvider):
    """OAuth provider that uses Divine API Key as client_id and Auth Token as client_secret."""

    def __init__(self, jwt_secret: str):
        self._jwt_secret = jwt_secret
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, dict] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if client_id in self._clients:
            return self._clients[client_id]
        auto_client = OAuthClientInformationFull(
            client_id=client_id,
            client_secret=None,
            redirect_uris=['https://claude.ai/oauth/callback', 'https://app.claude.ai/oauth/callback', 'https://claude.ai/api/mcp/auth_callback', 'https://app.claude.ai/api/mcp/auth_callback'],
            grant_types=['authorization_code', 'refresh_token'],
            response_types=['code'],
            token_endpoint_auth_method='client_secret_post',
            scope='astrology',
        )
        self._clients[client_id] = auto_client
        return auto_client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        pending_id = secrets.token_urlsafe(16)
        self._pending_auths = getattr(self, "_pending_auths", {})
        self._pending_auths[pending_id] = {
            "client_id": client.client_id,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "scopes": params.scopes or [],
            "state": params.state,
        }
        return f"/divine-login?pending={pending_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        data = self._auth_codes.get(authorization_code)
        if not data or data["client_id"] != client.client_id:
            return None
        if time.time() > data["expires_at"]:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=data["expires_at"],
            client_id=data["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=AnyUrl(data["redirect_uri"]),
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"],
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        data = self._auth_codes.pop(authorization_code.code, None)
        if not data:
            raise TokenError(error="invalid_grant", error_description="Authorization code not found")

        payload = {
            "divine_api_key": data.get("divine_api_key", ""),
            "divine_auth_token": data.get("divine_auth_token", ""),
            "exp": int(time.time()) + 86400 * 30,
            "iat": int(time.time()),
        }
        access_token = jwt.encode(payload, self._jwt_secret, algorithm="HS256")

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=86400 * 30,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        return None

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        raise TokenError(error="unsupported_grant_type", error_description="Refresh tokens not supported")

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            payload = jwt.decode(token, self._jwt_secret, algorithms=["HS256"])
            return AccessToken(
                token=token,
                client_id=payload["divine_api_key"],
                scopes=[],
                expires_at=payload.get("exp"),
                resource=None,
            )
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        pass


# Build auth settings for HTTP mode. issuer_url and resource_server_url carry the
# public /docs path prefix so the OAuth routes resolve under the existing /docs/
# nginx location (which strips the prefix and proxies to this container on 8004).
_auth_settings = None
_auth_provider = None
if _TRANSPORT == "http":
    _auth_provider = DivineOAuthProvider(_JWT_SECRET)
    _auth_settings = AuthSettings(
        issuer_url=f"https://{_MCP_HOST}/docs",
        resource_server_url=f"https://{_MCP_HOST}/docs",
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["astrology"],
            default_scopes=["astrology"],
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[],
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
    auth=_auth_settings,
    auth_server_provider=_auth_provider,
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
# OAuth Login Form -/divine-login
# ──────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Divine API - Connect Your Account</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
               min-height: 100vh; display: flex; align-items: center; justify-content: center; }
        .card { background: #fff; border-radius: 16px; padding: 40px; max-width: 420px; width: 90%;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3); }
        .logo { text-align: center; margin-bottom: 24px; font-size: 28px; }
        h1 { font-size: 22px; color: #1a1a2e; margin-bottom: 8px; text-align: center; }
        p { color: #666; font-size: 14px; margin-bottom: 24px; text-align: center; }
        label { display: block; font-size: 13px; font-weight: 600; color: #333; margin-bottom: 6px; }
        input { width: 100%; padding: 12px; border: 2px solid #e0e0e0; border-radius: 8px;
                font-size: 14px; margin-bottom: 16px; transition: border-color 0.2s; }
        input:focus { outline: none; border-color: #0f3460; }
        button { width: 100%; padding: 14px; background: #0f3460; color: #fff; border: none;
                 border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer;
                 transition: background 0.2s; }
        button:hover { background: #1a1a2e; }
        .help { text-align: center; margin-top: 16px; font-size: 12px; color: #999; }
        .help a { color: #0f3460; }
        .err { background: #fdecea; color: #c0392b; border: 1px solid #f5c6cb; padding: 10px 12px;
               border-radius: 8px; font-size: 13px; margin-bottom: 16px; text-align: center; }
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">&#128302;</div>
        <h1>Connect Divine API</h1>
        <p>Enter your Divine API credentials to connect Divine API tools to Claude.</p>
        {error}
        <form method="POST" action="/divine-login/submit">
            <input type="hidden" name="pending" value="{pending_id}">
            <label>API Key</label>
            <input type="text" name="api_key" placeholder="Your Divine API Key" required>
            <label>Auth Token</label>
            <input type="password" name="auth_token" placeholder="Your Divine Auth Token" required>
            <button type="submit">Connect</button>
        </form>
        <p class="help">Get your credentials at <a href="https://divineapi.com/api-keys" target="_blank">divineapi.com/api-keys</a></p>
    </div>
<script>
});
</script>
</body>
</html>"""


if _TRANSPORT == "http":
    @mcp.custom_route("/divine-login", methods=["GET"])
    async def divine_login_form(request):
        from starlette.responses import HTMLResponse
        pending_id = request.query_params.get("pending", "")
        html = _LOGIN_HTML.replace("{pending_id}", pending_id).replace("{error}", "")
        return HTMLResponse(html)

    @mcp.custom_route("/divine-login/submit", methods=["POST"])
    async def divine_login_submit(request):
        from starlette.responses import HTMLResponse, RedirectResponse
        form = await request.form()
        pending_id = form.get("pending", "")
        api_key = form.get("api_key", "")
        auth_token = form.get("auth_token", "")

        if not _auth_provider or not hasattr(_auth_provider, "_pending_auths"):
            return HTMLResponse("Error: Invalid session", status_code=400)

        # Peek without consuming so an invalid attempt can be retried on the same page.
        pending = _auth_provider._pending_auths.get(pending_id)
        if not pending:
            return HTMLResponse("Error: Session expired. Please try connecting again.", status_code=400)

        # Reject fake / inactive DivineAPI credentials before completing the flow.
        if not await _validate_divine_creds(api_key, auth_token):
            error_html = '<p class="err">Invalid DivineAPI key or token. Check your credentials and try again.</p>'
            html = _LOGIN_HTML.replace("{pending_id}", pending_id).replace("{error}", error_html)
            return HTMLResponse(html, status_code=401)

        # Valid: consume the pending session now and complete the flow.
        pending = _auth_provider._pending_auths.pop(pending_id, None)
        if not pending:
            return HTMLResponse("Error: Session expired. Please try connecting again.", status_code=400)

        # Create auth code with Divine API credentials embedded
        code = secrets.token_urlsafe(32)
        _auth_provider._auth_codes[code] = {
            "client_id": pending["client_id"],
            "divine_api_key": api_key,
            "divine_auth_token": auth_token,
            "code_challenge": pending["code_challenge"],
            "redirect_uri": pending["redirect_uri"],
            "redirect_uri_provided_explicitly": pending["redirect_uri_provided_explicitly"],
            "scopes": pending["scopes"],
            "expires_at": time.time() + 300,
        }

        # Redirect back to Claude with the auth code
        redirect_url = construct_redirect_uri(
            pending["redirect_uri"],
            code=code,
            state=pending.get("state"),
        )
        return RedirectResponse(url=redirect_url, status_code=302)


# ──────────────────────────────────────────────
# HTTP / ASGI App
# ──────────────────────────────────────────────


class ApiKeyToJwtMiddleware:
    """ASGI middleware that converts direct DivineAPI credentials into the JWT
    Bearer token the MCP auth layer expects. Two client shapes are supported:

    1. X-Divine-Api-Key + X-Divine-Auth-Token headers (VS Code, OpenAI, Gemini,
       custom clients).
    2. Authorization: Bearer <api_key>:<auth_token> - a single-field credential
       combo for platforms that cannot send custom headers (e.g. the Claude
       Messages API MCP connector). A real OAuth-issued JWT never contains a
       colon, so valid tokens are never touched.
    """

    def __init__(self, app, jwt_secret):
        self.app = app
        self.jwt_secret = jwt_secret

    def _mint(self, api_key, auth_token):
        return jwt.encode(
            {
                "divine_api_key": api_key,
                "divine_auth_token": auth_token,
                "exp": int(time.time()) + 3600,
                "iat": int(time.time()),
            },
            self.jwt_secret,
            algorithm="HS256",
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers_list = scope.get("headers", [])
            headers_dict = {k: v for k, v in headers_list}
            api_key = headers_dict.get(b"x-divine-api-key", b"").decode()
            auth_token = headers_dict.get(b"x-divine-auth-token", b"").decode()
            bearer = ""
            for k, v in headers_list:
                if k == b"authorization" and v.startswith(b"Bearer "):
                    bearer = v[7:].decode()
                    break

            creds = None
            if api_key and auth_token and not bearer:
                creds = (api_key, auth_token)
            elif ":" in bearer:
                combo_key, _, combo_token = bearer.partition(":")
                if combo_key and combo_token:
                    creds = (combo_key.strip(), combo_token.strip())

            # A real OAuth-issued JWT bearer has no colon and no X-Divine headers,
            # so it falls through here untouched and is validated by the FastMCP
            # auth layer. Only the direct-credential shapes are checked live and
            # rejected before a JWT is minted.
            if creds:
                if not await _validate_divine_creds(creds[0], creds[1]):
                    body = (b'{"error":"unauthorized: invalid or inactive DivineAPI '
                            b'key or token"}')
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"content-type", b"application/json"),
                                            (b"www-authenticate", b"Bearer")]})
                    await send({"type": "http.response.body", "body": body})
                    return
                token = self._mint(creds[0], creds[1])
                new_headers = [(k, v) for k, v in headers_list if k != b"authorization"]
                new_headers.append((b"authorization", f"Bearer {token}".encode()))
                scope = dict(scope, headers=new_headers)

        await self.app(scope, receive, send)


def create_http_app():
    """Create the ASGI app for production HTTP deployment with uvicorn.

    OAuth (claude.ai web) is handled by the FastMCP auth layer; direct-credential
    clients are handled by ApiKeyToJwtMiddleware, which mints the JWT the auth
    layer expects from X-Divine-Api-Key + X-Divine-Auth-Token headers or an
    Authorization: Bearer <api_key>:<auth_token> combo. CORS is enabled for
    browser-based MCP clients."""
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
    return ApiKeyToJwtMiddleware(application, _JWT_SECRET)


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
