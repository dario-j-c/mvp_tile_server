# Troubleshooting

---

## Startup Failures

### "Config file not found"

```
ValueError: Config file not found: config.json
```

The server looks for `config.json` in the current working directory by default. Either:
- Run from the directory containing your config, or
- Pass the full path: `python -m app /absolute/path/to/config.json`

---

### "Found N configuration errors"

Printed at startup before the process exits. The message lists every problem:

```
Found 2 configuration errors:

• Invalid tileset name '1osm': Must be alphanumeric...
• Tileset 'satellite': Path does not exist: /data/satellite.tar

✗ No valid tilesets found
```

**Common causes:**
| Error text | Fix |
|---|---|
| `Path does not exist` | Path is wrong or not mounted (Docker). Check with `ls /path/to/tiles`. |
| `Invalid tileset name` | Names must start with a letter or underscore; digits, hyphens, and underscores allowed after that. |
| `must have 'source' key` | Dict-format entry is missing the `source` field. |
| `must be a string` | `source` or `base_path` value is not a string in the JSON. |
| `Error reading tar file` | Tar file is corrupted or not a valid tar. Run `python inspect_tar.py /path/to/file.tar` to verify. |

---

### Startup is very slow

Startup scans directory tilesets to determine tile count and zoom bounds. For large directory trees this can take a while.

**Options:**
- `--no-scan` — skips all scanning; zoom bounds default to 1–25. Tile requests outside the actual zoom range will return `404 INVALID_ZOOM_LEVEL` if the scanned bounds are used elsewhere; without scanning, all zoom values 1–25 are accepted and requests for missing tiles return `404 TILE_NOT_FOUND` instead.
- Use tar archives — tar metadata is derived from the index at startup (header-only read, no tile data), which is much faster than walking a directory tree for large tilesets. Tar serving is also faster at request time, so this is the right default for event deployments regardless of startup speed.

Tar index building is proportional to the number of files, not the file size. ~1 million tiles in an uncompressed tar typically indexes in 15–60 seconds on SSD.

---

### Workers crash immediately after starting

Check the worker logs for `Configuration error:`. The most common cause is that the config path is wrong relative to the working directory of the worker process. Set `CONFIG_PATH` to an absolute path or ensure it's resolved correctly:

```bash
export CONFIG_PATH=/absolute/path/to/config.json
```

When using `python -m app`, the MAIN process sets `CONFIG_PATH` in the environment automatically. If running Uvicorn directly, set it manually:

```bash
CONFIG_PATH=/path/to/config.json uvicorn app.main:get_app --factory
```

---

## Tar Archive Issues

### Tiles not found from a tar archive

**1. Wrong `base_path`**

The most common cause. Inspect the archive first:

```bash
python inspect_tar.py /path/to/tiles.tar
```

The output shows the detected base path and the config snippet to use.

If tiles are at root level (e.g. `10/512/341.png` directly in the tar), omit `base_path` or set it to `""`. If they're nested (e.g. `data/tiles/10/512/341.png`), set `"base_path": "data/tiles"`.

**2. Index not built**

Check the index status:
```
GET /admin/status/{tileset_name}
```

If `status` is `error`, the response includes an `error` field describing the failure. Fix the underlying issue and call:
```
POST /admin/rebuild/{tileset_name}
```

**3. Extension mismatch**

The server probes `.png`, `.jpg`, `.jpeg`, `.webp` automatically. If your tiles use a different extension they won't be found. Verify with `inspect_tar.py` what extensions are in the archive.

---

### Compressed tar rejected at startup

```
Found 1 configuration error:

• Tileset 'satellite': Compressed tar files (.tar.gz, .tar.bz2, .tar.xz) are not supported...
```

The server refuses to start with compressed archives. Compressed tars require sequential decompression from byte 0 to reach any tile — concurrent requests would each decompress the entire file up to their target position, causing CPU saturation under normal event traffic.

**Fix:** repack as uncompressed:

```bash
mkdir /tmp/tiles_extracted
tar -xf tiles.tar.gz -C /tmp/tiles_extracted
tar -cf tiles.tar -C /tmp/tiles_extracted .
rm -rf /tmp/tiles_extracted
```

---

### "TAR_INDEX_UNAVAILABLE" (503)

The index is currently being rebuilt. This is transient — wait a few seconds and retry. If it persists, check `/admin/status/{tileset_name}` for an `error` status.

---

### Tar file replaced on disk, tiles still from old file

