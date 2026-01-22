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
import tarfile
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

# ---- Constants ----
SUPPORTED_EXTS: Tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")
TAR_EXTENSIONS: Tuple[str, ...] = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
)
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


# ---- Tar Archive Support ----


def _is_tar_file(path: Path) -> bool:
    """Check if a path points to a tar archive."""
    if not path.is_file():
        return False

    return any(str(path).endswith(ext) for ext in TAR_EXTENSIONS)


def _detect_tar_compression(tar_path: Path) -> str:
    """Detect compression type of tar archive."""
    path_str = str(tar_path)
    if path_str.endswith((".tar.gz", ".tgz")):
        return "gzip"
    elif path_str.endswith((".tar.bz2", ".tbz2")):
        return "bzip2"
    elif path_str.endswith((".tar.xz", ".txz")):
        return "xz"
    elif path_str.endswith(".tar"):
        return "uncompressed"
    return "unknown"


def _build_tar_index(
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


def _auto_detect_base_path(tar_path: Path, max_members: int = 100) -> Optional[str]:
    """
    Auto-detect base_path by scanning first N members for tile patterns.

    Returns:
        Detected base_path or None if tiles are at root
    """
    import re

    tile_pattern = re.compile(r"^(.*/)?(\d+)/(\d+)/(\d+\.\w+)$")
    detected_bases = []

    try:
        with tarfile.open(tar_path, "r:*") as tar:
            for i, member in enumerate(tar):
                if i >= max_members:
                    break

                if member.isfile():
                    match = tile_pattern.match(member.name)
                    if match and match.group(1):
                        base = match.group(1).rstrip("/")
                        detected_bases.append(base)

        # Return most common base path
        if detected_bases:
            from collections import Counter

            most_common = Counter(detected_bases).most_common(1)[0][0]
            return most_common

    except Exception as e:
        logger.warning("Error auto-detecting base_path for %s: %s", tar_path, e)

    return None


def _load_tileset_config(config_path: str) -> Dict[str, Dict[str, Union[str, Path]]]:
    """
    Load and validate tileset configuration from JSON file.

    Expected format (supports both directory and tar sources):
    {
        "tilesets": {
            "osm": "/path/to/osm/tiles",                    # Directory
            "satellite": "/path/to/satellite.tar",          # Tar (auto-detect base_path)
            "topo": {                                        # Tar with explicit base_path
                "source": "/path/to/topo.tar.gz",
                "base_path": "tiles"
            }
        }
    }

    Returns:
        Dictionary mapping tileset names to metadata dictionaries:
        {
            "tileset_name": {
                "source_type": "directory" | "tar",
                "source_path": Path object,
                "base_path": str (for tar only, optional)
            }
        }

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
    warnings = []
    validated_tilesets = {}

    for name, config_value in tilesets.items():
        # Validate tileset name
        if not isinstance(name, str) or not VALID_TILESET_NAME.match(name):
            errors.append(
                f"• Invalid tileset name '{name}': Must be alphanumeric + hyphens/underscores, and cannot start with a digit"
            )
            continue  # Skip path validation if name is invalid

        # Parse config value (can be string path or dict with source/base_path)
        source_path_str = None
        base_path = None

        if isinstance(config_value, str):
            source_path_str = config_value
        elif isinstance(config_value, dict):
            if "source" not in config_value:
                errors.append(
                    f"• Tileset '{name}': Dictionary config must have 'source' key"
                )
                continue
            source_path_str = config_value["source"]
            base_path = config_value.get("base_path", None)

            if not isinstance(source_path_str, str):
                errors.append(
                    f"• Tileset '{name}': 'source' must be a string, got {type(source_path_str).__name__}"
                )
                continue

            if base_path is not None and not isinstance(base_path, str):
                errors.append(
                    f"• Tileset '{name}': 'base_path' must be a string, got {type(base_path).__name__}"
                )
                continue
        else:
            errors.append(
                f"• Tileset '{name}': Config must be a string path or dict, got {type(config_value).__name__}"
            )
            continue

        # Validate and resolve path
        try:
            resolved_path = Path(source_path_str).resolve()
        except Exception as e:
            errors.append(
                f"• Tileset '{name}': Error resolving path '{source_path_str}': {e}"
            )
            continue

        # Check path exists
        if not resolved_path.exists():
            errors.append(f"• Tileset '{name}': Path does not exist: {resolved_path}")
            continue

        # Determine if this is a tar file or directory
        is_tar = _is_tar_file(resolved_path)

        if is_tar:
            # Validate tar file
            try:
                compression = _detect_tar_compression(resolved_path)

                # Warn about compressed tars
                if compression in ["gzip", "bzip2", "xz"]:
                    warnings.append(
                        f"• Tileset '{name}': Using {compression}-compressed tar. "
                        "This will be 5-10x slower than uncompressed. "
                        "Consider using .tar for better performance."
                    )

                # Auto-detect base_path if not provided
                if base_path is None:
                    detected = _auto_detect_base_path(resolved_path)
                    if detected:
                        base_path = detected
                        logger.info(
                            "Tileset '%s': Auto-detected base_path: '%s'",
                            name,
                            base_path,
                        )

                # Validate tar can be opened
                with tarfile.open(resolved_path, "r:*") as tar:
                    pass  # Just test opening

                validated_tilesets[name] = {
                    "source_type": "tar",
                    "source_path": resolved_path,
                    "base_path": base_path or "",
                }

            except Exception as e:
                errors.append(
                    f"• Tileset '{name}': Error reading tar file '{resolved_path}': {e}"
                )
                continue

        else:
            # Validate directory
            if not resolved_path.is_dir():
                errors.append(
                    f"• Tileset '{name}': Path is not a directory or tar file: {resolved_path}"
                )
                continue

            # Ignore base_path for directories
            if base_path:
                warnings.append(
                    f"• Tileset '{name}': 'base_path' is ignored for directory sources"
                )

            validated_tilesets[name] = {
                "source_type": "directory",
                "source_path": resolved_path,
            }

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

    # Log warnings if any
    if warnings:
        logger.warning("Configuration warnings:")
        for warning in warnings:
            logger.warning(warning)

    return validated_tilesets


def _scan_tiles(
    source_path: Path,
    source_type: str,
    base_path: str = "",
    max_samples: int = 5,
    timeout_seconds: int = 10,
) -> Tuple[int, List[str], List[int], int, int]:
    """
    Scan tiles from directory or tar archive to collect metadata.

    Args:
        source_path: Path to directory or tar file
        source_type: "directory" or "tar"
        base_path: For tar files, optional base path inside archive
        max_samples: Maximum sample tiles to collect
        timeout_seconds: Maximum time to spend scanning (fail-fast)

    Returns:
        tile_count, sample_tiles, zoom_levels_sorted, min_zoom, max_zoom
    """
    import time

    start_time = time.time()
    tile_count = 0
    sample_tiles: List[str] = []
    zoom_levels_found = set()

    if source_type == "directory":
        # Original directory scanning logic
        for z_dir in source_path.iterdir():
            if time.time() - start_time > timeout_seconds:
                logger.warning(
                    "Scan timeout reached for %s after %d tiles",
                    source_path.name,
                    tile_count,
                )
                break

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
                                    sample_tiles.append(f"{z}/{x}/{tile_file.name}")

    elif source_type == "tar":
        # Tar archive scanning logic
        normalized_base = base_path.strip("/") + "/" if base_path else ""

        try:
            with tarfile.open(source_path, "r:*") as tar:
                for member in tar:
                    if time.time() - start_time > timeout_seconds:
                        logger.warning(
                            "Scan timeout reached for %s after %d tiles",
                            source_path.name,
                            tile_count,
                        )
                        break

                    if not member.isfile():
                        continue

                    # Remove base_path prefix if present
                    member_path = member.name
                    if normalized_base and member_path.startswith(normalized_base):
                        member_path = member_path[len(normalized_base) :]

                    # Check if this matches tile pattern: z/x/y.ext
                    parts = member_path.split("/")
                    if len(parts) >= 3:
                        z_str, x_str, y_name = parts[-3], parts[-2], parts[-1]
                        if z_str.isdigit() and x_str.isdigit():
                            z = int(z_str)
                            x = int(x_str)
                            zoom_levels_found.add(z)
                            tile_count += 1

                            if len(sample_tiles) < max_samples:
                                sample_tiles.append(f"{z}/{x}/{y_name}")

        except Exception as e:
            logger.error("Error scanning tar file %s: %s", source_path, e)

    if zoom_levels_found:
        min_zoom = min(zoom_levels_found)
        max_zoom = max(zoom_levels_found)
        zoom_levels_sorted = sorted(zoom_levels_found)
    else:
        min_zoom = DEFAULT_MIN_Z
        max_zoom = DEFAULT_MAX_Z
        zoom_levels_sorted = []

    return tile_count, sample_tiles, zoom_levels_sorted, min_zoom, max_zoom


def _scan_all_tilesets(
    tilesets: Dict[str, Dict[str, Union[str, Path]]],
) -> Dict[str, Dict]:
    """
    Scan all configured tilesets for metadata.

    Args:
        tilesets: Dictionary from _load_tileset_config with metadata for each tileset

    Returns:
        Dictionary with tileset metadata for each configured tileset
    """
    all_metadata = {}

    for tileset_name, tileset_info in tilesets.items():
        source_path = tileset_info["source_path"]
        source_type = tileset_info["source_type"]
        base_path = tileset_info.get("base_path", "")

        logger.info(
            "Scanning tileset '%s' (%s) at %s",
            tileset_name,
            source_type,
            source_path,
        )

        try:
            tile_count, sample_tiles, zoom_levels, min_zoom, max_zoom = _scan_tiles(
                source_path=source_path,
                source_type=source_type,
                base_path=base_path,
            )

            # Add tileset name to sample tile paths
            sample_tiles_with_tileset = [
                f"/{tileset_name}/{tile}" for tile in sample_tiles
            ]

            all_metadata[tileset_name] = {
                "source_path": str(source_path),
                "source_type": source_type,
                "base_path": base_path,
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
                "source_path": str(source_path),
                "source_type": source_type,
                "base_path": base_path,
                "tile_count": 0,
                "sample_tiles": [],
                "zoom_levels": [],
                "min_zoom": DEFAULT_MIN_Z,
                "max_zoom": DEFAULT_MAX_Z,
                "error": str(e),
            }

    return all_metadata


def _find_tile_in_tar_index(
    tar_index: Dict[str, tarfile.TarInfo], z: int, x: int, y_name: str
) -> Optional[tarfile.TarInfo]:
    """
    Find tile member in pre-built tar index by trying exact file first,
    then probing other supported extensions.

    Returns:
        TarInfo object if found, None otherwise
    """
    y_path = Path(y_name)
    stem = y_path.stem
    ext = y_path.suffix.lower()

    candidates: List[str] = []

    # Try exact name if extension provided and supported
    if ext in SUPPORTED_EXTS:
        candidates.append(f"{z}/{x}/{y_name}")

    # Probe other extensions
    for e in SUPPORTED_EXTS:
        if e != ext:
            candidates.append(f"{z}/{x}/{stem}{e}")

    for candidate_key in candidates:
        if candidate_key in tar_index:
            return tar_index[candidate_key]

    return None


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

        # Build tar indexes and open file handles for tar-based tilesets
        tar_handles = {}
        tar_indexes = {}

        for tileset_name, tileset_info in tilesets.items():
            if tileset_info["source_type"] == "tar":
                source_path = tileset_info["source_path"]
                base_path = tileset_info.get("base_path", "")

                logger.info("Building tar index for tileset '%s'...", tileset_name)
                try:
                    # Build index
                    member_index, zoom_levels = _build_tar_index(source_path, base_path)
                    tar_indexes[tileset_name] = member_index

                    # Open persistent file handle for this worker
                    tar_handle = tarfile.open(source_path, "r:*")
                    tar_handles[tileset_name] = tar_handle

                    logger.info(
                        "Tileset '%s': Indexed %d tiles from tar archive",
                        tileset_name,
                        len(member_index),
                    )
                except Exception as e:
                    logger.error(
                        "Failed to build tar index for tileset '%s': %s",
                        tileset_name,
                        e,
                    )
                    raise

        tileset_metadata = {}
        if do_scan:
            logger.info("Pre-calculating tile metadata for all tilesets...")
            tileset_metadata = _scan_all_tilesets(tilesets)
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
        app.state.tar_handles = tar_handles
        app.state.tar_indexes = tar_indexes

        logger.info("Event tile server ready for displays!")
        try:
            yield
        finally:
            # Shutdown - close tar file handles
            logger.info("Event tile server shutting down...")
            for tileset_name, tar_handle in tar_handles.items():
                try:
                    tar_handle.close()
                    logger.info("Closed tar handle for tileset '%s'", tileset_name)
                except Exception as e:
                    logger.error(
                        "Error closing tar handle for tileset '%s': %s",
                        tileset_name,
                        e,
                    )

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
                "source_type": metadata["source_type"],
                "source_path": metadata["source_path"],
                "tile_count": metadata["tile_count"],
                "zoom_levels": metadata["zoom_levels"],
                "sample_tiles": metadata["sample_tiles"][
                    :3
                ],  # Limit samples in summary
            }
            total_tiles += metadata["tile_count"]

        return {
            "service": "Multi-Tileset Event Tile Server",
            "version": "2.4.0-event",
            "environment": "local-event",
            "tilesets": tilesets_info,
            "total_tiles": f"{total_tiles:,}",
            "health_check_url": "/health",
            "tileset_detail_url": "/tilesets/{tileset_name}",
            "tile_url_format": "/{tileset_name}/{z}/{x}/{y.ext}",
            "optimizations": [
                "Multi-worker tile serving via Uvicorn",
                "Multiple tileset support with independent caching",
                "Support for directory and tar archive sources",
                "Tar archive streaming (no disk extraction required)",
                "Aggressive client-side caching for looping displays (Cache-Control: immutable)",
                "Local network optimization",
                "Event stability features (e.g., connection limits, keep-alive)",
                "Optional pre-scanned tile metadata on startup",
            ],
            "note": "Optimized for local event deployment with typical zoom levels 1-25. Supports both directory and tar archive tile sources.",
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

        # Add base_path info for tar sources
        if metadata["source_type"] == "tar" and metadata.get("base_path"):
            response["base_path"] = metadata["base_path"]

        return response

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

        tileset_info = request.app.state.tilesets[tileset_name]
        source_type = tileset_info["source_type"]

        if source_type == "directory":
            # Original directory-based serving
            base_dir = tileset_info["source_path"]
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
                "X-Source-Type": "directory",
            }

            media_type = _media_type_for_suffix(tile_path.suffix)
            return FileResponse(path=tile_path, headers=headers, media_type=media_type)

        elif source_type == "tar":
            # Tar archive-based serving
            tar_index = request.app.state.tar_indexes.get(tileset_name)
            tar_handle = request.app.state.tar_handles.get(tileset_name)

            if not tar_index or not tar_handle:
                raise HTTPException(
                    status_code=500,
                    detail=f"Tar index not available for tileset '{tileset_name}'",
                )

            # Find tile in index
            tile_member = _find_tile_in_tar_index(tar_index, z, x, y_name)
            if not tile_member:
                raise HTTPException(
                    status_code=404,
                    detail=f"Tile not found: /{tileset_name}/{z}/{x}/{y_name}",
                )

            # Extract tile data from tar
            try:
                file_obj = tar_handle.extractfile(tile_member)
                if not file_obj:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to extract tile from tar archive",
                    )

                tile_data = file_obj.read()
                file_obj.close()
            except Exception as e:
                logger.error(
                    "Error extracting tile from tar for tileset '%s': %s",
                    tileset_name,
                    e,
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Error reading tile from tar archive: {str(e)}",
                )

            # Headers for tar-based tiles
            headers = {
                "Cache-Control": "public, max-age=86400, immutable",
                "ETag": f'W/"{tile_member.mtime}-{tile_member.size}"',
                "Last-Modified": email.utils.formatdate(tile_member.mtime, usegmt=True),
                "X-Tile-Server": "event-optimized",
                "X-Cache-Strategy": "local-event",
                "X-Tileset": tileset_name,
                "X-Source-Type": "tar",
            }

            # Determine media type from file extension
            tile_path = Path(tile_member.name)
            media_type = _media_type_for_suffix(tile_path.suffix)

            # Stream response from memory
            return StreamingResponse(
                BytesIO(tile_data),
                media_type=media_type,
                headers=headers,
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
