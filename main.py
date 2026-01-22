#!/usr/bin/env python3
"""
Event-optimized high-performance tile server using FastAPI and Uvicorn.
Serves static tile files from multiple configured tile directories.

Supports multiple tilesets configured via JSON file, following {tileset_name}/{z}/{x}/{y.ext}.

Optimized for local event deployment with looping displays and interactive maps.
Should handle zoom levels 1-25 with efficient multi-worker tile serving.

Usage:
    python3 main.py [config_file] -p [port] -b [bind address]

    Or run directly with uvicorn (for production events):
    uvicorn main:get_app --factory --host 0.0.0.0 --port 8000 --workers 4
"""

import argparse
import email.utils
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# ---- Constants ----
SUPPORTED_EXTS: Tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")
DEFAULT_MIN_Z = 1
DEFAULT_MAX_Z = 25

# Valid tileset name pattern: alphanumeric, hyphens, underscores, must not start with digit
VALID_TILESET_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]*$")

# ---- Logging ----
logger = logging.getLogger("event_tile_server")
_handler = logging.StreamHandler(sys.stdout)
_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
_handler.setFormatter(_formatter)
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


def _load_tileset_config(config_path: str) -> Dict[str, str]:
    """
    Load and validate tileset configuration from JSON file.

    Expected format:
    {
        "tilesets": {
            "osm": "/path/to/osm/tiles",
            "satellite": "/path/to/satellite/tiles"
        }
    }

    Returns:
        Dictionary mapping valid tileset names to resolved directory paths

    Raises:
        ValueError: If config is invalid or paths don't exist (with comprehensive error list)
    """
    config_file = Path(config_path)
    if not config_file.exists():
        raise ValueError(f"Config file not found: {config_path}")

    try:
        with open(config_file, "r") as f:
            config_data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in config file: {e}")
    except Exception as e:
        raise ValueError(f"Error reading config file: {e}")

    if "tilesets" not in config_data:
        raise ValueError("Config file must contain 'tilesets' key")

    tilesets = config_data["tilesets"]
    if not isinstance(tilesets, dict):
        raise ValueError("'tilesets' must be a dictionary")

    if not tilesets:
        raise ValueError("At least one tileset must be configured")

    # Collect ALL errors instead of failing on first one
    errors = []
    validated_tilesets = {}

    for name, path in tilesets.items():
        # Validate tileset name
        if not isinstance(name, str) or not VALID_TILESET_NAME.match(name):
            errors.append(
                f"• Invalid tileset name '{name}': Must be alphanumeric + hyphens/underscores, and cannot start with a digit"
            )
            continue  # Skip path validation if name is invalid

        # Validate path type
        if not isinstance(path, str):
            errors.append(
                f"• Tileset '{name}': Path must be a string, got {type(path).__name__}"
            )
            continue

        # Validate and resolve path
        try:
            resolved_path = Path(path).resolve()
        except Exception as e:
            errors.append(f"• Tileset '{name}': Error resolving path '{path}': {e}")
            continue

        # Check path exists
        if not resolved_path.exists():
            errors.append(f"• Tileset '{name}': Path does not exist: {resolved_path}")
            continue

        # Check path is directory
        if not resolved_path.is_dir():
            errors.append(
                f"• Tileset '{name}': Path is not a directory: {resolved_path}"
            )
            continue

        # If we get here, this tileset is valid
        validated_tilesets[name] = str(resolved_path)

    # If there were any errors, raise them all at once
    if errors:
        error_count = len(errors)
        error_summary = f"Found {error_count} configuration error{'s' if error_count > 1 else ''}:\n\n"
        detailed_errors = "\n".join(errors)

        if validated_tilesets:
            valid_count = len(validated_tilesets)
            footer = f"\n\n✓ {valid_count} tileset{'s' if valid_count > 1 else ''} validated successfully: {', '.join(validated_tilesets.keys())}"
        else:
            footer = "\n\n✗ No valid tilesets found"

        raise ValueError(error_summary + detailed_errors + footer)

    return validated_tilesets


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
                            # NOTE: Sample tiles now include tileset name in path
                            if len(sample_tiles) < max_samples:
                                # We'll need tileset name from caller context
                                sample_tiles.append(f"{z}/{x}/{tile_file.name}")

    if zoom_levels_found:
        min_zoom = min(zoom_levels_found)
        max_zoom = max(zoom_levels_found)
        zoom_levels_sorted = sorted(zoom_levels_found)
    else:
        min_zoom = DEFAULT_MIN_Z
        max_zoom = DEFAULT_MAX_Z
        zoom_levels_sorted = []

    return tile_count, sample_tiles, zoom_levels_sorted, min_zoom, max_zoom


