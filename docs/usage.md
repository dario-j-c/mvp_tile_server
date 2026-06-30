# Usage & API Reference

---

## CLI Reference

```bash
python -m app [config] [options]
uv run python -m app [config] [options]
```

| Argument | Default | Description |
|---|---|---|
| `config` | `config.json` | Path to tileset configuration JSON file |
| `-p`, `--port` | `8000` | Port to bind to |
| `-b`, `--bind` | `0.0.0.0` | Address to bind to |
| `--workers` | `4` | Number of Uvicorn worker processes |
| `--no-scan` | off | Skip directory scanning and tar index pre-building at startup (faster boot, default zoom bounds used) |
| `--event-mode` | off | Suppress access logs, minimise output; intended for live event operation |
| `--reload` | off | Auto-reload on code changes; development only, do not use at events |

### What happens at startup

1. **Config validation** — all paths are resolved and checked; any errors are printed and the process exits.
2. **Pre-scan / pre-build** (MAIN process, skipped with `--no-scan`) — two things happen here:
   - **Directory tilesets** are scanned once to determine tile count and zoom bounds. Results are written to a temp JSON file for workers to read.
   - **Tar tilesets** have their member indexes built and saved as `.idx` cache files alongside each tar (or in `TAR_CACHE_DIR` if set). Workers load the cache instead of re-parsing the archive.
3. **Workers start** — each worker:
   - Loads the tar index from the `.idx` cache written by MAIN (fast — just a pickle deserialise). If the cache is missing or older than the tar file, the worker parses the archive headers directly and saves a fresh cache. Each entry records the four values needed for serving: byte offset, size, mtime, and extension (`TileEntry`). The file is then memory-mapped (one `mmap` per tileset) so tile reads are direct in-memory slices — no per-request file open.
   - Metadata for tar tilesets (tile count, zoom levels, sample tiles) is derived from the index — no extra scan.
   - Directory metadata is read from the pre-scan temp file (or defaults if `--no-scan` was used).
4. **Server ready** — requests are accepted.

### Examples

```bash
# Minimal — reads config.json, binds 0.0.0.0:8000, 4 workers
uv run python -m app

# Custom config and port
uv run python -m app /data/tilesets.json -p 8080

# Event mode: 8 workers, no access logs, scan disabled for fast start
uv run python -m app config.json --workers 8 --event-mode --no-scan

# Development
uv run python -m app config.json --workers 1 --reload
```

---

## API Endpoints

All responses are JSON. Tile responses return binary image data with appropriate `Content-Type`.

