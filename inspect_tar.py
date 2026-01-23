#!/usr/bin/env python3
"""
Tar Archive Inspector for Tile Server

Quick utility to inspect tar archives and identify tile structure.
Helps configure the tile server with appropriate base_path for tar-based tilesets.

Usage:
    python3 inspect_tar.py <path_to_tar_file>
    python3 inspect_tar.py <path_to_tar_file> --timeout 60

Features:
- Detects compression type (tar, tar.gz, tar.bz2, tar.xz)
- Shows top-level directory structure
- Identifies tile patterns ({z}/{x}/{y.ext})
- Suggests configuration for tilesets.json
- Fails fast with configurable timeout
"""

import argparse
import re
import sys
import tarfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set


class TarInspectionTimeout(Exception):
    """Raised when tar inspection takes too long."""

    pass


def detect_compression(tar_path: Path) -> str:
    """Detect compression type of tar archive."""
    if tar_path.suffix == ".tgz" or tar_path.name.endswith(".tar.gz"):
        return "gzip"
    elif tar_path.name.endswith(".tar.bz2") or tar_path.suffix == ".tbz2":
        return "bzip2"
    elif tar_path.name.endswith(".tar.xz") or tar_path.suffix == ".txz":
        return "xz"
    elif tar_path.suffix == ".tar":
        return "uncompressed"
    else:
        # Try to detect by opening
        try:
            with tarfile.open(tar_path, "r:*") as tar:
                if hasattr(tar, "fileobj"):
                    return "unknown (auto-detected)"
        except Exception:
            pass
        return "unknown"


def format_size(size_bytes: int) -> str:
    """Format byte size to human-readable string."""
    size: float = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def inspect_tar_structure(
    tar_path: Path, max_members: int = 1000, timeout_seconds: int = 60
) -> Dict:
    """
    Inspect tar archive structure with fail-fast timeout.

    Returns:
        Dictionary with structure info, tile samples, and recommendations
    """
    start_time = time.time()

    top_level_dirs: Set[str] = set()
    tile_samples: List[str] = []
    zoom_levels: Set[int] = set()
    total_members = 0
    total_size = 0
    extensions: Dict[str, int] = defaultdict(int)

    # Pattern to match tile paths: {something}/{z}/{x}/{y.ext}
    tile_pattern = re.compile(r"^(.*/)?(\d+)/(\d+)/(\d+\.\w+)$")

    try:
        with tarfile.open(tar_path, "r:*") as tar:
            for member in tar:
                # Check timeout
                if time.time() - start_time > timeout_seconds:
                    raise TarInspectionTimeout(
                        f"Inspection timed out after {timeout_seconds}s. "
                        f"Scanned {total_members} members."
                    )

                # Check member limit
                if total_members >= max_members:
                    print(
                        f"\n⚠  Reached member limit ({max_members}). "
                        "Stopping scan for speed.\n"
                    )
                    break

                total_members += 1
                total_size += member.size

                # Track top-level directories
                parts = member.name.split("/")
                if len(parts) > 1 and member.isdir():
                    top_level_dirs.add(parts[0])

                # Check if this looks like a tile
                if member.isfile():
                    match = tile_pattern.match(member.name)
                    if match:
                        # Groups: (1) base_path, (2) z, (3) x, (4) y_name
                        z = int(match.group(2))
                        y_name = match.group(4)

                        zoom_levels.add(z)

                        # Collect samples
                        if len(tile_samples) < 10:
                            tile_samples.append(member.name)

                        # Track extension
                        ext = Path(y_name).suffix.lower()
                        extensions[ext] += 1

    except TarInspectionTimeout:
        raise
    except Exception as e:
        return {
            "error": str(e),
            "members_scanned": total_members,
        }

    elapsed = time.time() - start_time

    # Detect base_path from tile samples
    detected_base_path = None
    if tile_samples:
        first_tile = tile_samples[0]
        match = tile_pattern.match(first_tile)
        if match and match.group(1):
            # Remove trailing slash
            detected_base_path = match.group(1).rstrip("/")

    return {
        "success": True,
        "members_scanned": total_members,
        "total_size": total_size,
        "top_level_dirs": sorted(top_level_dirs),
        "tile_samples": tile_samples,
        "zoom_levels": sorted(zoom_levels),
        "extensions": dict(extensions),
        "detected_base_path": detected_base_path,
        "elapsed_time": elapsed,
    }


