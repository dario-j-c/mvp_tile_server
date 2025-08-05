#!/usr/bin/env python3
"""
Event-optimized high-performance tile server using FastAPI and Uvicorn.
Serves static tile files from a directory structure following {z}/{x}/{y.ext}.

Optimized for local event deployment with looping displays and interactive maps.
Should handle zoom levels 1-25 with efficient multi-worker tile serving.

Usage:
    python3 main.py [path to tiles] -p [port] -b [bind address]

    Or run directly with uvicorn (for production events):
    uvicorn main:get_app --factory --host 127.0.0.1 --port 8000 --workers 4
"""

import argparse
import email.utils
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Tuple

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# ---- Constants ----
SUPPORTED_EXTS: Tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")
DEFAULT_MIN_Z = 1
DEFAULT_MAX_Z = 25

# ---- Logging ----
logger = logging.getLogger("event_tile_server")
_handler = logging.StreamHandler(sys.stdout)
_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
_handler.setFormatter(_formatter)
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


def _scan_tiles(
    tiles_dir: Path, max_samples: int = 5
) -> Tuple[int, List[str], List[int], int, int]:
    """
    Scan the tiles directory to collect metadata.

    Returns:
        tile_count, sample_tiles, zoom_levels_sorted, min_zoom, max_zoom
    """
    tile_count = 0
    sample_tiles: List[str] = []
    zoom_levels_found = set()

    for z_dir in tiles_dir.iterdir():
        if z_dir.is_dir() and z_dir.name.isdigit():
            z = int(z_dir.name)
            zoom_levels_found.add(z)
            for x_dir in z_dir.iterdir():
                if x_dir.is_dir() and x_dir.name.isdigit():
                    x = int(x_dir.name)
                    for tile_file in x_dir.iterdir():
                        if tile_file.is_file():
                            tile_count += 1
                            if len(sample_tiles) < max_samples:
                                sample_tiles.append(f"/{z}/{x}/{tile_file.name}")

    if zoom_levels_found:
        min_zoom = min(zoom_levels_found)
        max_zoom = max(zoom_levels_found)
        zoom_levels_sorted = sorted(zoom_levels_found)
    else:
        min_zoom = DEFAULT_MIN_Z
        max_zoom = DEFAULT_MAX_Z
        zoom_levels_sorted = []

    return tile_count, sample_tiles, zoom_levels_sorted, min_zoom, max_zoom


def _find_tile_path(base_dir: Path, z: int, x: int, y_name: str) -> Optional[Path]:
    """
    Find the tile path by trying the exact file (if ext provided) first,
    then probing other supported extensions.
    """
    y_path = Path(y_name)
    stem = y_path.stem
    ext = y_path.suffix.lower()

    candidates: List[Path] = []

    # Try exact name if extension provided and supported
    if ext in SUPPORTED_EXTS:
        candidates.append(base_dir / str(z) / str(x) / y_name)

    # Probe other extensions
    for e in SUPPORTED_EXTS:
        if e != ext:
            candidates.append(base_dir / str(z) / str(x) / f"{stem}{e}")

    for p in candidates:
        if p.is_file():
            return p
    return None


def _media_type_for_suffix(suffix: str) -> Optional[str]:
    suffix = suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return None