The in-memory index still points to the old file. Trigger a rebuild:

```
POST /admin/rebuild/{tileset_name}
```

This re-reads the tar from disk, rebuilds the index, and swaps it atomically. Requests in-flight during the rebuild may receive 503; they will succeed once the rebuild completes.

---

## Tile Request Issues

### 404 INVALID_ZOOM_LEVEL

The requested zoom level is outside the range the server scanned for this tileset.

**Causes and fixes:**
- **Startup scan was skipped (`--no-scan`):** zoom range defaults to 1–25. If you're hitting this, the actual tile files don't exist at that zoom; the request is valid but the tile is absent.
- **Scan timed out:** `tile_count_complete` will be `false` in `/tilesets/{name}`. Call `POST /admin/rescan/{name}` to redo the scan.
- **Wrong zoom level in request:** verify the tileset's actual zoom range with `GET /tilesets/{name}`.
- **Tileset was swapped after startup:** call `POST /admin/rescan/{name}` (directory) or `POST /admin/rebuild/{name}` (tar) to update zoom bounds.

---

### 404 TILE_NOT_FOUND

The tile coordinates are valid but no tile file exists there. This is normal if your tileset has sparse coverage.

To confirm the tileset is being read correctly, check `sample_tiles` in `GET /tilesets/{name}` — request one of the samples and verify it returns 200.

---

### 404 TILESET_NOT_FOUND

The tileset name in the URL doesn't match any entry in the config. The error message lists configured names. Common causes:
- Typo in the URL
- Config file was edited and the server hasn't restarted
- Wrong config file loaded (check startup logs)

---

### 500 TILE_CORRUPTED

The tile file exists but cannot be read. Check:
- File permissions (the server process must be able to read it)
- Disk health — run `fsck` or check SMART status
- For tar tiles: the archive may be partially corrupted; run `tar -tf /path/to/archive.tar` to validate it

---

## Performance Issues

### High latency on directory tilesets under load

Directory serving requires 1–5 `stat` calls per request for extension probing, plus an async file read, all going through the thread pool. This is correct FastAPI practice, but it's inherently more expensive than tar serving, which is a single in-memory mmap slice with no filesystem calls.

For best performance, pack your tiles into an uncompressed tar. Directories are the right choice only when tiles need to be updated live without a server restart.

If you must use directories:
- Increase `--workers` to add more processes
- Check disk I/O with `iostat` or `iotop`
- Avoid network mounts (NFS, EFS, SMB) — each failed `stat` probe on a missing tile crosses the network; five of them per 404 adds up fast on sparse tilesets

### High latency on tar tilesets

Each tile extraction is an in-memory slice of a memory-mapped tar file. The byte offset is recorded in the index at startup; reads are O(1) with no per-request file open and no thread-pool dispatch. Latency should be very low.

If latency is high:
- Check whether the tar file is on a network mount; local SSD is strongly preferred
- Check OS page cache pressure — a very large tar with cold cache will fault pages on first access; subsequent reads are pure RAM

### Tile count shows 0 or very low

The startup scan may have timed out. Check `tile_count_complete` in `GET /tilesets/{name}`:
- If `false`, the scan was cut short; call `POST /admin/rescan/{name}` to retry
- If `true` with count 0, the tileset directory is empty or the tar contains no recognised tiles (check the `z/x/y.ext` path structure)

---

## Docker-Specific Issues

### Tiles not found in container

The most common cause is a path mismatch in the config. The config must use container-side paths:

```json
{
  "tilesets": {
    "osm": "/app/data/osm"
  }
}
```

Verify the volume is mounted and the path is accessible:

```bash
docker exec <container> ls /app/data/
```

### Container starts but immediately exits

Check the logs:
```bash
docker-compose logs tile-server
```

Look for `Configuration Validation Failed` — this means the config paths don't exist inside the container. Fix the `TILE_DATA_PATH` environment variable or the paths in your config file.

---

## Diagnosing an Unknown Problem

The `GET /` and `GET /tilesets/{name}` endpoints expose the server's full internal state. Start there:

1. `GET /health` — is the process alive?
2. `GET /` — are all expected tilesets listed? Are tile counts non-zero?
3. `GET /tilesets/{name}` — check `zoom_range`, `tile_count_complete`, `sample_tiles`
4. Request one of the `sample_tiles` directly — does it return 200?
5. For tar tilesets: `GET /admin/status/{name}` — is the index `ready`?
6. Check server logs for `ERROR` lines — these always include the tileset name and a reason.