def print_results(tar_path: Path, compression: str, results: Dict):
    """Print inspection results in a user-friendly format."""
    print("\n" + "=" * 70)
    print("TAR ARCHIVE INSPECTION RESULTS")
    print("=" * 70)

    print(f"\nFile: {tar_path}")
    print(f"Compression: {compression}")

    if "error" in results:
        print(f"\n❌ Error: {results['error']}")
        print(f"Members scanned before error: {results['members_scanned']}")
        return

    print(f"Size: {format_size(results['total_size'])}")
    print(f"Members scanned: {results['members_scanned']}")
    print(f"Scan time: {results['elapsed_time']:.2f}s")

    # Top-level structure
    print("\n" + "-" * 70)
    print("TOP-LEVEL STRUCTURE")
    print("-" * 70)

    if results["top_level_dirs"]:
        for dir_name in results["top_level_dirs"][:20]:  # Limit to 20
            print(f"  {dir_name}/")
        if len(results["top_level_dirs"]) > 20:
            print(f"  ... and {len(results['top_level_dirs']) - 20} more")
    else:
        print("  (No top-level directories found)")

    # Tile detection
    print("\n" + "-" * 70)
    print("TILE DETECTION")
    print("-" * 70)

    if results["tile_samples"]:
        print("\n✓ Found tiles matching {z}/{x}/{y.ext} pattern!")
        print(f"  Zoom levels detected: {results['zoom_levels']}")
        print(f"  File extensions: {list(results['extensions'].keys())}")

        print("\n  Sample tile paths:")
        for sample in results["tile_samples"][:5]:
            print(f"    {sample}")

        if len(results["tile_samples"]) > 5:
            print(f"    ... and {len(results['tile_samples']) - 5} more")
    else:
        print("\n✗ No tile pattern detected")
        print("  Could not find files matching {z}/{x}/{y.ext} structure")

    # Configuration recommendation
    print("\n" + "-" * 70)
    print("RECOMMENDED CONFIGURATION")
    print("-" * 70)

    if results["tile_samples"]:
        base_path = results["detected_base_path"]

        if base_path:
            print(f"\n✓ Detected base path: '{base_path}'")
            print("\nAdd to your tilesets.json:")
            print("\n{")
            print('  "tilesets": {')
            print('    "your_tileset_name": {{')
            print(f'      "source": "{tar_path}",')
            print(f'      "base_path": "{base_path}"')
            print("    }")
            print("  }")
            print("}\n")
        else:
            print("\n✓ Tiles are at root level of archive")
            print("\nAdd to your tilesets.json:")
            print("\n{")
            print('  "tilesets": {')
            print(f'    "your_tileset_name": "{tar_path}"')
            print("  }")
            print("}\n")

        # Performance warning for compressed archives
        if compression in ["gzip", "bzip2", "xz"]:
            print("⚠  PERFORMANCE WARNING:")
            print(f"   This is a {compression}-compressed archive.")
            print("   Compressed archives are 5-10x slower than uncompressed.")
            print("   Consider using uncompressed .tar files for production.")
    else:
        print("\n✗ Cannot generate configuration - no tiles found")
        print("\nPossible issues:")
        print("  - Archive may not contain map tiles")
        print("  - Tiles may use non-standard structure")
        print(
            f"  - Only scanned {results['members_scanned']} members (may need to increase limit)"
        )

    print("\n" + "=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Inspect tar archives for tile server configuration"
    )
    parser.add_argument(
        "tar_file",
        help="Path to tar archive to inspect",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Maximum seconds to spend inspecting (default: 60)",
    )
    parser.add_argument(
        "--max-members",
        type=int,
        default=1000,
        help="Maximum number of members to scan (default: 1000)",
    )

    args = parser.parse_args()

    tar_path = Path(args.tar_file)

    # Validate file exists
    if not tar_path.exists():
        print(f"❌ Error: File not found: {tar_path}", file=sys.stderr)
        sys.exit(1)

    if not tar_path.is_file():
        print(f"❌ Error: Not a file: {tar_path}", file=sys.stderr)
        sys.exit(1)

    # Detect compression
    print("Detecting compression type...")
    compression = detect_compression(tar_path)

    # Inspect archive
    print(
        f"Inspecting archive (timeout: {args.timeout}s, max members: {args.max_members})..."
    )
    print("This may take a moment for large archives...\n")

    try:
        results = inspect_tar_structure(
            tar_path,
            max_members=args.max_members,
            timeout_seconds=args.timeout,
        )
        print_results(tar_path, compression, results)

        if results.get("success") and results["tile_samples"]:
            sys.exit(0)
        else:
            sys.exit(1)

    except TarInspectionTimeout as e:
        print(f"\n❌ {e}", file=sys.stderr)
        print("\nTry:", file=sys.stderr)
        print("  - Increasing timeout: --timeout 120", file=sys.stderr)
        print("  - Increasing member limit: --max-members 5000", file=sys.stderr)
        sys.exit(2)

    except Exception as e:
        print(f"\n❌ Unexpected error: {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