def _scan_all_tilesets(tilesets: Dict[str, str]) -> Dict[str, Dict]:
    """
    Scan all configured tilesets for metadata.

    Returns:
        Dictionary with tileset metadata for each configured tileset
    """
    all_metadata = {}

    for tileset_name, tileset_path in tilesets.items():
        logger.info("Scanning tileset '%s' at %s", tileset_name, tileset_path)
        try:
            tile_count, sample_tiles, zoom_levels, min_zoom, max_zoom = _scan_tiles(
                Path(tileset_path)
            )

            # Add tileset name to sample tile paths
            sample_tiles_with_tileset = [
                f"/{tileset_name}/{tile}" for tile in sample_tiles
            ]

            all_metadata[tileset_name] = {
                "path": tileset_path,
                "tile_count": tile_count,
                "sample_tiles": sample_tiles_with_tileset,
                "zoom_levels": zoom_levels,
                "min_zoom": min_zoom,
                "max_zoom": max_zoom,
            }

            if zoom_levels:
                logger.info(
                    "Tileset '%s': %d tiles, zoom levels %d-%d",
                    tileset_name,
                    tile_count,
                    min_zoom,
                    max_zoom,
                )
            else:
                logger.info(
                    "Tileset '%s': %d tiles, no zoom structure detected",
                    tileset_name,
                    tile_count,
                )

        except Exception as e:
            logger.error("Error scanning tileset '%s': %s", tileset_name, e)
            all_metadata[tileset_name] = {
                "path": tileset_path,
                "tile_count": 0,
                "sample_tiles": [],
                "zoom_levels": [],
                "min_zoom": DEFAULT_MIN_Z,
                "max_zoom": DEFAULT_MAX_Z,
                "error": str(e),
            }

    return all_metadata


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


