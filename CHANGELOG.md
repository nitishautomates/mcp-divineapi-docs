# Changelog

All notable changes to the Divine API Documentation MCP Server.

## [Unreleased]

Added the OAuth authentication stack, ported faithfully from the Western Astrology MCP so the docs server connects on claude.ai web via the "Add custom connector" OAuth flow, exactly like the Indian and Western data MCPs. Direct-credential clients (Claude Desktop, Cursor, etc.) are unchanged.

- OAuth (claude.ai web): `DivineOAuthProvider` maps the DivineAPI key + token to an issued JWT. Authorization-server metadata, `/authorize`, `/token`, `/register`, `/revoke`, and the `/divine-login` credential page are all served under `https://mcp.divineapi.com/docs/...` (issuer and resource server set to the `/docs` sibling path so the routes resolve under the existing `/docs/` nginx location on port 8004).
- Direct credentials still work through `ApiKeyToJwtMiddleware`, which mints the JWT the auth layer expects from `X-Divine-Api-Key` + `X-Divine-Auth-Token` headers or an `Authorization: Bearer <api_key>:<auth_token>` combo. This replaces the earlier `_DivineAuthMiddleware`.
- The `/mcp` endpoint now requires a valid credential (OAuth token or direct headers/combo); an unauthenticated request returns 401 with an OAuth challenge. The five documentation tools are unchanged.
- Credentials are validated live against DivineAPI, so only an ACTIVE key + token is accepted and fake or expired ones are rejected. `_validate_divine_creds` checks a cheap endpoint (daily-horoscope on astroapi-5) and caches the result per credential pair for 10 minutes. Both auth paths enforce it: the header/combo middleware returns 401 for an invalid pair instead of minting a JWT, and `/divine-login/submit` re-renders the login page with an "Invalid DivineAPI key or token" error instead of completing the OAuth flow. It fails closed on a definitive rejection and fails open on a transient DivineAPI outage so a blip does not lock everyone out. The env `DIVINE_API_KEY` / `DIVINE_AUTH_TOKEN` are no longer the gate.
- Added `pyjwt>=2.0.0` to dependencies. New `MCP_JWT_SECRET` environment variable signs the issued tokens; set it to a stable value in the deployment `.env` so tokens survive container restarts.

## [1.0.0]

Initial release.

- Public, read-only MCP server for the DivineAPI REST API documentation. No authentication: no API key, no auth token, no OAuth. It serves already-public developer docs, so anyone can connect at `https://mcp.divineapi.com/docs/mcp`.
- Five tools:
  - `search_docs(query, limit=8)` - keyword search across every endpoint card, ranked by how many query tokens each card matches.
  - `get_endpoint(path)` - the full reference card for one path, with closest-match and "did you mean..." fallbacks.
  - `list_endpoints(category="")` - all `path  [host]` lines, filterable by category.
  - `get_playbook()` - the global rules block (auth, error semantics, selectors, field formats, house systems, what not to do).
  - `get_example(path)` - a real captured example response, with a version-agnostic slug fallback.
- Data: `docs-pack.txt` is fetched live from `https://developers.divineapi.com/docs-pack.txt` at startup with a bundled-snapshot fallback; `examples.json` is generated from the published Postman collection by `build_examples.py`.
- Deploy shape mirrors the DivineAPI data MCPs (python:3.12-slim, `uvicorn server:app` on port 8000, host port 8004 in docker-compose).