def create_app(tiles_dir: str, do_scan: bool = True) -> FastAPI:
    """
    Create and configure the FastAPI application for serving map tiles.

    Args:
        tiles_dir: The root directory where tile files are stored in a {z}/{x}/{y} structure.
        do_scan: If True, scan the directory on startup for metadata.
    """
    try:
        res_tiles_dir = Path(tiles_dir).resolve()
    except Exception as e:
        logger.error("Error resolving tiles directory '%s': %s", tiles_dir, e)
        logger.warning("Falling back to current directory for tile serving.")
        res_tiles_dir = Path(".").resolve()

    if not res_tiles_dir.is_dir():
        logger.warning(
            "Tiles directory not found at '%s'. Server will start but may not serve tiles correctly.",
            res_tiles_dir,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        logger.info("Event tile server starting - serving from %s", res_tiles_dir)
        tile_count = 0
        sample_tiles: List[str] = []
        zoom_levels_sorted: List[int] = []
        min_zoom = DEFAULT_MIN_Z
        max_zoom = DEFAULT_MAX_Z

        if do_scan:
            logger.info(
                "Pre-calculating tile count and zoom range for event displays (this may take a moment)..."
            )
            try:
                tile_count, sample_tiles, zoom_levels_sorted, min_zoom, max_zoom = (
                    _scan_tiles(res_tiles_dir)
                )
                if zoom_levels_sorted:
                    logger.info(
                        "Found %,d tiles across zoom levels %d-%d",
                        tile_count,
                        min_zoom,
                        max_zoom,
                    )
                else:
                    logger.info(
                        "No tiles detected during scan. Using default zoom range %d-%d",
                        DEFAULT_MIN_Z,
                        DEFAULT_MAX_Z,
                    )
            except Exception as e:
                logger.exception("Error scanning tiles directory: %s", e)
                # Fallback defaults are already set

        app.state.tile_count = tile_count
        app.state.sample_tiles = sample_tiles
        app.state.tiles_dir = res_tiles_dir
        app.state.zoom_levels = zoom_levels_sorted
        app.state.min_zoom = min_zoom
        app.state.max_zoom = max_zoom

        logger.info("Event tile server ready for displays!")
        try:
            yield
        finally:
            # Shutdown
            logger.info("Event tile server shutting down...")

    app = FastAPI(
        title="Event Tile Server",
        description="High-performance tile server optimized for local events with looping displays",
        version="2.2.0-event",
        lifespan=lifespan,
    )

    # Security + CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # For local events only
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.get("/health", summary="Health check endpoint")
    async def health_check():
        return {
            "status": "healthy",
            "service": "event-tile-server",
            "environment": "local-event",
        }

    @app.get("/", summary="Server information and status")
    async def root(request: Request):
        return {
            "service": "Event Tile Server",
            "version": "2.2.0-event",
            "environment": "local-event",
            "tiles_dir": str(request.app.state.tiles_dir),
            "total_tiles": f"{request.app.state.tile_count:,}",
            "zoom_levels": request.app.state.zoom_levels,
            "sample_tile_urls": request.app.state.sample_tiles,
            "health_check_url": "/health",
            "tile_url_format": "/{z}/{x}/{y.ext}",
            "optimizations": [
                "Multi-worker tile serving via Uvicorn",
                "Aggressive client-side caching for looping displays (Cache-Control: immutable)",
                "Local network optimization",
                "Event stability features (e.g., connection limits, keep-alive)",
                "Optional pre-scanned tile metadata on startup",
            ],
            "note": "Optimized for local event deployment with typical zoom levels 1-25.",
        }

    # Use :path converter for y to allow names like 123.png without extra routing.
    # NOTE: may add tiles to path to match old style; it's not important so not implemented
    @app.get("/tiles/{z}/{x}/{y:path}", summary="Serve a single map tile")
    async def get_tile(z: int, x: int, y: str, request: Request):
        """
        Serves an individual tile file (e.g., .png, .jpg) based on Z/X/Y coordinates.

        Validation:
          - z must be within [min_zoom, max_zoom] discovered or defaults.
          - x must be within [0, 2^z - 1].
          - y filename is sanitized and, if numeric stem, y index validated within [0, 2^z - 1].
        """
        min_allowed_z = request.app.state.min_zoom
        max_allowed_z = request.app.state.max_zoom
        if not (min_allowed_z <= z <= max_allowed_z):
            raise HTTPException(
                status_code=404,
                detail=f"Invalid zoom level: {z}. Must be between {min_allowed_z} and {max_allowed_z}.",
            )

        if not (0 <= x < (1 << z)):
            raise HTTPException(
                status_code=404,
                detail=f"Invalid X coordinate {x} for zoom {z}.",
            )

        # Sanitize Y: ensure no directory traversal; only filename allowed.
        y_name = Path(y).name
        if y_name != y:
            raise HTTPException(
                status_code=400,
                detail="Invalid Y coordinate format. Must be a filename (no path components).",
            )

        stem = Path(y_name).stem
        if stem.isdigit():
            y_int = int(stem)
            if not (0 <= y_int < (1 << z)):
                raise HTTPException(
                    status_code=404,
                    detail=f"Invalid Y coordinate {y_int} for zoom {z}.",
                )

        base_dir: Path = request.app.state.tiles_dir
        tile_path = _find_tile_path(base_dir, z, x, y_name)
        if not tile_path:
            raise HTTPException(
                status_code=404, detail=f"Tile not found: /{z}/{x}/{y_name}"
            )

        # Headers: Cache, ETag (weak), Last-Modified
        st = tile_path.stat()
        headers = {
            "Cache-Control": "public, max-age=86400, immutable",
            "ETag": f'W/"{st.st_mtime_ns}-{st.st_size}"',
            "Last-Modified": email.utils.formatdate(st.st_mtime, usegmt=True),
            "X-Tile-Server": "event-optimized",
            "X-Cache-Strategy": "local-event",
        }

        media_type = _media_type_for_suffix(tile_path.suffix)
        return FileResponse(path=tile_path, headers=headers, media_type=media_type)

    return app


def get_app() -> FastAPI:
    """
    Uvicorn factory entry point.

    Respects TILES_PATH and TILE_SCAN env vars:
      - TILES_PATH: path to tiles directory (default '.')
      - TILE_SCAN: '1' to enable startup scan (default '1'), '0' to disable
    """
    tiles_dir = os.getenv("TILES_PATH", ".")
    do_scan = os.getenv("TILE_SCAN", "1") != "0"
    return create_app(tiles_dir, do_scan=do_scan)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Event-optimized tile server using FastAPI/Uvicorn."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Path to tiles directory (default: current directory).",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=8000,
        help="Port to bind to (default: 8000).",
    )
    parser.add_argument(
        "-b",
        "--bind",
        default="127.0.0.1",
        help="Address to bind to (default: 127.0.0.1 for local events).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of worker processes for concurrent tile serving (default: 4).",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development (not recommended during events).",
    )
    parser.add_argument(
        "--event-mode",
        action="store_true",
        help="Enable event production mode with optimized logging and stability.",
    )
    parser.add_argument(
        "--no-scan",
        action="store_true",
        help="Skip startup scan for faster boot (uses default zoom bounds).",
    )
    return parser.parse_args()