Error responses follow a consistent format — see [Error Reference](#error-reference) below.

---

### `GET /health`

Health check. Always returns 200 while the process is alive.

**Response:**
```json
{
  "status": "healthy",
  "service": "multi-tileset-event-tile-server",
  "environment": "local-event"
}
```

---

### `GET /`

Server overview — lists all tilesets with aggregate metadata.

**Response:**
```json
{
  "service": "Multi-Tileset Event Tile Server",
  "version": "2.5.0-event",
  "tilesets": {
    "osm": {
      "source_type": "directory",
      "source_path": "/data/osm",
      "tile_count": 12450,
      "tile_count_complete": true,
      "zoom_levels": [10, 11, 12, 13],
      "sample_tiles": ["/osm/10/512/341.png"],
      "scanned_at": "2024-01-15T10:30:00.123456"
    }
  },
  "total_tiles": "12,450",
  "tile_url_format": "/{tileset_name}/{z}/{x}/{y.ext}"
}
```

**`tile_count_complete`** is `false` when the startup scan timed out before counting all tiles. The count is then a lower bound. Use `/admin/rescan/{name}` to retry.

---

### `GET /tilesets/{tileset_name}`

Detailed metadata for a single tileset.

**Path parameters:**
| Parameter | Description |
|---|---|
| `tileset_name` | Name as defined in config |

**Response:**
```json
{
  "name": "satellite",
  "source_type": "tar",
  "source_path": "/data/satellite.tar",
  "tile_count": "45,231",
  "tile_count_complete": true,
  "zoom_levels": [10, 11, 12, 13, 14],
  "zoom_range": "10-14",
  "sample_tiles": ["/satellite/10/512/341.png"],
  "tile_url_format": "/satellite/{z}/{x}/{y.ext}",
  "scanned_at": "2024-01-15T10:30:00.123456",
  "base_path": "tiles",
  "index_status": {
    "status": "ready",
    "tile_count": 45231,
    "zoom_levels": [10, 11, 12, 13, 14],
    "last_rebuilt": null
  }
}
```

`base_path` and `index_status` are only present for tar-based tilesets.

**Errors:**
| Code | Status | Condition |
|---|---|---|
| `TILESET_NOT_FOUND` | 404 | Name not in config |

---

### `GET /{tileset_name}/{z}/{x}/{y}`

Serve a tile. `y` includes the file extension (e.g. `341.png`).

**Path parameters:**
| Parameter | Description |
|---|---|
| `tileset_name` | Configured tileset name |
| `z` | Zoom level (integer) |
| `x` | X tile coordinate (integer) |
| `y` | Y filename with extension, e.g. `341.png` |

**Extension probing:** if the exact extension is not found, the server tries `.png`, `.jpg`, `.jpeg`, `.webp` in order. The first match is returned with the correct `Content-Type`.

**Caching:** successful responses include `ETag` and `Cache-Control: public, max-age=86400, immutable`. Clients can send `If-None-Match` to receive `304 Not Modified` when the tile hasn't changed.

**Response headers:**
```
Content-Type: image/png
Cache-Control: public, max-age=86400, immutable
ETag: W/"1705312200-4096"
Last-Modified: Mon, 15 Jan 2024 10:30:00 GMT
X-Tileset: osm
X-Source-Type: directory
X-Tile-Server: event-optimized
```

**Errors:**
| Code | Status | Condition |
|---|---|---|
| `TILESET_NOT_FOUND` | 404 | Tileset name not in config |
| `INVALID_ZOOM_LEVEL` | 404 | `z` outside the tileset's scanned zoom range |
| `INVALID_COORDINATE` | 404 | `x` or `y` value outside valid range for zoom level |
| `TILE_NOT_FOUND` | 404 | No tile file exists at these coordinates |
| `TILE_CORRUPTED` | 500 | File exists but cannot be read |
| `TAR_INDEX_UNAVAILABLE` | 503 | Tar index is being rebuilt; retry shortly |

---

### `POST /admin/rebuild/{tileset_name}`

**Tar tilesets only.** Re-reads the tar file from disk and rebuilds the in-memory index. Use this after replacing or modifying a tar file. The server continues to serve tiles from the old index until the rebuild completes.

**Path parameters:**
| Parameter | Description |
|---|---|
| `tileset_name` | Must be a tar-based tileset |

**Response:**
```json
{
  "status": "success",
  "message": "Index rebuilt successfully for tileset 'satellite'",
  "index_status": {
    "status": "ready",
    "tile_count": 45231,
    "zoom_levels": [10, 11, 12, 13, 14],
    "last_rebuilt": "2024-01-15T11:00:00.000000"
  }
}
```

**Errors:**
| Code | Status | Condition |
|---|---|---|
| `TILESET_NOT_FOUND` | 404 | Name not in config |
| — | 400 | Tileset is directory-based |
| — | 500 | Tar file unreadable or corrupted |

---

### `GET /admin/status/{tileset_name}`

**Tar tilesets only.** Returns the current state of the tar index.

**Path parameters:**
| Parameter | Description |
|---|---|
| `tileset_name` | Must be a tar-based tileset |

**Response:**
```json
{
  "tileset": "satellite",
  "status": "ready",
  "tile_count": 45231,
  "zoom_levels": [10, 11, 12, 13, 14],
  "last_rebuilt": "2024-01-15T11:00:00.000000"
}
```

`status` values:
| Value | Meaning |
|---|---|
| `ready` | Index built, serving normally |
| `rebuilding` | Rebuild in progress; tile requests may return 503 |
| `error` | Last build attempt failed; see `error` field |
| `not_initialized` | Tileset was never successfully initialized |

**Errors:**
| Code | Status | Condition |
|---|---|---|
| `TILESET_NOT_FOUND` | 404 | Name not in config |
| — | 400 | Tileset is directory-based |

---

### `POST /admin/rescan/{tileset_name}`

Refreshes the displayed metadata (tile count, zoom levels, sample tiles) for a tileset.

**Behaviour differs by source type:**

| Source | What happens |
|---|---|
| **Directory** | Walks the filesystem. Use after adding, removing, or swapping tile files in a directory tileset. |
| **Tar** | Reads from the in-memory index — O(1), no file I/O. The metadata already reflects the current index state. To pick up changes to the tar file on disk, call `/admin/rebuild` first. |

**Path parameters:**
| Parameter | Description |
|---|---|
| `tileset_name` | Any configured tileset |

**Response:**
```json
{
  "status": "success",
  "tileset": "osm",
  "tile_count": "12,450",
  "tile_count_complete": true,
  "zoom_levels": [10, 11, 12, 13],
  "scanned_at": "2024-01-15T11:05:00.123456"
}
```

**Errors:**
| Code | Status | Condition |
|---|---|---|
| `TILESET_NOT_FOUND` | 404 | Name not in config |
| — | 500 | Directory scan failed |

---

## Workflow Reference

### Updating a directory tileset

```
Add/remove/replace tile files on disk
  → POST /admin/rescan/{name}   (updates tile count, zoom bounds, sample tiles)
```

### Replacing a tar file

```
Write new tar file to disk
  → POST /admin/rebuild/{name}  (re-indexes from file, updates serving index + metadata)
  → POST /admin/rescan/{name}   (optional — metadata is already current after rebuild)
```

### Checking what the server knows

```
GET /                          → all tilesets, aggregate counts
GET /tilesets/{name}           → single tileset detail
GET /admin/status/{name}       → tar index state (ready/rebuilding/error)
```

---

## Error Reference

All `TileServerError` subclasses produce this response shape:

```json
{
  "error": "TILE_NOT_FOUND",
  "message": "Tile not found: /osm/10/512/341.png (tried extensions: .png, .jpg, .jpeg, .webp)",
  "path": "/osm/10/512/341.png"
}
```

| Error Code | HTTP | Description |
|---|---|---|
| `TILESET_NOT_FOUND` | 404 | Tileset name not in config. Message lists available names. |
| `INVALID_ZOOM_LEVEL` | 404 | Zoom outside scanned range. Message states valid range. |
| `INVALID_COORDINATE` | 404 | X or Y outside valid range for the zoom level. |
| `TILE_NOT_FOUND` | 404 | No tile at these coordinates (all extensions tried). |
| `TILE_CORRUPTED` | 500 | Tile exists but could not be read. Check disk/permissions. |
| `TAR_INDEX_UNAVAILABLE` | 503 | Index is rebuilding. Retry after a few seconds. |

Plain `HTTPException` responses (e.g. calling rebuild on a directory tileset) use FastAPI's default `{"detail": "..."}` format.

---

## Inspecting Tar Archives

Before configuring a tar archive, use the bundled utility to check its structure:

```bash
python inspect_tar.py /path/to/tiles.tar
python inspect_tar.py /path/to/tiles.tar --timeout 120 --max-members 5000
```

It reports compression type, detected zoom levels, file extensions, and generates the config snippet to paste directly into your config file.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CONFIG_PATH` | `config.json` | Path to the tileset config. Set automatically by MAIN; override when running Uvicorn directly. |
| `TILE_SCAN` | `1` | Set to `0` by MAIN so workers never re-scan. Set manually when running Uvicorn directly without `python -m app`. |
| `TILE_METADATA_FILE` | _(unset)_ | Path to the pre-scan JSON written by MAIN. Workers read it to get directory tileset metadata without rescanning. |
| `TAR_CACHE_DIR` | _(unset)_ | Directory where `.idx` cache files are written. Defaults to alongside the tar file (e.g. `tiles.tar.idx`), falling back to the OS temp directory if the tar's parent is not writable. |
