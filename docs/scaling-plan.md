# Scaling Plan: Large Tar Support and Cross-Worker Coordination

A design document comparing two architectural paths for handling the multi-worker coordination and RAM pressure problems at 36 GB+ tar file scale.

---

## Current Pain Points (From Docs and Code)

### 1. Cross-worker rebuild staleness (documented gotcha, not mitigated)

From `dev-guide.md`:
> "A rebuild triggered on one worker's request updates that worker's in-memory index, but other workers keep their old index until they receive their own rebuild request. This is by design."

It is by design, but it is a real operational gap. If you swap a tar on disk and call `POST /admin/rebuild/satellite`, only the worker that handles that request gets the new index. The other 3 workers continue serving tiles from the old mmap — including stale byte offsets that point into a file that may have been replaced or truncated.

This is a correctness issue, not just a performance issue.

### 2. Per-worker RAM duplication of the index

The existing `.idx` pickle cache solves re-parsing on startup. It does **not** solve the fact that each worker deserialises the full unified index into its own heap. For a 36 GB tar with a large tile count, the index can be hundreds of MB per worker:

| Tiles in archive | Estimated index size | 4 workers |
|---|---|---|
| 100 K | ~20 MB | ~80 MB |
| 1 M | ~200 MB | ~800 MB |
| 5 M | ~1 GB | ~4 GB |

Each `TileEntry` is a NamedTuple of 4 scalars (~64 bytes of object overhead) plus the string key (`"10/512/341.png"`, ~15–25 bytes). At 1 M tiles that's already ~200 MB before dict overhead.

### 3. First-run startup time (partially solved)

The `.idx` pickle cache solves subsequent restarts. The first-run parse of a 36 GB tar with many tiles can still take 15–60+ seconds. This is one-time per deployment, but worth noting.

### 4. What is already solved and should not be regressed

- **Startup redundancy**: MAIN builds the cache once; workers unpickle it. N−1 redundant parses eliminated.
- **Per-request latency**: O(1) mmap slice. No thread-pool, no syscall, no lock.
- **Atomic cache write**: `.tmp` → `os.replace()`. No partial reads.
- **mtime invalidation**: stale `.idx` is automatically discarded.
- **Force rebuild**: `POST /admin/rebuild` bypasses cache and writes a fresh `.idx` for subsequent workers.

---

## A Note on Honker

Researched at https://github.com/russellromney/honker (290+ commits, actively maintained, Apache-2.0/MIT dual licensed).

**What it actually is:** A SQLite extension + Python bindings that adds Postgres-style `NOTIFY`/`LISTEN` semantics to SQLite, with durable pub/sub, task queues, event streams, named locks, and rate limiting — all without an external broker.

**How it works under the hood:** Honker detects committed changes via SQLite's built-in `PRAGMA data_version` counter. When that integer increments (any write to the DB), all listeners are woken up to re-query their indexed state. The polling interval defaults to 1 ms. There is no kernel-level push; it is very-fast polling over a shared SQLite file.

**Key constraint:** Requires a native C extension loaded via `SELECT load_extension('honker')`. Must be compiled for and available in your deployment environment (verify for Debian WSL container before depending on it).

**Implication for Path A:** SQLite's `PRAGMA data_version` is a built-in feature — no extension required. For our simple "one integer bumped → workers reload" use case, we do **not** need Honker at all. Honker becomes valuable if Path A later adds durable task queues (e.g. "rebuild this tileset at 03:00"), dead-letter handling, or named distributed locks. For basic cross-worker notification, `PRAGMA data_version` polling at 1–2 s intervals is sufficient and has zero additional dependencies.

---

## Path A: SQLite Unified Store (stdlib only)

Replace `.idx` pickle files and temp JSON metadata with a single `tiles.db` SQLite database.

### Schema

```sql
CREATE TABLE tiles (
    archive_id  INTEGER NOT NULL REFERENCES archives(id),
    tile_key    TEXT    NOT NULL,   -- "10/512/341.png"
    byte_offset INTEGER NOT NULL,
    byte_size   INTEGER NOT NULL,
    mtime       REAL    NOT NULL,
    suffix      TEXT    NOT NULL,
    PRIMARY KEY (archive_id, tile_key)
);

CREATE TABLE archives (
    id          INTEGER PRIMARY KEY,
    path        TEXT    NOT NULL UNIQUE,
    mtime       REAL    NOT NULL,
    indexed_at  REAL    NOT NULL
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- meta stores: schema_version, last_updated (epoch float)
```

### How cross-worker notification works (without Honker)

Each worker runs a background asyncio task:

