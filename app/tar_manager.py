"""Tar archive index management and mmap-based tile extraction."""

import asyncio
import datetime
import email.utils
import logging
import mmap
import os
import pickle
import tarfile
import tempfile
from pathlib import Path
from typing import IO, Any, Dict, List, Optional, Set, Tuple

from app.exceptions import (
    TarIndexUnavailableError,
    TileCorruptedError,
    TileNotFoundError,
)
from app.utils import (
    TileEntry,
    find_tile_in_tar_index,
    media_type_for_suffix,
    parse_tile_member_path,
)

logger = logging.getLogger("event_tile_server")

_MAX_SAMPLE_TILES = 5


def _touch_sentinel(sentinel_path: Path) -> None:
    """Touch the reload sentinel file so other workers detect a fresh index."""
    try:
        sentinel_path.touch(exist_ok=True)
    except OSError as e:
        logger.warning("Failed to touch reload sentinel %s: %s", sentinel_path, e)


def get_tar_cache_path(tar_path: Path) -> Path:
    """Determine where to save the .idx cache file."""
    cache_dir = os.environ.get("TAR_CACHE_DIR")
    if cache_dir:
        cache_dir_path = Path(cache_dir)
        if not cache_dir_path.is_dir():
            logger.warning(
                "TAR_CACHE_DIR '%s' does not exist; falling back to default cache location",
                cache_dir,
            )
        else:
            return cache_dir_path / f"{tar_path.name}.idx"

    default_path = tar_path.with_suffix(tar_path.suffix + ".idx")
    try:
        if os.access(tar_path.parent, os.W_OK):
            return default_path
    except Exception:
        pass

    return Path(tempfile.gettempdir()) / f"{tar_path.name}.idx"


def build_unified_tar_index(tar_path: Path) -> Dict[str, TileEntry]:
    """Build a unified index of all tile members in a tar archive."""
    unified_index: Dict[str, TileEntry] = {}
    try:
        # "r:*" accepts any compression, but compressed tars are rejected at
        # config-load time, so only uncompressed archives reach this point.
        with tarfile.open(tar_path, "r:*") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                unified_index[member.name] = TileEntry(
                    offset=member.offset_data,
                    size=member.size,
                    mtime=member.mtime,
                    suffix=Path(member.name).suffix.lower(),
                )
        logger.debug(
            "Built unified tar index for %s: %d total files",
            tar_path.name,
            len(unified_index),
        )
    except Exception as e:
        logger.error("Error building unified tar index for %s: %s", tar_path, e)
        raise ValueError(f"Failed to build unified tar index: {e}") from e
    return unified_index


def filter_index_for_tileset(
    unified_index: Dict[str, TileEntry], base_path: str = ""
) -> Tuple[Dict[str, TileEntry], Set[int], List[str]]:
    """Extract tileset-specific structures from the unified index."""
    member_index: Dict[str, TileEntry] = {}
    zoom_levels: Set[int] = set()
    sample_tiles: List[str] = []

    if base_path:
        base_path = base_path.strip("/") + "/"

    for member_path, entry in unified_index.items():
        if base_path and not member_path.startswith(base_path):
            continue

        rel_path = member_path[len(base_path) :] if base_path else member_path
        parsed = parse_tile_member_path(rel_path)
        if parsed:
            z_str, x_str, y_name = parsed
            zoom_levels.add(int(z_str))
            tile_key = f"{z_str}/{x_str}/{y_name}"
            member_index[tile_key] = entry
            if len(sample_tiles) < _MAX_SAMPLE_TILES:
                sample_tiles.append(tile_key)

    return member_index, zoom_levels, sample_tiles


def load_or_build_tar_index(
    tar_path: Path, force_rebuild: bool = False
) -> Dict[str, TileEntry]:
    """Load the unified index from cache if valid, otherwise build and cache it."""
    cache_path = get_tar_cache_path(tar_path)

    if not force_rebuild and cache_path.exists():
        try:
            tar_mtime = tar_path.stat().st_mtime
            cache_mtime = cache_path.stat().st_mtime
            if cache_mtime >= tar_mtime:
                logger.debug(
                    "Loading tar index cache for %s from %s", tar_path.name, cache_path
                )
                with open(cache_path, "rb") as f:
                    unified_index = pickle.load(f)
                return unified_index
        except Exception as e:
            logger.warning(
                "Error checking/loading tar index cache for %s: %s", tar_path.name, e
            )

    logger.info("Building unified tar index for %s...", tar_path.name)
    unified_index = build_unified_tar_index(tar_path)

    try:
        temp_cache = cache_path.with_suffix(".tmp")
        with open(temp_cache, "wb") as f:
            pickle.dump(unified_index, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temp_cache, cache_path)
        logger.debug("Saved tar index cache to %s", cache_path)
    except Exception as e:
        logger.warning("Failed to save tar index cache to %s: %s", cache_path, e)

    return unified_index


