"""Configuration loading and validation for the tile server."""

import json
import logging
import re
import tarfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, TypedDict

from typing_extensions import NotRequired

from app.utils import detect_tar_compression, is_tar_file

logger = logging.getLogger("event_tile_server")


class TilesetConfig(TypedDict):
    """Type definition for tileset configuration dictionary."""

    source_type: str  # "directory" or "tar"
    source_path: Path
    base_path: NotRequired[str]  # Only for tar sources, optional


# Valid tileset name pattern: alphanumeric, hyphens, underscores, must not start with digit
VALID_TILESET_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]*$")
TILE_PATTERN = re.compile(r"^(.*/)?(\d+)/(\d+)/(\d+\.\w+)$")
DEFAULT_MIN_Z = 1
DEFAULT_MAX_Z = 25


def auto_detect_base_path(tar_path: Path, max_members: int = 100) -> Optional[str]:
    """
    Auto-detect base_path by scanning first N members for tile patterns.

    Args:
        tar_path: Path to the tar archive.
        max_members: Maximum number of members to scan.

    Returns:
        Detected base_path or None if tiles are at root.
    """
    detected_bases: List[str] = []

    try:
        with tarfile.open(tar_path, "r:*") as tar:
            for i, member in enumerate(tar):
                if i >= max_members:
                    break

                if member.isfile():
                    match = TILE_PATTERN.match(member.name)
                    if match and match.group(1):
                        base = match.group(1).rstrip("/")
                        detected_bases.append(base)

        # Return most common base path
        if detected_bases:
            most_common = Counter(detected_bases).most_common(1)[0][0]
            return most_common

    except Exception as e:
        logger.warning("Error auto-detecting base_path for %s: %s", tar_path, e)

    return None


def load_tileset_config(
    config_path: str, show_warnings: bool = True
) -> Dict[str, TilesetConfig]:
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
    errors: List[str] = []
    warnings: List[str] = []
    validated_tilesets: Dict[str, TilesetConfig] = {}

    for name, config_value in tilesets.items():
        # Validate tileset name
        if not isinstance(name, str) or not VALID_TILESET_NAME.match(name):
            errors.append(
                f"• Invalid tileset name '{name}': Must be alphanumeric + hyphens/underscores, and cannot start with a digit"
            )
            continue

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
        if is_tar_file(resolved_path):
            # Validate tar file
            try:
                compression = detect_tar_compression(resolved_path)

                # Warn about compressed tars
                if compression in ["gzip", "bzip2", "xz"]:
                    warnings.append(
                        f"• Tileset '{name}': Using {compression}-compressed tar. "
                        "This will be 5-10x slower than uncompressed. "
                        "Consider using .tar for better performance."
                    )

                # Auto-detect base_path if not provided
                if base_path is None:
                    detected = auto_detect_base_path(resolved_path)
                    if detected:
                        base_path = detected
                        logger.info(
                            "Tileset '%s': Auto-detected base_path: '%s'",
                            name,
                            base_path,
                        )

                # Validate tar can be opened
                with tarfile.open(resolved_path, "r:*") as _tar:
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

    # Log warnings if any (can be suppressed for worker processes)
    if warnings and show_warnings:
        logger.warning("Configuration warnings:")
        for warning in warnings:
            logger.warning(warning)

    return validated_tilesets


def scan_tiles(
    source_path: Path,
    source_type: str,
    base_path: str = "",
    max_samples: int = 5,
    timeout_seconds: int = 10,
) -> Tuple[int, List[str], List[int], int, int]:
    """
    Scan tiles from directory or tar archive to collect metadata.

    Args:
        source_path: Path to directory or tar file.
        source_type: Either "directory" or "tar".
        base_path: For tar files, optional base path inside archive.
        max_samples: Maximum sample tiles to collect.
        timeout_seconds: Maximum time to spend scanning (fail-fast).

    Returns:
        Tuple of (tile_count, sample_tiles, zoom_levels_sorted, min_zoom, max_zoom).
    """
    start_time = time.time()
    tile_count = 0
    sample_tiles: List[str] = []
    zoom_levels_found: Set[int] = set()

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


def scan_all_tilesets(
    tilesets: Dict[str, TilesetConfig],
) -> Dict[str, Dict[str, Any]]:
    """
    Scan all configured tilesets for metadata.

    Args:
        tilesets: Dictionary from load_tileset_config with metadata for each tileset.

    Returns:
        Dictionary mapping tileset names to their metadata dictionaries.
    """
    all_metadata: Dict[str, Dict[str, Any]] = {}

    for tileset_name, tileset_info in tilesets.items():
        source_path: Path = tileset_info["source_path"]
        source_type: str = tileset_info["source_type"]
        base_path: str = tileset_info.get("base_path", "")

        logger.info(
            "Scanning tileset '%s' (%s) at %s",
            tileset_name,
            source_type,
            source_path,
        )

        try:
            tile_count, sample_tiles, zoom_levels, min_zoom, max_zoom = scan_tiles(
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
