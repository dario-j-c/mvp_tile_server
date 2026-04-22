"""Tar archive index management and concurrent tile extraction."""

import asyncio
import datetime
import email.utils
import logging
import tarfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app.exceptions import (
    TarIndexUnavailableError,
    TileCorruptedError,
    TileNotFoundError,
)
from app.utils import (
    detect_tar_compression,
    find_tile_in_tar_index,
    media_type_for_suffix,
    parse_tile_member_path,
)

logger = logging.getLogger("event_tile_server")

_MAX_SAMPLE_TILES = 5


def build_tar_index(
    tar_path: Path, base_path: str = ""
) -> Tuple[Dict[str, tarfile.TarInfo], Set[int], List[str]]:
    """
    Build an index of tile members in a tar archive for fast lookup.

    Args:
        tar_path: Path to tar archive.
        base_path: Optional path prefix inside the archive (e.g. "tiles").

    Returns:
        Tuple of (member_index, zoom_levels, sample_tiles).
        member_index maps "z/x/y.ext" -> TarInfo (which stores the byte offset).
        zoom_levels is the set of integer zoom levels found.
        sample_tiles is up to _MAX_SAMPLE_TILES representative paths.
    """
    member_index: Dict[str, tarfile.TarInfo] = {}
    zoom_levels: Set[int] = set()
    sample_tiles: List[str] = []

    if base_path:
        base_path = base_path.strip("/") + "/"

    try:
        with tarfile.open(tar_path, "r:*") as tar:
            for member in tar:
                if not member.isfile():
                    continue

                member_path = member.name
                if base_path and member_path.startswith(base_path):
                    member_path = member_path[len(base_path) :]

                parsed = parse_tile_member_path(member_path)
                if parsed:
                    z_str, x_str, y_name = parsed
                    zoom_levels.add(int(z_str))
                    tile_key = f"{z_str}/{x_str}/{y_name}"
                    member_index[tile_key] = member
                    if len(sample_tiles) < _MAX_SAMPLE_TILES:
                        sample_tiles.append(tile_key)

        logger.debug(
            "Built tar index for %s: %d tiles, zoom levels %s",
            tar_path.name,
            len(member_index),
            sorted(zoom_levels) if zoom_levels else "none",
        )

    except Exception as e:
        logger.error("Error building tar index for %s: %s", tar_path, e)
        raise ValueError(f"Failed to build tar index: {e}")

    return member_index, zoom_levels, sample_tiles


class TarManager:
    """
    Manager for tar file indexes with concurrent per-request tile extraction.

    Each tile extraction opens a fresh file descriptor, seeks to TarInfo.offset_data
    (the absolute byte position stored during indexing), reads the tile, and closes.
    No shared handle is kept, so extractions run fully concurrently in the thread pool.

    Attributes:
        rebuild_lock: Prevents concurrent index rebuilds.
        tar_indexes: Pre-built indexes mapping tile paths to TarInfo objects.
        source_paths: Path to each tileset's tar file, used for per-request opens.
        index_status: Tracks index state per tileset (ready, rebuilding, error).
    """

    def __init__(self) -> None:
        self.rebuild_lock: asyncio.Lock = asyncio.Lock()
        self.tar_indexes: Dict[str, Dict[str, tarfile.TarInfo]] = {}
        self.source_paths: Dict[str, Path] = {}
        self.compression_types: Dict[str, str] = {}
        self.index_status: Dict[str, Dict[str, Any]] = {}

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
                self.tar_indexes[tileset_name] = member_index
                self.source_paths[tileset_name] = source_path
                self.compression_types[tileset_name] = detect_tar_compression(
                    source_path
                )
                self.index_status[tileset_name] = {
                    "status": "ready",
                    "tile_count": len(member_index),
                    "zoom_levels": zoom_levels_sorted,
                    "last_rebuilt": None,
                }

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
                new_index, zoom_levels, _ = await asyncio.to_thread(
                    build_tar_index, source_path, base_path
                )

                zoom_levels_sorted = sorted(zoom_levels)
                self.tar_indexes[tileset_name] = new_index
                self.source_paths[tileset_name] = source_path
                self.compression_types[tileset_name] = detect_tar_compression(
                    source_path
                )
                self.index_status[tileset_name] = {
                    "status": "ready",
                    "tile_count": len(new_index),
                    "zoom_levels": zoom_levels_sorted,
                    "last_rebuilt": datetime.datetime.now().isoformat(),
                }

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
        Extract a tile from a tar archive using a per-request file descriptor.

        TarInfo.offset_data is the absolute byte position in the file recorded
        during indexing. We open the tar, seek there, read the tile's bytes, and
        close — no shared handle, no lock, fully concurrent.

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
        source_path = self.source_paths.get(tileset_name)

        if not tar_index or not source_path:
            raise TarIndexUnavailableError(tileset_name)

        tile_member, tried_extensions = find_tile_in_tar_index(tar_index, z, x, y_name)
        if not tile_member:
            raise TileNotFoundError(tileset_name, z, x, y_name, tried_extensions)

        etag = f'W/"{tile_member.mtime}-{tile_member.size}"'
        media_type = (
            media_type_for_suffix(Path(tile_member.name).suffix)
            or "application/octet-stream"
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

        # Capture scalars before entering the thread — TarInfo is not thread-safe to share.
        offset = tile_member.offset_data
        size = tile_member.size
        mtime = tile_member.mtime
        compression = self.compression_types.get(tileset_name, "uncompressed")

        if compression == "uncompressed":
            # Direct seek to the absolute byte offset in the raw file — O(1), fully concurrent.
            def extract_tile() -> bytes:
                with open(source_path, "rb") as fh:
                    fh.seek(offset)
                    return fh.read(size)
        else:
            # Compressed tar: offset_data is a position in the decompressed stream, not the
            # raw file, so we can't use a raw seek. Open a fresh TarFile per request instead.
            # This decompresses sequentially up to the tile position — inherently slower, but
            # compressed tars are already documented as a performance trade-off.
            def extract_tile() -> bytes:  # type: ignore[misc]
                with tarfile.open(source_path, "r:*") as tf:
                    f = tf.extractfile(tile_member)
                    if f is None:
                        raise ValueError("extractfile returned None")
                    data = f.read()
                    f.close()
                    return data

        try:
            tile_data = await asyncio.to_thread(extract_tile)
        except Exception as e:
            logger.error(
                "Error extracting tile from tar for tileset '%s': %s", tileset_name, e
            )
            raise TileCorruptedError(
                tileset_name, z, x, y_name, f"Extraction failed: {str(e)}"
            )

        headers = {
            "Cache-Control": "public, max-age=86400, immutable",
            "ETag": etag,
            "Last-Modified": email.utils.formatdate(mtime, usegmt=True),
            "X-Tile-Server": "event-optimized",
            "X-Cache-Strategy": "local-event",
            "X-Tileset": tileset_name,
            "X-Source-Type": "tar",
        }

        return tile_data, media_type, headers

    async def close_all(self) -> None:
        """No-op: per-request file descriptors are closed after each extraction."""