```python
async def _watch_for_reload(self, tileset_name: str, source_path: Path) -> None:
    last_seen = self._db_version()
    while True:
        await asyncio.sleep(1.5)
        current = self._db_version()
        if current != last_seen:
            last_seen = current
            await self._reload_from_db(tileset_name, source_path)
```

`_db_version()` reads a single integer from `meta` — a fast read that hits the OS page cache. When MAIN (or any worker's rebuild endpoint) updates the DB and bumps `last_updated`, all workers detect it within ~1.5 seconds and reload from the DB without restarting.

If Honker proves out, replace the `asyncio.sleep` poll with `async for msg in db.listen("index_updated")`. The rest of the architecture is identical.

### How RAM is saved

SQLite pages live in the **OS page cache**, shared across all processes reading the same file. Workers do not deserialise the entire index at startup — they query only the tiles they actually serve. For a 36 GB tar with 1 M tiles:

| Metric | Current (pickle) | SQLite |
|---|---|---|
| Per-worker startup RAM | ~200 MB | ~0 (no startup load) |
| 4 workers total | ~800 MB | ~0 extra |
| OS page cache (all workers share) | 200 MB (unpickle) × 4 | ~200 MB (DB pages, shared once) |

The trade-off is that each tile request now includes a SQLite query:

```python
row = db.execute(
    "SELECT byte_offset, byte_size, mtime, suffix FROM tiles "
    "WHERE archive_id=? AND tile_key=?",
    (archive_id, tile_key)
).fetchone()
```

**Lookup latency**: ~10–50 μs vs ~0.05 μs (Python dict). At 1,000 req/s per worker, the SQLite overhead is 10–50 ms/s of CPU. At 10,000 req/s, it's 100–500 ms/s — approaching a meaningful fraction of a CPU core. This is the principal trade-off.

### Mitigating the latency hit: LRU cache

```python
from functools import lru_cache

@lru_cache(maxsize=8192)
def _lookup_tile(self, archive_id: int, tile_key: str) -> Optional[TileEntry]:
    ...
```

An 8,192-entry LRU covers the hot tile set for most event deployments (tile clients repeatedly request the same viewport). Cache hits return at dict speed; only cold misses hit SQLite. For event use cases where the same area is viewed by many clients, this is highly effective.

### What changes

| File | Change |
|---|---|
| `app/tar_manager.py` | Replace pickle load/save with SQLite read/write; add background watcher task; add LRU cache for lookups |
| `app/__main__.py` | Build `tiles.db` instead of `.idx` files; same pre-build contract |
| `app/config.py` | Write directory scan results to `tiles.db` instead of temp JSON |
| `app/main.py` | Remove `TILE_METADATA_FILE` env var path; workers read from DB |
| `tests/test_tar_cache.py` | Rewrite for SQLite-based cache layer |
| `.gitignore` | Add `*.db`, `*.db-wal`, `*.db-shm` |

### Complexity

High. This touches every layer of the data pipeline and all four test files. It also changes the startup contract between MAIN and workers in a non-trivial way.

---

## Path B: Sentinel File Notification + Keep Pickle Cache

Solve the cross-worker staleness problem only, without changing the storage layer.

### Cross-worker notification via sentinel file

On `POST /admin/rebuild/{name}`:
1. `rebuild_index()` runs as today (force_rebuild=True, writes fresh `.idx`)
2. After the swap, `os.utime(".reload_sentinel")` — touches a file to bump its mtime
3. Each worker runs a background asyncio task:

```python
async def _watch_sentinel(self, sentinel_path: Path) -> None:
    last_mtime = sentinel_path.stat().st_mtime if sentinel_path.exists() else 0.0
    while True:
        await asyncio.sleep(2.0)
        try:
            mtime = sentinel_path.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime > last_mtime:
            last_mtime = mtime
            logger.info("Reload sentinel detected — reloading tar indexes")
            await self._reload_all_indexes()
```

`_reload_all_indexes()` calls `load_or_build_tar_index()` for each known tar (cache hit, fast) and swaps the index atomically — same swap logic as `rebuild_index()` without the full parse.

### What changes

| File | Change |
|---|---|
| `app/tar_manager.py` | Add `_watch_sentinel` background task; `rebuild_index()` touches sentinel; add `_reload_all_indexes()` |
| `app/main.py` | Start sentinel watcher in lifespan; pass sentinel path |
| Tests | Add test for sentinel detection and reload |

### What it does NOT solve

- RAM duplication — each worker still holds its own copy of the full index
- First-run parse time — unchanged
- Unified storage — `.idx` files and temp JSON remain separate

### Complexity

Low. ~100 lines of new code, all in `tar_manager.py` and `main.py`. No schema changes, no dependency changes, no test file rewrites.

---

## Comparison

| Concern | Path A (SQLite) | Path B (Sentinel) |
|---|---|---|
| Cross-worker notification | ✓ ~1.5s propagation | ✓ ~2s propagation |
| Per-worker RAM (1M tile index) | ✓ ~0 startup, warm LRU | ✗ ~200 MB each |
| Per-request lookup latency | ✗ +10–50 μs (mitigated by LRU) | ✓ unchanged (~0.05 μs) |
| First-run parse time | ✓ same (tar headers still read once) | ✓ same |
| Unified storage | ✓ single tiles.db | ✗ .idx + temp JSON |
| Dependency changes | ✗ sqlite3 (stdlib) | ✓ none |
| Implementation effort | ✗ High (~2–3 days) | ✓ Low (~half day) |
| Risk of regression | ✗ High (all layers change) | ✓ Low (additive only) |
| Honker required | Optional | No |

---

## Status

**Path B is implemented** (`app/tar_manager.py`, `app/main.py`, `tests/test_tar_cache.py`). See git log for the commit.

Path A remains the planned upgrade path when RAM pressure from large indexes is confirmed by measurement.

---

## Recommendation

**Do Path B first. Then revisit Path A if RAM becomes a measured problem.**

Reasoning:

1. **The cross-worker staleness is the only correctness gap.** Path B closes it with low risk.

2. **RAM is only a problem at high tile counts.** Whether the 36 GB tar generates a 20 MB or 2 GB index depends on how many tiles are in it, not how large the tar is. Measure first with `GET /admin/status/{name}` → `tile_count`. Multiply by ~200 bytes to estimate per-worker heap pressure.

3. **LRU mitigation for SQLite only helps if the hot set fits in the cache.** For event deployments where clients are viewing a specific geographic region, this is likely. For world-coverage tilesets served at random zoom, it is not.

4. **Path A is the right long-term architecture** — unified storage, cross-worker notification, shared OS page cache — but it should be built when RAM pressure is confirmed, not speculatively.

5. **Verify Honker before depending on it.** If it exists and is maintained, it turns the polling loop into true push notification. If it doesn't, the polling loop is fine.

---

## Implementation Summary (Path B)

**New in `app/tar_manager.py`:**
- `_touch_sentinel(path)` — module-level helper; touches the file, logs warning on failure
- `TarManager._tileset_sources` — maps tileset name → `(source_path, base_path)` for reload use
- `TarManager._last_loaded_cache_mtime` — tracks the `.idx` mtime we last loaded per tileset; prevents self-reload after a rebuild
- `TarManager._sentinel_path/mtime/task/poll_interval` — watcher state
- `TarManager.set_sentinel_path(path)` — registers the sentinel and reads its baseline mtime
- `TarManager.start_sentinel_watcher()` — spawns the `_watch_sentinel` asyncio task
- `TarManager._watch_sentinel()` — polls every 2 s (configurable via `_sentinel_poll_interval` for test overrides); calls `_reload_stale_indexes()` on mtime change
- `TarManager._reload_stale_indexes()` — under `rebuild_lock`, reloads any tileset whose `.idx` is newer than `_last_loaded_cache_mtime`; swaps mmap atomically, same pattern as `rebuild_index()`
- `rebuild_index()` — now updates `_last_loaded_cache_mtime` and calls `_touch_sentinel()` after the swap
- `initialize_tileset()` — now populates `_tileset_sources` and `_last_loaded_cache_mtime`
- `close_all()` — now cancels the sentinel task before closing mmaps

**New in `app/main.py`:**
- `_compute_sentinel_path(config_path)` — stable per-config path derived from MD5 of config path; uses `TAR_CACHE_DIR` if set, falls back to `tempfile.gettempdir()`
- lifespan now calls `set_sentinel_path` + `start_sentinel_watcher` if any tar tilesets are present

**New in `tests/test_tar_cache.py`:**
- 11 new tests covering `_touch_sentinel`, `set_sentinel_path`, rebuild-touches-sentinel, self-reload prevention, `_reload_stale_indexes` reload/skip logic, watcher task lifecycle, and watcher-detects-change end-to-end

### Migration path from Path B to Path A

Because Path B is purely additive, the migration to Path A later does not require undoing Path B — it replaces the pickle cache and sentinel file with the SQLite DB and DB-version watcher. The sentinel file becomes the DB's `last_updated` row. The pickle load/save becomes a `SELECT`/`INSERT`. The background task pattern is identical.
