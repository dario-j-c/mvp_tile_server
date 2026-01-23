"""Thread-safe tar archive management for serving tiles."""

import asyncio
import datetime
import email.utils
import logging
import tarfile
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from app.exceptions import (
    TarIndexUnavailableError,
    TileCorruptedError,
    TileNotFoundError,
)
from app.utils import find_tile_in_tar_index, media_type_for_suffix

logger = logging.getLogger("event_tile_server")


def build_tar_index(
    tar_path: Path, base_path: str = ""
) -> Tuple[Dict[str, tarfile.TarInfo], Set[int]]:
    """
    Build an index of tile members in a tar archive for fast lookup.

    Args:
        tar_path: Path to tar archive
        base_path: Optional base path inside tar (e.g., "tiles")

    Returns:
        Tuple of (member_index, zoom_levels)
        member_index: Dict mapping "z/x/y.ext" to TarInfo objects
        zoom_levels: Set of zoom levels found
    """
    member_index = {}
    zoom_levels = set()

    # Normalize base_path
    if base_path:
        base_path = base_path.strip("/") + "/"

    try:
        with tarfile.open(tar_path, "r:*") as tar:
            for member in tar:
                if not member.isfile():
                    continue

                # Remove base_path prefix if present
                member_path = member.name
                if base_path and member_path.startswith(base_path):
                    member_path = member_path[len(base_path) :]

                # Check if this matches tile pattern: z/x/y.ext
                parts = member_path.split("/")
                if len(parts) >= 3:
                    z_str, x_str, y_name = parts[-3], parts[-2], parts[-1]
                    if z_str.isdigit() and x_str.isdigit():
                        z = int(z_str)
                        zoom_levels.add(z)

                        # Store with normalized path for lookup
                        tile_key = f"{z_str}/{x_str}/{y_name}"
                        member_index[tile_key] = member

        logger.info(
            "Built tar index for %s: %d tiles, zoom levels %s",
            tar_path.name,
            len(member_index),
            sorted(zoom_levels) if zoom_levels else "none",
        )

    except Exception as e:
        logger.error("Error building tar index for %s: %s", tar_path, e)
        raise ValueError(f"Failed to build tar index: {e}")

    return member_index, zoom_levels