def main():
    """Main entry point: starts the event-optimized Uvicorn server."""
    args = parse_arguments()

    # Configure logging level
    if args.event_mode:
        logger.setLevel(logging.WARNING)
    else:
        logger.setLevel(logging.INFO)

    # Export env for worker factory to consume
    os.environ["TILES_PATH"] = args.path
    os.environ["TILE_SCAN"] = "0" if args.no_scan else "1"

    print("\n" + "=" * 50)
    print("Starting Event Tile Server")
    print(f"📁 Serving tiles from: {os.path.abspath(args.path)}")
    print(f"🌐 Listening on: http://{args.bind}:{args.port}")
    print(f"⚡ Using {args.workers} worker processes for optimal tile serving")
    print("🔎 Startup scan: {}".format("disabled" if args.no_scan else "enabled"))
    print("Optimized for: Zoom levels 1-25, looping displays, interactive maps")

    if args.event_mode:
        print("Event production mode: Enhanced stability and minimal logging enabled.")

    print("=" * 50 + "\n")

    # Uvicorn configuration, tuned for event stability and performance
    uvicorn_config = {
        "app": "main:get_app",
        "factory": True,
        "host": args.bind,
        "port": args.port,
        "workers": args.workers,
        "reload": args.reload,
        "loop": "uvloop",
        "http": "httptools",
        "limit_concurrency": 100,
        "backlog": 256,
        "timeout_keep_alive": 300,
        "limit_max_requests": 50000,
    }

    if args.event_mode:
        uvicorn_config.update(
            {
                "log_level": "warning",
                "access_log": False,
            }
        )
    else:
        uvicorn_config.update(
            {
                "log_level": "info",
                "access_log": True,
            }
        )

    try:
        uvicorn.run(**uvicorn_config)
    except KeyboardInterrupt:
        print("\n🛑 Event tile server stopped.")
    except Exception as e:
        print(f"❌ Server encountered a critical error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