def build_tar_index(
    tar_path: Path, base_path: str = "", force_rebuild: bool = False
) -> Tuple[Dict[str, TileEntry], Set[int], List[str]]:
    """
    Build an index of tile members in a tar archive for fast lookup.

    Args:
        tar_path: Path to tar archive.
        base_path: Optional path prefix inside the archive (e.g. "tiles").
        force_rebuild: If True, bypass the cache and rebuild the index.

    Returns:
        Tuple of (member_index, zoom_levels, sample_tiles).
    """
    unified_index = load_or_build_tar_index(tar_path, force_rebuild=force_rebuild)
    return filter_index_for_tileset(unified_index, base_path)


class TarManager:
    """
    Manager for tar file indexes with mmap-based tile extraction.

    On initialization each tileset's tar file is memory-mapped (one file descriptor
    per tileset per worker). Tile reads are synchronous slices of the mmap — O(1)
    seek, no per-request FD open/close, no thread pool required.

    Attributes:
        rebuild_lock: Prevents concurrent index rebuilds.
        tar_indexes: Pre-built indexes mapping tile paths to TileEntry objects.
        mmaps: Memory-mapped views of each tileset's tar file.
        index_status: Tracks index state per tileset (ready, rebuilding, error).
    """

    def __init__(self) -> None:
        self.rebuild_lock: asyncio.Lock = asyncio.Lock()
        self.tar_indexes: Dict[str, Dict[str, TileEntry]] = {}
        self.mmaps: Dict[str, mmap.mmap] = {}
        self._mmap_files: Dict[str, IO[bytes]] = {}
        self.index_status: Dict[str, Dict[str, Any]] = {}
        # Sentinel-based cross-worker reload
        self._tileset_sources: Dict[str, Tuple[Path, str]] = {}
        self._last_loaded_cache_mtime: Dict[str, float] = {}
        self._sentinel_path: Optional[Path] = None
        self._sentinel_mtime: float = 0.0
        self._sentinel_task: Optional[asyncio.Task] = None
        self._sentinel_poll_interval: float = 2.0  # override in tests

    async def initialize_tileset(
        self, tileset_name: str, source_path: Path, base_path: str = ""
    ) -> Tuple[int, List[str], List[int]]:
        """
        Index a tar tileset and return its metadata.

        The metadata (tile count, sample tiles, zoom levels) is derived directly
        from the index — no separate scan pass required.

        Args:
            tileset_name: Unique name for the tileset.
            source_path: Path to the tar archive.
            base_path: Optional path prefix inside the archive.

        Returns:
            Tuple of (tile_count, sample_tiles, zoom_levels_sorted).

        Raises:
            Exception: If the tar cannot be indexed.
        """
        async with self.rebuild_lock:
            logger.debug("Initializing tar tileset '%s'...", tileset_name)

            try:
                member_index, zoom_levels, sample_tiles = await asyncio.to_thread(
                    build_tar_index, source_path, base_path
                )

                zoom_levels_sorted = sorted(zoom_levels)
                fh: IO[bytes] = open(source_path, "rb")
                mmap_obj = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
                self.tar_indexes[tileset_name] = member_index
                self.mmaps[tileset_name] = mmap_obj
                self._mmap_files[tileset_name] = fh
                self.index_status[tileset_name] = {
                    "status": "ready",
                    "tile_count": len(member_index),
                    "zoom_levels": zoom_levels_sorted,
                    "last_rebuilt": None,
                }

                # Record source and cache version for sentinel-triggered reloads.
                self._tileset_sources[tileset_name] = (source_path, base_path)
                cache_path = get_tar_cache_path(source_path)
                try:
                    self._last_loaded_cache_mtime[tileset_name] = (
                        cache_path.stat().st_mtime
                    )
                except OSError:
                    self._last_loaded_cache_mtime[tileset_name] = 0.0

                logger.debug(
                    "Tileset '%s': indexed %d tiles from tar archive",
                    tileset_name,
                    len(member_index),
                )

                return len(member_index), sample_tiles, zoom_levels_sorted

            except Exception as e:
                logger.error(
                    "Failed to initialize tar tileset '%s': %s", tileset_name, e
                )
                self.index_status[tileset_name] = {"status": "error", "error": str(e)}
                raise

    async def rebuild_index(
        self, tileset_name: str, source_path: Path, base_path: str = ""
    ) -> None:
        """
        Rebuild the index for a tileset without restarting the server.

        Args:
            tileset_name: Name of the tileset to rebuild.
            source_path: Path to the tar archive.
            base_path: Optional path prefix inside the archive.

        Raises:
            Exception: If index rebuild fails.
        """
        async with self.rebuild_lock:
            logger.info("Rebuilding index for tileset '%s'...", tileset_name)

            if tileset_name in self.index_status:
                self.index_status[tileset_name]["status"] = "rebuilding"

            try:
                # force_rebuild=True: the tar changed, so bypass cache and
                # write a fresh .idx file for subsequent workers to pick up.
                new_index, zoom_levels, _ = await asyncio.to_thread(
                    build_tar_index, source_path, base_path, True
                )

                zoom_levels_sorted = sorted(zoom_levels)
                new_fh: IO[bytes] = open(source_path, "rb")
                new_mmap = mmap.mmap(new_fh.fileno(), 0, access=mmap.ACCESS_READ)

                old_mmap = self.mmaps.get(tileset_name)
                old_fh = self._mmap_files.get(tileset_name)

                self.tar_indexes[tileset_name] = new_index
                self.mmaps[tileset_name] = new_mmap
                self._mmap_files[tileset_name] = new_fh
                self.index_status[tileset_name] = {
                    "status": "ready",
                    "tile_count": len(new_index),
                    "zoom_levels": zoom_levels_sorted,
                    "last_rebuilt": datetime.datetime.now().isoformat(),
                }

                # Close old mmap after swap — no await between swap and close,
                # so no other coroutine can observe a half-replaced state.
                if old_mmap:
                    old_mmap.close()
                if old_fh:
                    old_fh.close()

                # Update our own cache-mtime record before touching the sentinel so
                # this worker's watcher skips the self-reload.
                cache_path = get_tar_cache_path(source_path)
                try:
                    self._last_loaded_cache_mtime[tileset_name] = (
                        cache_path.stat().st_mtime
                    )
                except OSError:
                    pass
                if self._sentinel_path is not None:
                    _touch_sentinel(self._sentinel_path)

                logger.info(
                    "Successfully rebuilt index for tileset '%s': %d tiles",
                    tileset_name,
                    len(new_index),
                )

            except Exception as e:
                logger.error(
                    "Failed to rebuild index for tileset '%s': %s", tileset_name, e
                )
                self.index_status[tileset_name]["status"] = "error"
                self.index_status[tileset_name]["error"] = str(e)
                raise

    async def get_tile_from_tar(
        self,
        tileset_name: str,
        z: int,
        x: int,
        y_name: str,
        if_none_match: Optional[str] = None,
    ) -> Tuple[Optional[bytes], str, Dict[str, str]]:
        """
        Extract a tile from a tar archive via an mmap slice.

        TileEntry.offset is the absolute byte position in the file recorded during
        indexing. The file is already mmap'd — extraction is a single in-memory
        slice with no per-request FD open and no thread-pool dispatch required.

        Args:
            tileset_name: Name of the tileset.
            z: Zoom level.
            x: X tile coordinate.
            y_name: Y filename with extension (e.g. "123.png").
            if_none_match: Client ETag for conditional 304 support.

        Returns:
            (tile_data, media_type, headers). tile_data is None for a 304.

        Raises:
            TarIndexUnavailableError: Index not ready.
            TileNotFoundError: Tile absent from index.
            TileCorruptedError: Tile present but unreadable.
        """
        if tileset_name not in self.index_status:
            raise TarIndexUnavailableError(tileset_name)

        status = self.index_status[tileset_name]["status"]
        if status != "ready":
            if status == "rebuilding":
                raise TarIndexUnavailableError(tileset_name)
            error_msg = self.index_status[tileset_name].get("error", "Unknown error")
            raise TileCorruptedError(
                tileset_name, z, x, y_name, f"Index error: {error_msg}"
            )

        tar_index = self.tar_indexes.get(tileset_name)
        mmap_obj = self.mmaps.get(tileset_name)

        if not tar_index or not mmap_obj:
            raise TarIndexUnavailableError(tileset_name)

        tile_entry, tried_extensions = find_tile_in_tar_index(tar_index, z, x, y_name)
        if not tile_entry:
            raise TileNotFoundError(tileset_name, z, x, y_name, tried_extensions)

        etag = f'W/"{tile_entry.mtime}-{tile_entry.size}"'
        media_type = (
            media_type_for_suffix(tile_entry.suffix) or "application/octet-stream"
        )

        if if_none_match and if_none_match == etag:
            return (
                None,
                media_type,
                {
                    "ETag": etag,
                    "Cache-Control": "public, max-age=86400, immutable",
                },
            )

        try:
            tile_data: bytes = mmap_obj[
                tile_entry.offset : tile_entry.offset + tile_entry.size
            ]
        except Exception as e:
            logger.error(
                "Error reading tile from mmap for tileset '%s': %s", tileset_name, e
            )
            raise TileCorruptedError(
                tileset_name, z, x, y_name, f"mmap read failed: {e}"
            )

        headers = {
            "Cache-Control": "public, max-age=86400, immutable",
            "ETag": etag,
            "Last-Modified": email.utils.formatdate(tile_entry.mtime, usegmt=True),
            "X-Tileset": tileset_name,
            "X-Source-Type": "tar",
        }

        return tile_data, media_type, headers

    async def close_all(self) -> None:
        """Close all mmap views, file handles, and the sentinel watcher task."""
        if self._sentinel_task is not None and not self._sentinel_task.done():
            self._sentinel_task.cancel()
            try:
                await self._sentinel_task
            except asyncio.CancelledError:
                pass
        for mmap_obj in self.mmaps.values():
            mmap_obj.close()
        for fh in self._mmap_files.values():
            fh.close()
        self.mmaps.clear()
        self._mmap_files.clear()

    def set_sentinel_path(self, path: Path) -> None:
        """Register the sentinel file path; reads its current mtime as the baseline."""
        self._sentinel_path = path
        try:
            self._sentinel_mtime = path.stat().st_mtime
        except OSError:
            self._sentinel_mtime = 0.0

    def start_sentinel_watcher(self) -> None:
        """Start the background task that watches for cross-worker reload signals."""
        if self._sentinel_task is None or self._sentinel_task.done():
            self._sentinel_task = asyncio.create_task(self._watch_sentinel())

    async def _watch_sentinel(self) -> None:
        """Poll the sentinel file; reload stale indexes when its mtime advances."""
        while True:
            try:
                await asyncio.sleep(self._sentinel_poll_interval)
                if self._sentinel_path is None:
                    continue
                try:
                    mtime = self._sentinel_path.stat().st_mtime
                except OSError:
                    continue
                if mtime > self._sentinel_mtime:
                    self._sentinel_mtime = mtime
                    logger.info(
                        "Reload sentinel changed — reloading stale tar indexes"
                    )
                    await self._reload_stale_indexes()
            except asyncio.CancelledError:
                return

    async def _reload_stale_indexes(self) -> None:
        """Reload any tileset whose .idx cache is newer than the version we loaded."""
        async with self.rebuild_lock:
            for name, (source_path, base_path) in self._tileset_sources.items():
                cache_path = get_tar_cache_path(source_path)
                try:
                    cache_mtime = cache_path.stat().st_mtime
                except OSError:
                    continue
                if cache_mtime <= self._last_loaded_cache_mtime.get(name, 0.0):
                    continue

                logger.info("Auto-reloading index for tileset '%s'...", name)
                try:
                    new_index, zoom_levels, _ = await asyncio.to_thread(
                        build_tar_index, source_path, base_path
                    )
                    zoom_levels_sorted = sorted(zoom_levels)
                    new_fh: IO[bytes] = open(source_path, "rb")
                    new_mmap = mmap.mmap(new_fh.fileno(), 0, access=mmap.ACCESS_READ)

                    old_mmap = self.mmaps.get(name)
                    old_fh = self._mmap_files.get(name)

                    self.tar_indexes[name] = new_index
                    self.mmaps[name] = new_mmap
                    self._mmap_files[name] = new_fh
                    self.index_status[name] = {
                        "status": "ready",
                        "tile_count": len(new_index),
                        "zoom_levels": zoom_levels_sorted,
                        "last_rebuilt": datetime.datetime.now().isoformat(),
                    }
                    self._last_loaded_cache_mtime[name] = cache_mtime

                    if old_mmap:
                        old_mmap.close()
                    if old_fh:
                        old_fh.close()

                    logger.info(
                        "Auto-reloaded index for tileset '%s': %d tiles",
                        name,
                        len(new_index),
                    )
                except Exception as e:
                    logger.error(
                        "Failed to auto-reload index for tileset '%s': %s", name, e
                    )