class TarManager:
    """
    Thread-safe manager for tar file handles and indexes.

    Handles concurrent access to tar archives and supports hot index
    rebuilding without server restart.

    Attributes:
        tileset_locks: Per-tileset locks to prevent concurrent tar reads.
        rebuild_lock: Lock for index rebuilding operations.
        tar_handles: Open tar file handles, one per tileset.
        tar_indexes: Pre-built indexes mapping tile paths to TarInfo.
        index_status: Metadata tracking index state (ready, rebuilding, error).
    """

    def __init__(self) -> None:
        self.tileset_locks: Dict[str, asyncio.Lock] = {}
        self.rebuild_lock: asyncio.Lock = asyncio.Lock()
        self.tar_handles: Dict[str, tarfile.TarFile] = {}
        self.tar_indexes: Dict[str, Dict[str, tarfile.TarInfo]] = {}
        self.index_status: Dict[str, Dict[str, Any]] = {}

    async def initialize_tileset(
        self, tileset_name: str, source_path: Path, base_path: str = ""
    ) -> None:
        """
        Initialize tar handle and index for a tileset.

        Args:
            tileset_name: Unique name for the tileset.
            source_path: Path to the tar archive.
            base_path: Optional path prefix inside the tar archive.

        Raises:
            Exception: If tar file cannot be opened or indexed.
        """
        async with self.rebuild_lock:
            # Create lock for this tileset if it doesn't exist
            if tileset_name not in self.tileset_locks:
                self.tileset_locks[tileset_name] = asyncio.Lock()

            logger.info("Initializing tar tileset '%s'...", tileset_name)

            try:
                # Build index (runs in thread pool to avoid blocking)
                member_index, zoom_levels = await asyncio.to_thread(
                    build_tar_index, source_path, base_path
                )

                # Open tar handle (runs in thread pool)
                tar_handle = await asyncio.to_thread(tarfile.open, source_path, "r:*")

                # Store results
                self.tar_indexes[tileset_name] = member_index
                self.tar_handles[tileset_name] = tar_handle
                self.index_status[tileset_name] = {
                    "status": "ready",
                    "tile_count": len(member_index),
                    "zoom_levels": sorted(zoom_levels),
                    "last_rebuilt": None,
                }

                logger.info(
                    "Tileset '%s': Indexed %d tiles from tar archive",
                    tileset_name,
                    len(member_index),
                )

            except Exception as e:
                logger.error(
                    "Failed to initialize tar tileset '%s': %s", tileset_name, e
                )
                self.index_status[tileset_name] = {
                    "status": "error",
                    "error": str(e),
                }
                raise

    async def rebuild_index(
        self, tileset_name: str, source_path: Path, base_path: str = ""
    ) -> None:
        """
        Rebuild index for a tileset without restarting the server.

        Args:
            tileset_name: Name of the tileset to rebuild.
            source_path: Path to the tar archive.
            base_path: Optional path prefix inside the tar archive.

        Raises:
            Exception: If index rebuild fails.
        """

        async with self.rebuild_lock:
            logger.info("Rebuilding index for tileset '%s'...", tileset_name)

            # Mark as rebuilding
            if tileset_name in self.index_status:
                self.index_status[tileset_name]["status"] = "rebuilding"

            try:
                # Build new index
                new_index, zoom_levels = await asyncio.to_thread(
                    build_tar_index, source_path, base_path
                )

                # Close old tar handle if exists
                if tileset_name in self.tar_handles:
                    old_handle = self.tar_handles[tileset_name]
                    await asyncio.to_thread(old_handle.close)

                # Open new tar handle
                new_handle = await asyncio.to_thread(tarfile.open, source_path, "r:*")

                # Atomic swap: update index and handle together
                async with self.tileset_locks[tileset_name]:
                    self.tar_indexes[tileset_name] = new_index
                    self.tar_handles[tileset_name] = new_handle

                # Update status
                self.index_status[tileset_name] = {
                    "status": "ready",
                    "tile_count": len(new_index),
                    "zoom_levels": sorted(zoom_levels),
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
        Thread-safe tile extraction from tar archive.

        Args:
            tileset_name: Name of the tileset.
            z: Zoom level.
            x: X tile coordinate.
            y_name: Y coordinate with file extension (e.g., "123.png").
            if_none_match: ETag from client for conditional request (304 support).

        Returns:
            Tuple of (tile_data, media_type, response_headers).
            tile_data is None if if_none_match matches (304 response).

        Raises:
            TarIndexUnavailableError: If index is not ready.
            TileNotFoundError: If tile doesn't exist.
            TileCorruptedError: If tile exists but can't be read.
        """
        # Check if index is available
        if tileset_name not in self.index_status:
            raise TarIndexUnavailableError(tileset_name)

        status = self.index_status[tileset_name]["status"]
        if status != "ready":
            if status == "rebuilding":
                raise TarIndexUnavailableError(tileset_name)
            elif status == "error":
                error_msg = self.index_status[tileset_name].get(
                    "error", "Unknown error"
                )
                raise TileCorruptedError(
                    tileset_name, z, x, y_name, f"Index error: {error_msg}"
                )

        # Get index and handle
        tar_index = self.tar_indexes.get(tileset_name)
        tar_handle = self.tar_handles.get(tileset_name)

        if not tar_index or not tar_handle:
            raise TarIndexUnavailableError(tileset_name)

        # Find tile in index
        tile_member, tried_extensions = find_tile_in_tar_index(tar_index, z, x, y_name)
        if not tile_member:
            raise TileNotFoundError(tileset_name, z, x, y_name, tried_extensions)

        # Build ETag and check for conditional request (304 Not Modified)
        etag = f'W/"{tile_member.mtime}-{tile_member.size}"'
        tile_path = Path(tile_member.name)
        media_type = (
            media_type_for_suffix(tile_path.suffix) or "application/octet-stream"
        )

        if if_none_match and if_none_match == etag:
            # Return None for tile_data to signal 304 response
            headers = {
                "ETag": etag,
                "Cache-Control": "public, max-age=86400, immutable",
            }
            return None, media_type, headers

        # Extract tile data with lock to prevent concurrent reads
        async with self.tileset_locks[tileset_name]:
            try:
                # Run extraction in thread pool to avoid blocking
                def extract_tile():
                    file_obj = tar_handle.extractfile(tile_member)
                    if not file_obj:
                        raise ValueError("extractfile returned None")
                    tile_data = file_obj.read()
                    file_obj.close()
                    return tile_data

                tile_data = await asyncio.to_thread(extract_tile)

            except Exception as e:
                logger.error(
                    "Error extracting tile from tar for tileset '%s': %s",
                    tileset_name,
                    e,
                )
                raise TileCorruptedError(
                    tileset_name, z, x, y_name, f"Extraction failed: {str(e)}"
                )

        # Prepare response headers
        headers = {
            "Cache-Control": "public, max-age=86400, immutable",
            "ETag": etag,
            "Last-Modified": email.utils.formatdate(tile_member.mtime, usegmt=True),
            "X-Tile-Server": "event-optimized",
            "X-Cache-Strategy": "local-event",
            "X-Tileset": tileset_name,
            "X-Source-Type": "tar",
        }

        return tile_data, media_type, headers

    async def close_all(self) -> None:
        """Close all open tar file handles gracefully."""
        for tileset_name, tar_handle in self.tar_handles.items():
            try:
                await asyncio.to_thread(tar_handle.close)
                logger.info("Closed tar handle for tileset '%s'", tileset_name)
            except Exception as e:
                logger.error(
                    "Error closing tar handle for tileset '%s': %s",
                    tileset_name,
                    e,
                )
