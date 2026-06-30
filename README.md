# MVP Tile Server

A high-performance FastAPI tile server for serving multiple tilesets from directory trees or tar archives, optimised for local event deployments.

## Overview

Designed to serve tiles to looping displays and interactive maps at events where the server runs locally and restarts are disruptive. Supports multiple independent tilesets, configurable at runtime without code changes.

**Tile sources:**
- **Tar archives** — the default choice. Tiles are served by seeking to a pre-indexed byte offset in a memory-mapped file — no per-request disk I/O, no thread-pool overhead. Use uncompressed `.tar`; compressed formats are rejected at startup.
- **Directories** — standard `z/x/y.ext` filesystem layout. Use when tiles need to be updated live without a server restart. Slower than tar due to per-request filesystem lookups.

**Detailed docs:**
- [Usage & API Reference](docs/usage.md) — all CLI options, every endpoint, request/response format
- [Troubleshooting](docs/troubleshooting.md) — startup failures, tar issues, tile errors, performance
- [Developer Guide](docs/dev-guide.md) — codebase walkthrough, how to add features, where bugs hide

---

## Installation

```bash
uv sync
```

---

## Quick Start

**1. Create a config file:**

```json
{
  "tilesets": {
    "osm": "/path/to/osm/tiles",
    "satellite": "/path/to/satellite.tar"
  }
}
```

**2. Start the server:**

```bash
uv run python -m app config.json
```

**3. Verify:**

```
GET http://localhost:8000/health
GET http://localhost:8000/
```

Tiles are served at `/{tileset_name}/{z}/{x}/{y.ext}`.

---

## Configuration

Config is a JSON file with a `tilesets` dict. Each entry is either a path string or a dict with `source` and optional `base_path`.

```json
{
  "tilesets": {
    "osm":       "/data/osm_tiles",
    "satellite": "/data/sat.tar",
    "topo": {
      "source":    "/data/topo.tar",
      "base_path": "tiles"
    }
  }
}
```

**Tileset name rules:** alphanumeric, hyphens and underscores allowed, cannot start with a digit.

**Tar `base_path`:** the path inside the archive where `z/x/y.ext` tiles begin. Omit it and the server auto-detects. Use `python inspect_tar.py /path/to/archive.tar` to inspect an archive before configuring it.

**Choosing a source type:**

| | Tar | Directory |
|---|---|---|
| Tile lookup | O(1) in-memory index | 1–5 filesystem stat calls |
| Extraction | mmap slice — no disk I/O | async file read from disk |
| Live tile updates | Requires `/admin/rebuild` | Instant — files read on demand |
| **Use when** | **Performance matters (events)** | **Tiles change without restarts** |

For event deployments, prefer tar. Use a directory only when you need to swap, add, or remove individual tile files while the server is running.

See `config/config_example.json` for a documented template.

---

## Project Structure

```
mvp_tile_server/
├── app/
│   ├── __main__.py         # CLI entry point (python -m app)
│   ├── main.py             # FastAPI app, routes, lifespan
│   ├── config.py           # Config loading, validation, directory scanning
│   ├── tar_manager.py      # Tar index management and tile extraction
│   ├── exceptions.py       # Custom exception classes and HTTP codes
│   └── utils.py            # Shared utilities (path parsing, media types)
├── config/
│   ├── config.json         # Active config (edit for your deployment)
│   └── config_example.json # Documented template
├── env/
│   ├── .env.example        # Environment variable template
│   └── .env                # Your settings (git-ignored)
├── docs/
│   ├── usage.md            # Full CLI and API reference
│   ├── troubleshooting.md  # Diagnosis and fixes
│   └── dev-guide.md        # Codebase walkthrough for contributors
├── tests/
│   ├── conftest.py
│   ├── test_integration.py
│   ├── test_unit.py
│   ├── test_property.py
│   └── test_tar_cache.py
├── docker-compose.yml
├── Dockerfile
└── inspect_tar.py          # Tar structure inspection utility
```

---

## Running the Tests

```bash
uv run pytest tests/ -v
```

---

## Docker

```bash
# 1. Copy and configure environment
cp env/.env.example env/.env

# 2. Edit config/config.json with your tileset paths (use /app/data/... container paths)

# 3. Start
docker-compose --env-file env/.env up
```

| Variable | Default | Description |
|---|---|---|
| `TILE_SERVER_PORT` | `8000` | Host port |
| `TILE_DATA_PATH` | `./test_data` | Host path mounted at `/app/data/` |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |

The container expects your config at `/app/config.json` (mapped from `config/config.json`) and tile data under `/app/data/` (mapped from `TILE_DATA_PATH`).

---

## Packing Tiles into a Tar

For anything performance-sensitive, pack your tiles into an uncompressed tar before deploying. Tar serving is faster than directory serving: tile lookup is an O(1) index read, and extraction is a direct in-memory slice with no filesystem calls per request.

```bash
tar -cf tiles.tar -C /path/to/tile/directory .
```

Compressed formats (`.tar.gz`, `.tar.bz2`, `.tar.xz`) are not supported — the server will refuse to start if one is configured.

To check a tar's structure before configuring it:

```bash
python inspect_tar.py /path/to/tiles.tar
```

---

## License

TBD (I'll figure this out later.)