def create_app(config_path: str, do_scan: bool = True) -> FastAPI:
    """
    Create and configure the FastAPI application for serving map tiles.

    Args:
        config_path: Path to JSON configuration file containing tileset definitions
        do_scan: If True, scan directories on startup for metadata.
    """

    # Load and validate tileset configuration
    try:
        tilesets = _load_tileset_config(config_path)
        logger.info("Loaded %d tilesets from config", len(tilesets))
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        raise

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        logger.info("Event tile server starting with %d tilesets", len(tilesets))

        tileset_metadata = {}
        if do_scan:
            logger.info("Pre-calculating tile metadata for all tilesets...")
            tileset_metadata = _scan_all_tilesets(tilesets)
        else:
            # Create minimal metadata without scanning
            for name, path in tilesets.items():
                tileset_metadata[name] = {
                    "path": path,
                    "tile_count": 0,
                    "sample_tiles": [],
                    "zoom_levels": [],
                    "min_zoom": DEFAULT_MIN_Z,
                    "max_zoom": DEFAULT_MAX_Z,
                }

        app.state.tilesets = tilesets
        app.state.tileset_metadata = tileset_metadata

        logger.info("Event tile server ready for displays!")
        try:
            yield
        finally:
            # Shutdown
            logger.info("Event tile server shutting down...")

    app = FastAPI(
        title="Multi-Tileset Event Tile Server",
        description="High-performance tile server with multiple tileset support, optimized for local events",
        version="2.3.0-event",
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
            "service": "multi-tileset-event-tile-server",
            "environment": "local-event",
        }

    @app.get("/", summary="Server information and status")
    async def root(request: Request):
        tilesets_info = {}
        total_tiles = 0

        for name, metadata in request.app.state.tileset_metadata.items():
            tilesets_info[name] = {
                "path": metadata["path"],
                "tile_count": metadata["tile_count"],
                "zoom_levels": metadata["zoom_levels"],
                "sample_tiles": metadata["sample_tiles"][
                    :3
                ],  # Limit samples in summary
            }
            total_tiles += metadata["tile_count"]

        return {
            "service": "Multi-Tileset Event Tile Server",
            "version": "2.3.0-event",
            "environment": "local-event",
            "tilesets": tilesets_info,
            "total_tiles": f"{total_tiles:,}",
            "health_check_url": "/health",
            "tileset_detail_url": "/tilesets/{tileset_name}",
            "tile_url_format": "/{tileset_name}/{z}/{x}/{y.ext}",
            "optimizations": [
                "Multi-worker tile serving via Uvicorn",
                "Multiple tileset support with independent caching",
                "Aggressive client-side caching for looping displays (Cache-Control: immutable)",
                "Local network optimization",
                "Event stability features (e.g., connection limits, keep-alive)",
                "Optional pre-scanned tile metadata on startup",
            ],
            "note": "Optimized for local event deployment with typical zoom levels 1-25.",
        }

    @app.get(
        "/tilesets/{tileset_name}",
        summary="Get detailed information about a specific tileset",
    )
    async def get_tileset_info(tileset_name: str, request: Request):
        """Get detailed metadata for a specific tileset."""
        if tileset_name not in request.app.state.tileset_metadata:
            raise HTTPException(
                status_code=404,
                detail=f"Tileset '{tileset_name}' not found. Available tilesets: {list(request.app.state.tilesets.keys())}",
            )

        metadata = request.app.state.tileset_metadata[tileset_name]
        return {
            "name": tileset_name,
            "path": metadata["path"],
            "tile_count": f"{metadata['tile_count']:,}",
            "zoom_levels": metadata["zoom_levels"],
            "zoom_range": f"{metadata['min_zoom']}-{metadata['max_zoom']}"
            if metadata["zoom_levels"]
            else "unknown",
            "sample_tiles": metadata["sample_tiles"],
            "tile_url_format": f"/{tileset_name}/{{z}}/{{x}}/{{y.ext}}",
        }

    @app.get(
        "/{tileset_name}/{z}/{x}/{y:path}",
        summary="Serve a single map tile from specified tileset",
    )
    async def get_tile(tileset_name: str, z: int, x: int, y: str, request: Request):
        """
        Serves an individual tile file (e.g., .png, .jpg) from a specific tileset based on Z/X/Y coordinates.

        Validation:
          - tileset_name must be a configured tileset
          - z must be within [min_zoom, max_zoom] discovered or defaults.
          - x must be within [0, 2^z - 1].
          - y filename is sanitized and, if numeric stem, y index validated within [0, 2^z - 1].
        """
        # Validate tileset exists
        if tileset_name not in request.app.state.tilesets:
            raise HTTPException(
                status_code=404,
                detail=f"Tileset '{tileset_name}' not found. Available tilesets: {list(request.app.state.tilesets.keys())}",
            )

        metadata = request.app.state.tileset_metadata[tileset_name]
        min_allowed_z = metadata["min_zoom"]
        max_allowed_z = metadata["max_zoom"]

        if not (min_allowed_z <= z <= max_allowed_z):
            raise HTTPException(
                status_code=404,
                detail=f"Invalid zoom level: {z}. Must be between {min_allowed_z} and {max_allowed_z} for tileset '{tileset_name}'.",
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

        base_dir = Path(request.app.state.tilesets[tileset_name])
        tile_path = _find_tile_path(base_dir, z, x, y_name)
        if not tile_path:
            raise HTTPException(
                status_code=404,
                detail=f"Tile not found: /{tileset_name}/{z}/{x}/{y_name}",
            )

        # Headers: Cache, ETag (weak), Last-Modified
        st = tile_path.stat()
        headers = {
            "Cache-Control": "public, max-age=86400, immutable",
            "ETag": f'W/"{st.st_mtime_ns}-{st.st_size}"',
            "Last-Modified": email.utils.formatdate(st.st_mtime, usegmt=True),
            "X-Tile-Server": "event-optimized",
            "X-Cache-Strategy": "local-event",
            "X-Tileset": tileset_name,
        }

        media_type = _media_type_for_suffix(tile_path.suffix)
        return FileResponse(path=tile_path, headers=headers, media_type=media_type)

    return app


def get_app() -> FastAPI:
    """
    Uvicorn factory entry point.

    Respects CONFIG_PATH and TILE_SCAN env vars:
      - CONFIG_PATH: path to tileset config file (default 'tilesets.json')
      - TILE_SCAN: '1' to enable startup scan (default '1'), '0' to disable
    """
    config_path = os.getenv("CONFIG_PATH", "tilesets.json")
    do_scan = os.getenv("TILE_SCAN", "1") != "0"
    return create_app(config_path, do_scan=do_scan)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Event-optimized multi-tileset tile server using FastAPI/Uvicorn."
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="tilesets.json",
        help="Path to tileset configuration JSON file (default: tilesets.json).",
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
        default="0.0.0.0",
        help="Address to bind to (default: 0.0.0.0 for local events).",
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

    # Validate configuration early to avoid worker crashes
    try:
        logger.info("Validating configuration...")
        tilesets = _load_tileset_config(args.config)
        logger.info(
            "✓ Configuration valid: %d tilesets loaded successfully", len(tilesets)
        )
        for name, path in tilesets.items():
            logger.info("  - %s: %s", name, path)
    except ValueError as e:
        print(f"\n❌ Configuration Validation Failed:")
        print("=" * 60)
        print(str(e))
        print("=" * 60)
        print("\nPlease fix the above issues and try again.")
        print("💡 Tip: Use absolute paths to avoid path resolution issues.")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error loading configuration: {e}")
        sys.exit(1)

    # Export env for worker factory to consume
    os.environ["CONFIG_PATH"] = args.config
    os.environ["TILE_SCAN"] = "0" if args.no_scan else "1"

    print("\n" + "=" * 50)
    print("Starting Multi-Tileset Event Tile Server")
    print(f"⚙️  Loading configuration from: {os.path.abspath(args.config)}")
    print(f"🌐 Listening on: http://{args.bind}:{args.port}")
    print(f"⚡ Using {args.workers} worker processes for optimal tile serving")
    print("🔎 Startup scan: {}".format("disabled" if args.no_scan else "enabled"))
    print(
        "Optimized for: Multiple tilesets, zoom levels 1-25, looping displays, interactive maps"
    )

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
