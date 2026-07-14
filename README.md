# Divine API - Documentation MCP Server

A public, read-only MCP server that answers "how do I use the DivineAPI REST API". It serves the already-public developer documentation: every endpoint, its parameters, its response fields, the global auth and error rules, and real captured example responses.

This server does NOT call the astrology APIs and needs NO credentials. It is a reference, not a data source. To actually run astrology requests, use the `divineapi` SDK or the data MCP servers:

- Indian / Vedic: `https://mcp.divineapi.com/indian/mcp`
- Western: `https://mcp.divineapi.com/western/mcp`
- Horoscope, Tarot and Numerology: `https://mcp.divineapi.com/horoscope/mcp`

## Public endpoint

```
https://mcp.divineapi.com/docs/mcp
```

No API key, no auth token, no OAuth. Just connect.

## Tools (5)

| Tool | What it does |
|------|--------------|
| `search_docs(query, limit=8)` | Case-insensitive keyword search across every endpoint card; returns the top matching cards (most query tokens first). |
| `get_endpoint(path)` | The full reference card for one exact path, or the closest match / a "did you mean..." list. |
| `list_endpoints(category="")` | Every `path  [host]` line, optionally filtered to a category (Indian, Western, Numerology, PDF, Horoscope, Lifestyle, Calculators). |
| `get_playbook()` | The global rules: auth, error semantics per host, birth params, horoscope selectors, field formats, house systems, and what not to do. |
| `get_example(path)` | A real captured example response body for the path (version-agnostic slug fallback), or a pointer to the card's `returns:` line. |

## Data sources

- `docs-pack.txt` is fetched live at startup from `https://developers.divineapi.com/docs-pack.txt` (10s timeout); on failure the server falls back to the bundled snapshot shipped in this repo.
- `examples.json` is a map of `normalized_path -> example response body`, generated from the published Postman collection by `build_examples.py`. Regenerate it with:

```bash
python3 build_examples.py "/path/to/DivineAPI_Collection_with_examples_Final Version.json"
```

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install ".[http]"

# stdio (for Claude Desktop, Cursor, etc.)
divineapi-docs-mcp

# HTTP (streamable-http on :8000)
MCP_TRANSPORT=http uvicorn server:app --host 0.0.0.0 --port 8000
```

## Deploy

Mirrors the DivineAPI data MCPs. The container runs `uvicorn server:app` on port 8000; docker-compose maps host port `8004 -> 8000`.

```bash
docker compose up -d --build
```

Documentation: https://developers.divineapi.com
