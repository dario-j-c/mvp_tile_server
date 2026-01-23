"""Utility functions for tile detection, path finding, and media type resolution."""

import tarfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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


def is_tar_file(path: Path) -> bool:
    """
    Check if a path points to a tar archive based on file extension.

    Args:
        path: Path to check.

    Returns:
        True if the path is a file with a tar extension, False otherwise.
    """
    if not path.is_file():
        return False
    return any(str(path).endswith(ext) for ext in TAR_EXTENSIONS)


def detect_tar_compression(tar_path: Path) -> str:
    """
    Detect compression type of a tar archive based on file extension.

    Args:
        tar_path: Path to the tar archive.

    Returns:
        Compression type: "gzip", "bzip2", "xz", "uncompressed", or "unknown".
    """
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


def find_tile_in_tar_index(
    tar_index: Dict[str, tarfile.TarInfo], z: int, x: int, y_name: str
) -> Tuple[Optional[tarfile.TarInfo], List[str]]:
    """
    Find tile member in pre-built tar index.

    Tries the exact filename first, then probes other supported extensions.

    Args:
        tar_index: Pre-built index mapping "z/x/y.ext" to TarInfo objects.
        z: Zoom level.
        x: X tile coordinate.
        y_name: Y coordinate with file extension (e.g., "123.png").

    Returns:
        Tuple of (TarInfo if found else None, list of tried extensions).
    """
    y_path = Path(y_name)
    stem = y_path.stem
    ext = y_path.suffix.lower()

    candidates: List[str] = []
    tried_extensions: List[str] = []

    # Try exact name if extension provided and supported
    if ext in SUPPORTED_EXTS:
        candidates.append(f"{z}/{x}/{y_name}")
        tried_extensions.append(ext)

    # Probe other extensions
    for e in SUPPORTED_EXTS:
        if e != ext:
            candidates.append(f"{z}/{x}/{stem}{e}")
            tried_extensions.append(e)

    for candidate_key in candidates:
        if candidate_key in tar_index:
            return tar_index[candidate_key], tried_extensions

    return None, tried_extensions


def find_tile_path(
    base_dir: Path, z: int, x: int, y_name: str
) -> Tuple[Optional[Path], List[str]]:
    """
    Find the tile file path in a directory-based tileset.

    Tries the exact filename first, then probes other supported extensions.

    Args:
        base_dir: Base directory of the tileset.
        z: Zoom level.
        x: X tile coordinate.
        y_name: Y coordinate with file extension (e.g., "123.png").

    Returns:
        Tuple of (Path if found else None, list of tried extensions).
    """
    y_path = Path(y_name)
    stem = y_path.stem
    ext = y_path.suffix.lower()

    candidates: List[Path] = []
    tried_extensions: List[str] = []

    # Try exact name if extension provided and supported
    if ext in SUPPORTED_EXTS:
        candidates.append(base_dir / str(z) / str(x) / y_name)
        tried_extensions.append(ext)

    # Probe other extensions
    for e in SUPPORTED_EXTS:
        if e != ext:
            candidates.append(base_dir / str(z) / str(x) / f"{stem}{e}")
            tried_extensions.append(e)

    for p in candidates:
        if p.is_file():
            return p, tried_extensions
    return None, tried_extensions


def media_type_for_suffix(suffix: str) -> Optional[str]:
    """
    Get the MIME type for a file extension.

    Args:
        suffix: File extension including the dot (e.g., ".png").

    Returns:
        MIME type string (e.g., "image/png") or None if unsupported.
    """
    suffix = suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return None
