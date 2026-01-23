"""Custom exceptions for the tile server with HTTP status codes."""

from typing import List, Optional


class TileServerError(Exception):
    """
    Base exception for tile server errors.

    Attributes:
        message: Human-readable error message.
        status_code: HTTP status code to return.
        error_code: Machine-readable error code for API responses.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        error_code: Optional[str] = None,
    ) -> None:
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(self.message)


class TilesetNotFoundError(TileServerError):
    """Raised when a requested tileset does not exist in configuration."""

    def __init__(self, tileset_name: str, available_tilesets: List[str]) -> None:
        message = (
            f"Tileset '{tileset_name}' not found. "
            f"Available tilesets: {', '.join(available_tilesets)}"
        )
        super().__init__(message, status_code=404, error_code="TILESET_NOT_FOUND")
        self.tileset_name = tileset_name
        self.available_tilesets = available_tilesets


class InvalidZoomLevelError(TileServerError):
    """Raised when zoom level is outside the valid range for a tileset."""

    def __init__(self, z: int, min_z: int, max_z: int, tileset_name: str) -> None:
        message = (
            f"Invalid zoom level {z} for tileset '{tileset_name}'. "
            f"Valid range: {min_z}-{max_z}"
        )
        super().__init__(message, status_code=404, error_code="INVALID_ZOOM_LEVEL")
        self.z = z
        self.min_z = min_z
        self.max_z = max_z


class InvalidCoordinateError(TileServerError):
    """Raised when a tile coordinate is outside the valid range for its zoom level."""

    def __init__(self, coord_name: str, coord_value: int, z: int) -> None:
        max_coord = (1 << z) - 1
        message = (
            f"Invalid {coord_name} coordinate {coord_value} for zoom {z}. "
            f"Valid range: 0-{max_coord}"
        )
        super().__init__(message, status_code=404, error_code="INVALID_COORDINATE")


class TileNotFoundError(TileServerError):
    """Raised when a tile file does not exist at the specified coordinates."""

    def __init__(
        self,
        tileset_name: str,
        z: int,
        x: int,
        y: str,
        tried_extensions: Optional[List[str]] = None,
    ) -> None:
        message = f"Tile not found: /{tileset_name}/{z}/{x}/{y}"
        if tried_extensions:
            message += f" (tried extensions: {', '.join(tried_extensions)})"
        super().__init__(message, status_code=404, error_code="TILE_NOT_FOUND")


class TileCorruptedError(TileServerError):
    """Raised when a tile exists but cannot be read (corrupted or permission issue)."""

    def __init__(self, tileset_name: str, z: int, x: int, y: str, reason: str) -> None:
        message = f"Tile corrupted or unreadable: /{tileset_name}/{z}/{x}/{y}. Reason: {reason}"
        super().__init__(message, status_code=500, error_code="TILE_CORRUPTED")


class TarIndexUnavailableError(TileServerError):
    """Raised when a tar index is not built or temporarily unavailable."""

    def __init__(self, tileset_name: str) -> None:
        message = f"Tar index unavailable for tileset '{tileset_name}'. Server may be rebuilding index."
        super().__init__(message, status_code=503, error_code="TAR_INDEX_UNAVAILABLE")
