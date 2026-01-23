"""
FastAPI application for the tile server.

Event-optimized high-performance tile server using FastAPI and Uvicorn.
Serves static tile files from multiple configured tile directories or tar archives.

Supports multiple tilesets configured via JSON file, following {tileset_name}/{z}/{x}/{y.ext}.

Optimized for local event deployment with looping displays and interactive maps.
Should handle zoom levels 1-25 with efficient multi-worker tile serving.

Usage:
    python -m app [config_file] -p [port] -b [bind address]

    Or run directly with uvicorn (for production events):
    uvicorn app.main:get_app --factory --host 0.0.0.0 --port 8000 --workers 4
"""

import asyncio
import email.utils
import json
import logging
import os
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.config import (
    DEFAULT_MAX_Z,
    DEFAULT_MIN_Z,
    load_tileset_config,
    scan_all_tilesets,
)
from app.exceptions import (
    InvalidCoordinateError,
    InvalidZoomLevelError,
    TileCorruptedError,
    TileNotFoundError,
    TileServerError,
    TilesetNotFoundError,
)
from app.tar_manager import TarManager
from app.utils import find_tile_path, media_type_for_suffix

logger = logging.getLogger("event_tile_server")


def create_app(
    config_path: str,
    do_scan: bool = True,
    metadata_file: Optional[str] = None,
) -> FastAPI:
    """
    Create and configure the FastAPI application for serving map tiles.

    Args:
        config_path: Path to JSON configuration file containing tileset definitions.
        do_scan: If True, scan directories on startup for metadata.
        metadata_file: Path to pre-scanned metadata JSON file (from MAIN process).

    Returns:
        Configured FastAPI application instance.

    Raises:
        ValueError: If configuration file is invalid.
    """
    # Configure logging for worker processes (basicConfig is idempotent)
    pid = os.getpid()
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(levelname)s:\t[WORKER {pid}] %(message)s",
    )

    # Load pre-scanned metadata if available (from MAIN process)
    pre_scanned_metadata = None
    if metadata_file and os.path.exists(metadata_file):
        try:
            with open(metadata_file, "r") as f:
                pre_scanned_metadata = json.load(f)
            logger.info("Loaded pre-scanned metadata from MAIN process")
        except Exception as e:
            logger.warning("Failed to load pre-scanned metadata: %s", e)

    # Load and validate tileset configuration (suppress warnings, MAIN already showed them)
    try:
        tilesets = load_tileset_config(config_path, show_warnings=False)
        logger.info("Loaded %d tilesets from config", len(tilesets))
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        raise

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        logger.info("Event tile server starting with %d tilesets", len(tilesets))

        # Initialize tar manager
        tar_manager = TarManager()

        # Initialize tar-based tilesets
        for tileset_name, tileset_info in tilesets.items():
            if tileset_info["source_type"] == "tar":
                source_path: Path = tileset_info["source_path"]
                base_path: str = tileset_info.get("base_path", "")

                try:
                    await tar_manager.initialize_tileset(
                        tileset_name, source_path, base_path
                    )
                except Exception as e:
                    logger.error(
                        "Failed to initialize tar tileset '%s': %s", tileset_name, e
                    )
                    # Continue with other tilesets even if one fails

        # Load metadata (prefer pre-scanned from MAIN, fallback to scanning or defaults)
        tileset_metadata = {}
        if pre_scanned_metadata:
            tileset_metadata = pre_scanned_metadata
            logger.info(
                "Using pre-scanned metadata for %d tilesets", len(tileset_metadata)
            )
        elif do_scan:
            logger.info("Pre-calculating tile metadata for all tilesets...")
            tileset_metadata = scan_all_tilesets(tilesets)
        else:
            # Create minimal metadata without scanning
            for name, tileset_info in tilesets.items():
                tileset_metadata[name] = {
                    "source_path": str(tileset_info["source_path"]),
                    "source_type": tileset_info["source_type"],
                    "base_path": tileset_info.get("base_path", ""),
                    "tile_count": 0,
                    "sample_tiles": [],
                    "zoom_levels": [],
                    "min_zoom": DEFAULT_MIN_Z,
                    "max_zoom": DEFAULT_MAX_Z,
                }

        app.state.tilesets = tilesets
        app.state.tileset_metadata = tileset_metadata
        app.state.tar_manager = tar_manager

        logger.info("Event tile server ready for displays!")
        try:
            yield
        finally:
            # Shutdown
            logger.info("Event tile server shutting down...")
            await tar_manager.close_all()

    app = FastAPI(
        title="Multi-Tileset Event Tile Server",
        description="High-performance tile server with multiple tileset support, optimized for local events",
        version="2.5.0-event",
        lifespan=lifespan,
    )

    # Security + CORS
    # WARNING: This permissive CORS configuration (`allow_origins=["*"]`) is suitable only for
    # internal networks and local development. If this server were to be exposed to the
    # public internet, this MUST be changed to a restrictive list of allowed origins
    # to prevent Cross-Site Request Forgery (CSRF) and other web security vulnerabilities.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # For local events only
        allow_credentials=False,
        allow_methods=["GET", "POST"],  # Added POST for rebuild endpoint
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    # Custom exception handler for TileServerError
    @app.exception_handler(TileServerError)
    async def tile_server_error_handler(request: Request, exc: TileServerError):
        error_code = exc.error_code or "INTERNAL_ERROR"
        logger.warning("%s - %s [%s]", error_code, exc.message, request.url.path)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": error_code,
                "message": exc.message,
                "path": str(request.url.path),
            },
        )

    @app.get("/health", summary="Health check endpoint")
    async def health_check():
        return {
            "status": "healthy",
            "service": "multi-tileset-event-tile-server",
            "environment": "local-event",
        }

    @app.get("/", summary="Server information and status")
    async def root(request: Request):
        tilesets_info = {}
        total_tiles = 0

        for name, metadata in request.app.state.tileset_metadata.items():
            tilesets_info[name] = {
                "source_type": metadata["source_type"],
                "source_path": metadata["source_path"],
                "tile_count": metadata["tile_count"],
                "zoom_levels": metadata["zoom_levels"],
                "sample_tiles": metadata["sample_tiles"][:3],
            }
            total_tiles += metadata["tile_count"]

        return {
            "service": "Multi-Tileset Event Tile Server",
            "version": "2.5.0-event",
            "environment": "local-event",
            "tilesets": tilesets_info,
            "total_tiles": f"{total_tiles:,}",
            "health_check_url": "/health",
            "tileset_detail_url": "/tilesets/{tileset_name}",
            "tile_url_format": "/{tileset_name}/{z}/{x}/{y.ext}",
            "admin_endpoints": {
                "rebuild_tar_index": "/admin/rebuild/{tileset_name}",
                "tar_index_status": "/admin/status/{tileset_name}",
            },
            "optimizations": [
                "Multi-worker tile serving via Uvicorn",
                "Thread-safe tar archive access with async locks",
                "Multiple tileset support with independent caching",
                "Support for directory and tar archive sources",
                "Tar archive streaming (no disk extraction required)",
                "Hot index rebuild without server restart",
                "Aggressive client-side caching for looping displays",
                "Local network optimization",
                "Event stability features",
                "Optional pre-scanned tile metadata on startup",
            ],
            "note": "Optimized for local event deployment with zoom levels 1-25. Supports both directory and tar archive tile sources.",
        }

    @app.get(
        "/tilesets/{tileset_name}",
        summary="Get detailed information about a specific tileset",
    )
    async def get_tileset_info(tileset_name: str, request: Request):
        """Get detailed metadata for a specific tileset."""
        if tileset_name not in request.app.state.tileset_metadata:
            available = list(request.app.state.tilesets.keys())
            raise TilesetNotFoundError(tileset_name, available)

        metadata = request.app.state.tileset_metadata[tileset_name]
        response = {
            "name": tileset_name,
            "source_type": metadata["source_type"],
            "source_path": metadata["source_path"],
            "tile_count": f"{metadata['tile_count']:,}",
            "zoom_levels": metadata["zoom_levels"],
            "zoom_range": f"{metadata['min_zoom']}-{metadata['max_zoom']}"
            if metadata["zoom_levels"]
            else "unknown",
            "sample_tiles": metadata["sample_tiles"],
            "tile_url_format": f"/{tileset_name}/{{z}}/{{x}}/{{y.ext}}",
        }

        # Add tar-specific info
        if metadata["source_type"] == "tar":
            if metadata.get("base_path"):
                response["base_path"] = metadata["base_path"]

            # Add index status if available
            tar_manager = request.app.state.tar_manager
            if tileset_name in tar_manager.index_status:
                response["index_status"] = tar_manager.index_status[tileset_name]

        return response

    @app.post(
        "/admin/rebuild/{tileset_name}",
        summary="Rebuild tar index for a tileset without restarting server",
    )
    async def rebuild_tar_index(tileset_name: str, request: Request):
        """
        Rebuild the tar index for a specific tileset.
        Only works for tar-based tilesets.
        """
        if tileset_name not in request.app.state.tilesets:
            available = list(request.app.state.tilesets.keys())
            raise TilesetNotFoundError(tileset_name, available)

        tileset_info = request.app.state.tilesets[tileset_name]

        if tileset_info["source_type"] != "tar":
            raise HTTPException(
                status_code=400,
                detail=f"Tileset '{tileset_name}' is not a tar-based tileset. "
                f"Index rebuild only supported for tar sources.",
            )

        source_path: Path = tileset_info["source_path"]
        base_path: str = tileset_info.get("base_path", "")

        tar_manager = request.app.state.tar_manager

        try:
            await tar_manager.rebuild_index(tileset_name, source_path, base_path)

            return {
                "status": "success",
                "message": f"Index rebuilt successfully for tileset '{tileset_name}'",
                "index_status": tar_manager.index_status[tileset_name],
            }

        except Exception as e:
            logger.error("Error rebuilding index for '%s': %s", tileset_name, e)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to rebuild index: {str(e)}",
            )

    @app.get(
        "/admin/status/{tileset_name}",
        summary="Get tar index status for a tileset",
    )
    async def get_tar_status(tileset_name: str, request: Request):
        """Get the current status of a tar-based tileset's index."""
        if tileset_name not in request.app.state.tilesets:
            available = list(request.app.state.tilesets.keys())
            raise TilesetNotFoundError(tileset_name, available)

        tileset_info = request.app.state.tilesets[tileset_name]

        if tileset_info["source_type"] != "tar":
            raise HTTPException(
                status_code=400,
                detail=f"Tileset '{tileset_name}' is not a tar-based tileset.",
            )

        tar_manager = request.app.state.tar_manager

        if tileset_name not in tar_manager.index_status:
            return {
                "tileset": tileset_name,
                "status": "not_initialized",
            }

        return {
            "tileset": tileset_name,
            **tar_manager.index_status[tileset_name],
        }

    @app.get(
        "/{tileset_name}/{z}/{x}/{y:path}",
        summary="Serve a single map tile from specified tileset",
    )
    async def get_tile(tileset_name: str, z: int, x: int, y: str, request: Request):
        """
        Serves an individual tile file (e.g., .png, .jpg) from a specific tileset
        based on Z/X/Y coordinates.

        Validation:
          - tileset_name must be a configured tileset
          - z must be within [min_zoom, max_zoom]
          - x must be within [0, 2^z - 1]
          - y filename is sanitized and validated
        """
        # Validate tileset exists
        if tileset_name not in request.app.state.tilesets:
            available = list(request.app.state.tilesets.keys())
            raise TilesetNotFoundError(tileset_name, available)

        metadata = request.app.state.tileset_metadata[tileset_name]
        min_allowed_z = metadata["min_zoom"]
        max_allowed_z = metadata["max_zoom"]

        # Validate zoom level
        if not (min_allowed_z <= z <= max_allowed_z):
            raise InvalidZoomLevelError(z, min_allowed_z, max_allowed_z, tileset_name)

        # Validate X coordinate
        if not (0 <= x < (1 << z)):
            raise InvalidCoordinateError("X", x, z)

        # Sanitize Y: ensure no directory traversal
        y_name = Path(y).name
        if y_name != y:
            raise HTTPException(
                status_code=400,
                detail="Invalid Y coordinate format. Must be a filename (no path components).",
            )

        # Validate Y coordinate if numeric
        stem = Path(y_name).stem
        if stem.isdigit():
            y_int = int(stem)
            if not (0 <= y_int < (1 << z)):
                raise InvalidCoordinateError("Y", y_int, z)

        tileset_info = request.app.state.tilesets[tileset_name]
        source_type = tileset_info["source_type"]

        if source_type == "directory":
            # Directory-based serving
            base_dir = tileset_info["source_path"]

            # Run sync file operations in thread pool to avoid blocking event loop
            tile_path, tried_extensions = await asyncio.to_thread(
                find_tile_path, base_dir, z, x, y_name
            )

            if not tile_path:
                raise TileNotFoundError(tileset_name, z, x, y_name, tried_extensions)

            # Verify file is readable (in thread pool)
            try:
                st = await asyncio.to_thread(tile_path.stat)
            except Exception as e:
                raise TileCorruptedError(
                    tileset_name, z, x, y_name, f"Cannot stat file: {str(e)}"
                )

            etag = f'W/"{st.st_mtime_ns}-{st.st_size}"'

            # Check for conditional request (304 Not Modified)
            if_none_match = request.headers.get("If-None-Match")
            if if_none_match and if_none_match == etag:
                return Response(
                    status_code=304,
                    headers={
                        "ETag": etag,
                        "Cache-Control": "public, max-age=86400, immutable",
                    },
                )

            headers = {
                "Cache-Control": "public, max-age=86400, immutable",
                "ETag": etag,
                "Last-Modified": email.utils.formatdate(st.st_mtime, usegmt=True),
                "X-Tile-Server": "event-optimized",
                "X-Cache-Strategy": "local-event",
                "X-Tileset": tileset_name,
                "X-Source-Type": "directory",
            }

            media_type = media_type_for_suffix(tile_path.suffix)

            try:
                return FileResponse(
                    path=tile_path, headers=headers, media_type=media_type
                )
            except Exception as e:
                raise TileCorruptedError(
                    tileset_name, z, x, y_name, f"Cannot read file: {str(e)}"
                )

        elif source_type == "tar":
            # Tar archive-based serving
            tar_manager = request.app.state.tar_manager
            if_none_match = request.headers.get("If-None-Match")

            try:
                tile_data, media_type, headers = await tar_manager.get_tile_from_tar(
                    tileset_name, z, x, y_name, if_none_match=if_none_match
                )

                # Check for 304 Not Modified (tile_data is None)
                if tile_data is None:
                    return Response(status_code=304, headers=headers)

                return StreamingResponse(
                    BytesIO(tile_data),
                    media_type=media_type,
                    headers=headers,
                )

            except TileServerError:
                # Re-raise our custom exceptions
                raise
            except Exception as e:
                # Catch any unexpected errors
                logger.error(
                    "Unexpected error serving tile from tar: %s", e, exc_info=True
                )
                raise TileCorruptedError(
                    tileset_name, z, x, y_name, f"Unexpected error: {str(e)}"
                )

        else:
            raise HTTPException(
                status_code=500,
                detail=f"Unknown source type: {source_type}",
            )

    return app


def get_app() -> FastAPI:
    """
    Uvicorn factory entry point.

    Reads configuration from environment variables:
        CONFIG_PATH: Path to tileset config file (default: 'tilesets.json').
        TILE_SCAN: '1' to enable startup scan (default), '0' to disable.
        TILE_METADATA_FILE: Path to pre-scanned metadata (from MAIN process).

    Returns:
        Configured FastAPI application instance.
    """
    config_path = os.getenv("CONFIG_PATH", "tilesets.json")
    do_scan = os.getenv("TILE_SCAN", "1") != "0"
    metadata_file = os.getenv("TILE_METADATA_FILE")
    return create_app(config_path, do_scan=do_scan, metadata_file=metadata_file)
