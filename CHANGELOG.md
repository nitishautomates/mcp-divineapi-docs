# Changelog

All notable changes to the Divine API Documentation MCP Server.

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
