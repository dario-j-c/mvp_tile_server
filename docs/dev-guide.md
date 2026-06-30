# Developer Guide

A handover document for developers new to this codebase. After reading this alongside the source files it references, you should be able to understand how requests flow through the system, make changes confidently, and know where to look when things break.

---

## Table of Contents

1. [What this server does](#what-this-server-does)
2. [How the code is organised](#how-the-code-is-organised)
3. [The startup sequence, step by step](#the-startup-sequence-step-by-step)
4. [How a tile request is served](#how-a-tile-request-is-served)
5. [Tar indexing in depth](#tar-indexing-in-depth)
6. [State and where it lives](#state-and-where-it-lives)
7. [Module reference](#module-reference)
8. [The test suite](#the-test-suite)
9. [How to make common changes](#how-to-make-common-changes)
10. [Where bugs typically hide](#where-bugs-typically-hide)
11. [Gotchas and invariants](#gotchas-and-invariants)

---

## What this server does

This is a FastAPI tile server. It reads pre-built map tile files and serves them over HTTP to mapping clients (e.g. Leaflet, OpenLayers, looping displays at events).

Tiles are addressed by three integers — zoom level `z`, column `x`, row `y` — and fetched at URLs like `/osm/10/512/341.png`. The server supports two storage backends:

- **Tar tilesets** — the default for event deployments. All tiles are packed into a single uncompressed `.tar` file. At startup each worker loads a pre-built in-memory index (one `TileEntry` per tile recording the byte offset, size, mtime, and extension) from a `.idx` cache file written by the MAIN process, then memory-maps the file. Tile requests are served by a single in-memory slice — no filesystem calls at request time.
- **Directory tilesets** — tiles stored as files in a `z/x/y.ext` directory tree. Each request requires 1–5 `stat` calls for extension probing plus an async file read. Use this when tiles need to be updated on disk without a server restart; otherwise prefer tar.

Multiple independent tilesets can be configured simultaneously, mixing types. The server is tuned for local deployments (events, installations) where restarts are disruptive.

---

## How the code is organised

```
app/
├── __main__.py     CLI entry point; MAIN process only
├── main.py         FastAPI app factory, all routes, lifespan
├── config.py       Config loading, validation, directory scanning
├── tar_manager.py  Tar indexing and per-request tile extraction
├── exceptions.py   All custom error classes
└── utils.py        Shared helpers (path parsing, media types, tar detection)

tests/
├── conftest.py         Shared fixtures, test data setup
├── test_integration.py API endpoint tests via TestClient
├── test_unit.py        Unit tests for individual functions/classes
└── test_property.py    Hypothesis property-based tests

config/
├── config.json         Active deployment config (edit for your environment)
└── config_example.json Documented template

docs/
├── usage.md            CLI and API reference (operators and users)
├── troubleshooting.md  Diagnosis and fixes for known problems
└── dev-guide.md        This file
```

The `app/` package is where everything interesting happens. The four files you'll touch most often are `main.py` (routes), `tar_manager.py` (tar I/O), `config.py` (startup scanning), and `exceptions.py` (error handling).

---

## The startup sequence, step by step

Understanding startup is essential because several subtle design decisions were made to handle the multi-worker model correctly.

### Two kinds of processes: MAIN and workers

When you run `python -m app config.json --workers 4`, two distinct roles exist:

1. **MAIN process** (`app/__main__.py`) — the Python process you launched. It validates the config, pre-scans directory tilesets once, writes the results to a temp JSON file, then starts Uvicorn. After that it hands off control and becomes the process supervisor.

2. **Worker processes** (Uvicorn forks) — each worker calls `get_app()` in `app/main.py`, which calls `create_app(...)` which runs the FastAPI `lifespan` context. Workers do the actual request serving; MAIN does not serve requests.

### Why pre-process in MAIN instead of each worker?

MAIN does two things before forking workers, both to avoid redundant work:

**Directory tilesets:** walking a large filesystem takes 30–120 seconds. With 4 workers, doing it 4 times wastes 3x that time. MAIN scans once, writes the result to a temp JSON file, sets `TILE_METADATA_FILE`, and workers read it instead.

**Tar tilesets:** MAIN builds a unified index for each unique tar archive (reads all member headers; no tile data) and saves it as a pickled `.idx` file alongside the tar (or in `TAR_CACHE_DIR`). Workers load that file via `load_or_build_tar_index()` rather than re-parsing the archive headers. For large archives (~1 million tiles, 15–60 seconds to parse) this is a material saving per worker. If the cache is missing or older than the tar, a worker falls back to parsing the archive directly and writes a fresh cache.

### The full sequence

```
python -m app config.json              (MAIN process starts)
  │
  ├─ load_tileset_config()             Validate JSON, resolve paths, detect tar vs dir
  │
  ├─ [if not --no-scan]
  │    ├─ [if directory tilesets]
  │    │    scan_all_tilesets()        Walk filesystem, count tiles, find zoom bounds
  │    │    write to tempfile          JSON blob, path stored in TILE_METADATA_FILE env var
  │    │
  │    └─ [if tar tilesets]
  │         load_or_build_tar_index()  Build unified index for each unique tar;
  │                                    save as .idx alongside tar (or in TAR_CACHE_DIR)
  │
  ├─ set env vars:
  │    CONFIG_PATH, TILE_SCAN=0, TILE_METADATA_FILE
  │
  └─ uvicorn.run(...)                  Forks N worker processes
       │
       └─ (each worker) get_app()      Reads CONFIG_PATH, TILE_SCAN, TILE_METADATA_FILE
            └─ create_app(...)
                 └─ lifespan()
                      ├─ [for each tar tileset]
                      │    tar_manager.initialize_tileset()
                      │      └─ build_tar_index()
                      │           └─ load_or_build_tar_index()  Load .idx cache (fast) or
                      │                                          parse tar headers (fallback)
                      │           └─ filter_index_for_tileset() Apply base_path, build
                      │                                          z/x/y.ext → TileEntry map
                      │    Store metadata in tileset_metadata[name]
                      │
                      ├─ [if pre-scanned metadata file exists]
                      │    tileset_metadata.update(pre_scanned_metadata)
                      │    (directory metadata merged in, tar metadata already present)
                      │
                      ├─ [else if do_scan and directory tilesets]
                      │    asyncio.to_thread(scan_tiles, ...)  per tileset
                      │
                      ├─ app.state.tilesets = tilesets         (config, never changes)
                      ├─ app.state.tileset_metadata = ...      (can be updated by admin endpoints)
                      └─ app.state.tar_manager = tar_manager   (indexes live here)
```

After `lifespan` yields, the server is ready. On shutdown, `close_all()` closes all mmap views and their underlying file handles.

---

## How a tile request is served

All tile serving goes through the single route handler in `app/main.py`:

```
GET /{tileset_name}/{z}/{x}/{y:path}   →   get_tile(tileset_name, z, x, y, request)
```

The handler validates in order, short-circuiting on the first error:

1. **Tileset exists** — `tileset_name in app.state.tilesets` → 404 `TILESET_NOT_FOUND`
2. **Zoom in range** — `min_zoom <= z <= max_zoom` → 404 `INVALID_ZOOM_LEVEL`
3. **X in range** — `0 <= x < 2^z` → 404 `INVALID_COORDINATE`
4. **Y sanitised** — `Path(y).name == y` (no directory traversal) → 400
5. **Y in range** — if `y` stem is numeric, same bounds check as X → 404 `INVALID_COORDINATE`

Then it branches by source type:

**Directory:** calls `find_tile_path(base_dir, z, x, y_name)` in a thread (blocking I/O off the event loop), returns a `FileResponse`. Extension probing happens inside `find_tile_path`: tries the requested extension first, then `.png`, `.jpg`, `.jpeg`, `.webp`.

**Tar:** calls `tar_manager.get_tile_from_tar(...)` which looks up the tile in the in-memory index and slices the bytes from the memory-mapped file. For 304 caching, ETag is `W/"mtime-size"` from the `TileEntry`; the slice is a synchronous in-memory read with no thread-pool dispatch.

Both paths set `Cache-Control: public, max-age=86400, immutable` and honour `If-None-Match`.

---

## Tar indexing in depth

This is the most non-obvious part of the system and the most performance-sensitive.

### What the index is

`build_unified_tar_index()` opens the tar file and iterates every member, building a dict keyed by the raw archive path:

```
"10/512/341.png"  →  TileEntry(offset=8192, size=4096, mtime=1705312200.0, suffix=".png")
```

`TileEntry` is a `NamedTuple` of four scalars — the only values needed for serving. `offset` is the absolute byte position in the raw file where the tile's data begins (`TarInfo.offset_data`); `size` is how many bytes to read.

Full `TarInfo` objects are not kept: they hold uid, gid, linkname, and dozens of other fields that are irrelevant after indexing and would multiply RAM usage significantly for large tilesets.

`filter_index_for_tileset()` then takes that unified index and builds the tile-serving view: strips the `base_path` prefix (if any), parses each remaining path as `z/x/y.ext` using `parse_tile_member_path`, and returns `(member_index, zoom_levels_set, sample_tiles_list)`. Non-tile members are silently skipped.

### The cache layer

Iterating tar headers is proportional to the number of files (~15–60 seconds for a million tiles on SSD). With multiple workers, each would parse the same archive redundantly. Instead:

1. MAIN calls `load_or_build_tar_index()` once per unique tar path.
2. `load_or_build_tar_index()` calls `build_unified_tar_index()` and pickles the result to a `.idx` file alongside the tar (e.g. `tiles.tar.idx`). Set `TAR_CACHE_DIR` to write caches elsewhere.
3. Workers call `build_tar_index()` → `load_or_build_tar_index()`, which reads and unpickles the `.idx` file. Cache validity is determined by mtime comparison: if the `.idx` is newer than the tar, it's valid.
4. On `POST /admin/rebuild`, `force_rebuild=True` bypasses the cache read, re-parses the archive, and overwrites the `.idx` file.

### How per-request extraction works

At startup, after building the index, the tar file is memory-mapped:

```python
fh = open(source_path, "rb")
mmap_obj = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
```

One file descriptor and one mmap are kept per tileset per worker. When a tile is requested:

```python
tile_data = mmap_obj[tile_entry.offset : tile_entry.offset + tile_entry.size]
```

This is a synchronous in-memory slice. The OS handles paging — only the pages that contain the requested tile are loaded from disk. There is no per-request file open, no seek syscall, no lock, and no thread-pool dispatch. Concurrent requests are safe because mmap reads are read-only and independent.

**Only uncompressed `.tar` files are supported.** For uncompressed tars, `offset_data` is an absolute position in the raw file on disk, so the mmap slice is O(1). For compressed formats, `offset_data` is a position in the decompressed stream and doesn't correspond to any raw byte position — the only way to reach a member would be to decompress sequentially from the beginning. The server rejects compressed archives at startup.

### Rebuild without restart

`POST /admin/rebuild/{name}` lets you swap a tar file on disk and reload the index without restarting. The handler calls `tar_manager.rebuild_index()`, which:

1. Sets `index_status[name]["status"] = "rebuilding"` — tile requests during this window return 503
2. Calls `build_tar_index()` in a thread — reads new tar headers
3. Opens a new file handle and mmap for the new file
4. Atomically swaps the index and mmap — now serving from new file
5. Closes the old mmap and file handle
6. Updates `index_status[name]["status"] = "ready"`

There is no await between step 4 and step 5, so no other coroutine can observe the half-replaced state. The rebuild lock (`self.rebuild_lock`) prevents two rebuilds running concurrently.

---

## State and where it lives

All per-request-cycle state lives on `app.state`, which FastAPI makes available through `request.app.state`. There are three attributes:

| Attribute | Type | Contents | Mutated by |
|---|---|---|---|
| `app.state.tilesets` | `Dict[str, TilesetConfig]` | Config loaded at startup; source type, path, base_path | Never after startup |
| `app.state.tileset_metadata` | `Dict[str, dict]` | Tile counts, zoom bounds, sample tiles, scanned_at | `/admin/rescan/{name}`, `/admin/rebuild/{name}` (indirectly via tar_manager) |
| `app.state.tar_manager` | `TarManager` | In-memory tar indexes, index status, source paths | `/admin/rebuild/{name}` |

`TarManager` itself holds:

| Attribute | Contents |
|---|---|
| `tar_indexes` | `{tileset_name: {"z/x/y.ext": TileEntry, ...}}` |
| `mmaps` | `{tileset_name: mmap.mmap}` — one memory-mapped view per tileset |
| `_mmap_files` | `{tileset_name: IO[bytes]}` — underlying file handles kept open for the mmap |
| `index_status` | `{tileset_name: {"status": "ready", "tile_count": N, "zoom_levels": [...], ...}}` |
| `rebuild_lock` | `asyncio.Lock` — prevents concurrent rebuilds |

**Important:** `app.state` is per-worker-process. Workers do not share memory. A rebuild or rescan on one worker does not affect other workers. This is expected: Uvicorn's multi-worker model uses OS processes, not threads.

---

## Module reference

### `app/__main__.py`

Entry point when you run `python -m app`. Responsible for the MAIN process only:
- Parsing CLI arguments
- Calling `load_tileset_config()` to validate early
- Pre-scanning directory tilesets and writing metadata to a temp file
- Setting environment variables that workers read
- Calling `uvicorn.run()`

Nothing here is reachable from tests directly. Tests bypass `__main__` entirely and call `create_app()` directly.

### `app/main.py`

The FastAPI app. Contains:
- `create_app(config_path, do_scan, metadata_file)` — builds and returns the `FastAPI` instance. This is what tests call.
- `get_app()` — reads environment variables and calls `create_app()`; this is what Uvicorn's `--factory` mode calls.
- `lifespan()` — async context manager that runs on startup/shutdown. Initialises tar indexes and directory metadata.
- All route handlers: `health_check`, `root`, `get_tileset_info`, `rebuild_tar_index`, `rescan_tileset`, `get_tar_status`, `get_tile`.

The exception handler for `TileServerError` (registered via `@app.exception_handler`) is also here. It converts the custom exception classes into consistent JSON responses.

### `app/config.py`

Two public responsibilities:

**`load_tileset_config(config_path, show_warnings)`** — reads the JSON config, validates every tileset, resolves paths, auto-detects `base_path` for tars. Collects all errors and raises a single `ValueError` listing them all (so operators see everything wrong at once, not just the first problem). Returns a `Dict[str, TilesetConfig]`.

**`scan_tiles(source_path, source_type, base_path, max_samples, timeout_seconds)`** — walks a directory or tar to count tiles, collect sample paths, and discover zoom levels. Returns a 6-tuple: `(tile_count, sample_tiles, zoom_levels_sorted, min_zoom, max_zoom, scan_complete)`. `scan_complete` is `False` if the timeout triggered before finishing. Called from the lifespan, from `rescan_tileset`, and from `scan_all_tilesets`.

**`scan_all_tilesets(tilesets)`** — wraps `scan_tiles` in a loop, used by MAIN to pre-scan directory tilesets. Returns a dict of metadata keyed by tileset name.

### `app/tar_manager.py`

**`get_tar_cache_path(tar_path)`** — returns the path where the `.idx` cache file will be written. Checks `TAR_CACHE_DIR` env var first, then tries alongside the tar file, then falls back to the OS temp directory.

**`build_unified_tar_index(tar_path)`** — opens the tar and builds a raw index of every file member, keyed by the member's full archive path. This is the slow step (one header read per member); subsequent calls load from cache instead.

**`filter_index_for_tileset(unified_index, base_path)`** — extracts the tile-serving view from the unified index: strips the `base_path` prefix, parses each remaining path as `z/x/y.ext`, and returns `(member_index, zoom_levels_set, sample_tiles_list)`. This is where base_path stripping and tile-key building happen.

**`load_or_build_tar_index(tar_path, force_rebuild)`** — cache layer. Loads the pickled unified index from the `.idx` file if it exists and is newer than the tar. Otherwise calls `build_unified_tar_index()` and saves the result. `force_rebuild=True` bypasses the cache read and overwrites the cache file.

**`build_tar_index(tar_path, base_path, force_rebuild)`** — thin wrapper: calls `load_or_build_tar_index` then `filter_index_for_tileset`. This is the entry point used by `TarManager`.

**`TarManager`** — the class that owns all tar state for a worker.
- `initialize_tileset()` — called once per tar tileset at startup; calls `build_tar_index` (which loads from cache), opens the file, creates the mmap, and returns `(tile_count, sample_tiles, zoom_levels)`.
- `rebuild_index()` — called by the rebuild admin endpoint; calls `build_tar_index` with `force_rebuild=True` (bypasses and rewrites the cache), builds a new mmap, swaps them atomically, then closes the old pair.
- `get_tile_from_tar()` — called per tile request; looks up the `TileEntry` in the index and slices the bytes from the mmap.
- `close_all()` — closes all mmap views and underlying file handles; called on worker shutdown.

### `app/exceptions.py`

Seven exception classes, all inheriting from `TileServerError`:

| Class | HTTP | Error code |
|---|---|---|
| `TilesetNotFoundError` | 404 | `TILESET_NOT_FOUND` |
| `InvalidZoomLevelError` | 404 | `INVALID_ZOOM_LEVEL` |
| `InvalidCoordinateError` | 404 | `INVALID_COORDINATE` |
| `TileNotFoundError` | 404 | `TILE_NOT_FOUND` |
| `TileCorruptedError` | 500 | `TILE_CORRUPTED` |
| `TarIndexUnavailableError` | 503 | `TAR_INDEX_UNAVAILABLE` |

All are caught by the `TileServerError` exception handler in `main.py`, which formats them as `{"error": "...", "message": "...", "path": "..."}`.

When you add a new error type, inherit from `TileServerError`, set `status_code` and `error_code` as class attributes, and the handler picks it up automatically.

### `app/utils.py`

Small, pure functions shared across modules:

- `is_tar_file(path)` — extension-based check; does not open the file
- `detect_tar_compression(tar_path)` — returns `"uncompressed"`, `"gzip"`, `"bzip2"`, `"xz"`, or `"unknown"` from the extension
- `parse_tile_member_path(member_path)` — given a string like `"data/tiles/10/512/341.png"`, returns `("10", "512", "341.png")` or `None`. Used in both `filter_index_for_tileset` and `scan_tiles`.
- `find_tile_in_tar_index(tar_index, z, x, y_name)` — looks up a tile in the index, probing alternate extensions if the exact one isn't found
- `find_tile_path(base_dir, z, x, y_name)` — same probing logic but for filesystem directories
- `media_type_for_suffix(suffix)` — `.png` → `"image/png"`, etc.

---

## The test suite

Run all tests with:

```bash
uv run pytest tests/ -v
```

### Three test files

**`tests/conftest.py`** — shared fixtures. The `client` fixture creates a `TestClient` backed by a real `FastAPI` app using actual test data in `test_data/`. The `setup_test_config` session fixture writes `test_data/test_config.json` with resolved absolute paths before any test runs.

**`tests/test_integration.py`** — API-level tests. Makes HTTP requests through `TestClient` and asserts on response codes, headers, and JSON bodies. These are the most useful tests for catching regressions when you change routes or response shapes.

**`tests/test_unit.py`** — unit-level tests for `build_tar_index`, `TarManager`, `scan_tiles`, `load_tileset_config`, coordinate parsing, and all the utility functions.

**`tests/test_property.py`** — Hypothesis-driven property tests. Generates random inputs to verify that functions don't crash unexpectedly and that certain invariants hold.

**`tests/test_tar_cache.py`** — tests for the tar index caching layer: `get_tar_cache_path` (default path, `TAR_CACHE_DIR` env var, read-only fallback), `load_or_build_tar_index` (cache hit, cache invalidation by mtime), and `filter_index_for_tileset` (no base_path, base_path prefix stripping, multi-prefix isolation).

### Test data

The `test_data/` directory contains pre-built fixtures:
- `directory_tiles/` — tiles in `z/x/y.png` layout
- `directory_tiles_2/` — a second directory tileset for multi-tileset tests
- `tiles_uncompressed.tar` — uncompressed tar
- `tiles_nested.tar` — tar with tiles nested under `map_data/tiles/`

If you add a new tileset type or new structural variant, add fixture data here and update `conftest.py`.

### Tips for writing tests

- Use the `client` fixture for integration tests; it has full startup including tar indexing.
- Use `client_no_scan` for tests where you want default zoom bounds without scanning.
- Call `create_app(config_path=str(TEST_CONFIG_PATH), do_scan=False)` directly if you need a fresh app instance with custom setup.
- `TarManager` can be instantiated directly for unit tests; call `asyncio.run(manager.initialize_tileset(...))` to set it up.

---

## How to make common changes

### Add a new CLI flag

1. Add `parser.add_argument(...)` in `parse_arguments()` in `__main__.py`.
2. If the flag controls worker behaviour, translate it to an env var (like `TILE_SCAN` or `TILE_METADATA_FILE`) and set it in `main()` before `uvicorn.run()`.
3. Read the env var in `get_app()` and pass it to `create_app()`.
4. Add the parameter to `create_app()`.

### Add a new API endpoint

1. Add the handler inside `create_app()` in `main.py` using `@app.get(...)` or `@app.post(...)`.
2. Access tileset config via `request.app.state.tilesets` and metadata via `request.app.state.tileset_metadata`.
3. Raise `TilesetNotFoundError(name, available)` for unknown tilesets — the exception handler formats the response automatically.
4. Add integration tests in `test_integration.py`.

### Add a new error type

1. Create a subclass of `TileServerError` in `exceptions.py`.
2. Set `status_code` (int) and `error_code` (string constant) as class attributes.
3. Set a descriptive `message` in `__init__`.
4. The existing exception handler in `main.py` will handle it automatically — no changes needed there.

### Add a new tileset source type (e.g. MBTiles, S3)

The key files to change:

1. **`app/config.py`** — `load_tileset_config()` currently branches on `is_tar_file()` vs directory. Add detection and validation for the new type. Add `"source_type": "your_type"` to the returned dict.

2. **`app/main.py`** — the `lifespan` initialises tilesets by source type. Add a branch for your new type. The `get_tile` handler also branches by `source_type` — add your serving logic there.

3. **`app/utils.py`** — add helper functions if needed (path resolution, format detection, etc.).

4. **Tests** — add fixture data, add test cases in both integration and unit files.

### Change how tile metadata is stored or returned

`tileset_metadata[name]` is a plain dict. Its schema is defined implicitly by what the lifespan writes. The keys used elsewhere are:

- `source_type`, `source_path`, `base_path`
- `tile_count`, `tile_count_complete`
- `zoom_levels`, `min_zoom`, `max_zoom`
- `sample_tiles`
- `scanned_at`

If you add a key, write it in the lifespan (for startup), in `rescan_tileset` (for rescan), and expose it in `get_tileset_info` if users should see it.

---

## Where bugs typically hide

### Wrong zoom bounds after startup

**Symptom:** requests for tiles that exist return `404 INVALID_ZOOM_LEVEL`.

**Where to look:**
- `app.state.tileset_metadata[name]["min_zoom"]` and `["max_zoom"]` for the relevant tileset
- Check `tile_count_complete` — if `False`, the startup scan timed out and zoom bounds may be wrong
- For tar tilesets, check `tar_manager.index_status[name]["zoom_levels"]`

**Common cause:** the scan timed out (`scan_tiles` in `config.py`, the `timeout_seconds` parameter). Call `POST /admin/rescan/{name}` to retry.

### Tar tiles returning 503

**Symptom:** `TAR_INDEX_UNAVAILABLE` from a tar tileset that should be ready.

**Where to look:** `tar_manager.index_status[tileset_name]["status"]`. The value will be `"rebuilding"` (transient — wait) or `"error"` (permanent — there will be an `"error"` key with the message).

**Common causes:** the tar file was replaced on disk while the server was running (index still points to old file), or the tar is corrupted. Run `tar -tf /path/to/file.tar` to validate.

### Directory tiles returning 404 for tiles that exist

**Symptom:** `TILE_NOT_FOUND` for a tile you know is on disk.

**Where to look:**
- `find_tile_path` in `utils.py` — is the extension in `SUPPORTED_EXTS`?
- `find_tile_in_tar_index` for tar tilesets — same check
- Is the file actually at the expected path? Log `base_dir / str(z) / str(x) / y_name`.

**Common cause:** tile uses an extension not in the probe list (e.g. `.mvt`, `.pbf` for vector tiles). Add it to `SUPPORTED_EXTS` in `utils.py` and add a corresponding entry in `media_type_for_suffix`.

### Pre-scanned metadata not being used by workers

**Symptom:** workers are scanning directory tilesets again despite MAIN pre-scanning (slow startup, duplicate log lines).

**Where to look:** the `TILE_METADATA_FILE` env var. If it's not set or the file doesn't exist by the time workers start, `create_app()` falls through to the in-worker scan path.

**Common cause:** the temp file path was set in `os.environ` but the fork happened before the file was written (race condition). MAIN writes the file before calling `uvicorn.run()`, so this should not occur in normal operation. Check for exceptions in the MAIN scan path.

### Config errors only appearing in workers, not MAIN

**Symptom:** MAIN reports config valid, but workers crash immediately.

**Where to look:** `CONFIG_PATH` env var. Workers call `load_tileset_config(os.getenv("CONFIG_PATH", "config.json"))`. If the path is relative, it's resolved relative to the worker's working directory, which may differ from MAIN's.

**Fix:** always use absolute paths, or ensure MAIN sets `CONFIG_PATH` to an absolute path (it does, via `os.environ["CONFIG_PATH"] = args.config`).

### ETag mismatch — 304 never returned

**Symptom:** clients never get 304; every request returns 200.

**Where to look:**
- Directory tiles: ETag is `W/"mtime_ns-size"` from `st.st_mtime_ns` and `st.st_size`. This changes if the file changes — intended.
- Tar tiles: ETag is `W/"mtime-size"` from `TileEntry.mtime` and `TileEntry.size`. `mtime` is captured from the tar header when the index is built; it changes only if the tar is recreated.

**Common cause:** the client isn't sending `If-None-Match`. The server only checks that header.

---

## Gotchas and invariants

**Workers do not share memory.** `app.state` is per-process. A rebuild triggered on one worker's request updates that worker's in-memory index, but other workers keep their old index until they receive their own rebuild request. This is by design — it's the nature of the multi-process model.

**`TILE_SCAN=0` is always set for workers.** MAIN sets this before starting Uvicorn so workers never scan directories themselves. If you're running Uvicorn directly (not via `python -m app`), workers default to scanning if `TILE_SCAN` isn't set. Either set it yourself or pass `do_scan=False` to `create_app()`.

**`base_path` stripping.** If a tar has tiles at `tiles/10/512/341.png` and `base_path="tiles"`, `filter_index_for_tileset` strips the prefix so the index key is `"10/512/341.png"`. If you ever change the stripping logic, change it in `filter_index_for_tileset` and verify `find_tile_in_tar_index` still matches.

**`parse_tile_member_path` takes the last three components.** Given `"a/b/c/10/512/341.png"` it returns `("10", "512", "341.png")`. This means any path ending in `z/x/y.ext` where `z` and `x` are digits is treated as a tile. If your tar contains non-tile files with this structure, they'll be indexed as tiles. In practice this doesn't matter because tiles are the only image files in a tile archive.

**Config is validated at startup and never reloaded.** `app.state.tilesets` is set once in `lifespan` and never changed. To add or remove a tileset, you must restart the server.

**Extension probing order.** Both `find_tile_path` and `find_tile_in_tar_index` try the requested extension first, then probe `.png`, `.jpg`, `.jpeg`, `.webp`. If the client requests `341.png` but the file is `341.jpg`, the server returns the `.jpg` with `Content-Type: image/jpeg`. This is intentional but can surprise clients that expect the content type to match the requested extension.

For **directory tilesets on network mounts** (NFS, EFS, SMB), failed probes are expensive: each `is_file()` call that misses crosses the network. Five failed probes per 404 on a sparse tileset will cause measurable latency spikes. Use tar for network-stored tilesets, or ensure the client always requests the correct extension.

**Zoom validation uses metadata, not filesystem reality.** If `min_zoom=10` and `max_zoom=14` in `tileset_metadata`, a request for zoom 9 returns `INVALID_ZOOM_LEVEL` even if a `9/` directory exists. The metadata is set at startup scan or rescan. After adding tiles at a new zoom level, call `POST /admin/rescan/{name}` to update the bounds.
