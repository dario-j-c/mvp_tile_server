import json
import logging
import shutil
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path

import pytest

from app.config import (
    auto_detect_base_path,
    load_tileset_config,
    scan_tiles,
)
from app.exceptions import (
    InvalidCoordinateError,
    InvalidZoomLevelError,
    TarIndexUnavailableError,
    TileCorruptedError,
    TileNotFoundError,
    TileServerError,
    TilesetNotFoundError,
)
from app.tar_manager import TarManager, build_tar_index
from app.utils import (
    detect_tar_compression,
    find_tile_in_tar_index,
    find_tile_path,
    is_tar_file,
    media_type_for_suffix,
)

# Note: _find_tile_in_tar_index is tested via TarManager


@pytest.fixture(scope="function")
def temp_dir():
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_is_tar_file_with_tar(temp_dir):
    """Test detection of .tar file"""
    tar_path = temp_dir / "test.tar"
    tar_path.touch()
    assert is_tar_file(tar_path)


def test_is_tar_file_with_tar_gz(temp_dir):
    """Test detection of .tar.gz file"""
    tar_path = temp_dir / "test.tar.gz"
    tar_path.touch()
    assert is_tar_file(tar_path)


def test_is_tar_file_with_directory(temp_dir):
    """Test that directories are not detected as tar files"""
    dir_path = temp_dir / "not_a_tar"
    dir_path.mkdir()
    assert not is_tar_file(dir_path)


def test_is_tar_file_with_other_file(temp_dir):
    """Test that non-tar files are not detected"""
    file_path = temp_dir / "test.json"
    file_path.touch()
    assert not is_tar_file(file_path)


def test_detect_compression_uncompressed():
    """Test detection of uncompressed tar"""
    tar_path = Path("test.tar")
    assert detect_tar_compression(tar_path) == "uncompressed"


def test_detect_compression_gzip():
    """Test detection of gzip compression"""
    tar_path = Path("test.tar.gz")
    assert detect_tar_compression(tar_path) == "gzip"


def test_detect_compression_bzip2():
    """Test detection of bzip2 compression"""
    tar_path = Path("test.tar.bz2")
    assert detect_tar_compression(tar_path) == "bzip2"


def test_detect_compression_xz():
    """Test detection of xz compression"""
    tar_path = Path("test.tar.xz")
    assert detect_tar_compression(tar_path) == "xz"


def test_png_media_type():
    assert media_type_for_suffix(".png") == "image/png"


def test_jpg_media_type():
    assert media_type_for_suffix(".jpg") == "image/jpeg"
    assert media_type_for_suffix(".jpeg") == "image/jpeg"


def test_webp_media_type():
    assert media_type_for_suffix(".webp") == "image/webp"


def test_unknown_media_type():
    assert media_type_for_suffix(".xyz") is None


def test_case_insensitive_media_type():
    assert media_type_for_suffix(".PNG") == "image/png"
    assert media_type_for_suffix(".JPG") == "image/jpeg"


@pytest.fixture(scope="function")
def tile_dir(temp_dir):
    tile_dir_path = temp_dir / "10" / "5"
    tile_dir_path.mkdir(parents=True)
    (tile_dir_path / "3.png").touch()
    (tile_dir_path / "4.jpg").touch()
    return temp_dir


def test_find_existing_tile(tile_dir):
    """Test finding a tile that exists"""
    tile_path, _ = find_tile_path(tile_dir, 10, 5, "3.png")
    assert tile_path is not None
    assert tile_path.exists()


def test_find_tile_with_extension_probing(tile_dir):
    """Test finding a tile by probing different extensions"""
    # Request .webp but .png exists
    tile_path, _ = find_tile_path(tile_dir, 10, 5, "3.webp")
    assert tile_path is not None
    assert tile_path.suffix == ".png"


def test_find_nonexistent_tile(tile_dir):
    """Test that nonexistent tiles return None"""
    tile_path, _ = find_tile_path(tile_dir, 10, 5, "999.png")
    assert tile_path is None


def test_load_directory_config(temp_dir):
    """Test loading configuration with directory paths"""
    # Create test directory
    tiles_dir = temp_dir / "tiles"
    tiles_dir.mkdir()

    # Create config
    config = {"tilesets": {"test": str(tiles_dir)}}
    config_path = temp_dir / "config.json"
    config_path.write_text(json.dumps(config))

    # Load config
    result = load_tileset_config(str(config_path))

    assert "test" in result
    assert result["test"]["source_type"] == "directory"
    assert result["test"]["source_path"] == tiles_dir.resolve()


def test_load_invalid_tileset_name(temp_dir):
    """Test that invalid tileset names are rejected"""
    config = {"tilesets": {"123invalid": "/fake/path"}}
    config_path = temp_dir / "config.json"
    config_path.write_text(json.dumps(config))

    with pytest.raises(ValueError, match="Invalid tileset name"):
        load_tileset_config(str(config_path))


def test_load_nonexistent_path(temp_dir):
    """Test that nonexistent paths are rejected"""
    config = {"tilesets": {"test": "/this/path/does/not/exist"}}
    config_path = temp_dir / "config.json"
    config_path.write_text(json.dumps(config))

    with pytest.raises(ValueError, match="does not exist"):
        load_tileset_config(str(config_path))


def test_load_missing_config_file():
    """Test that missing config file raises error"""
    with pytest.raises(ValueError, match="not found"):
        load_tileset_config("/nonexistent/config.json")


@pytest.fixture(scope="function")
def tar_file(temp_dir):
    # Create a test tar file with tile structure
    tar_path = temp_dir / "test.tar"
    with tarfile.open(tar_path, "w") as tar:
        # Add some fake tiles
        for z in [10, 11]:
            for x in [0, 1]:
                for y in [0, 1]:
                    from io import BytesIO

                    tile_data = b"fake tile data"
                    tile_info = tarfile.TarInfo(name=f"{z}/{x}/{y}.png")
                    tile_info.size = len(tile_data)
                    tar.addfile(tile_info, BytesIO(tile_data))
    return tar_path


def test_build_tar_index(tar_file):
    """Test building index from tar archive"""
    member_index, zoom_levels = build_tar_index(tar_file)

    # Should have 8 tiles (2 zooms * 2 x * 2 y)
    assert len(member_index) == 8

    # Should detect zoom levels 10 and 11
    assert zoom_levels == {10, 11}

    # Check specific tile exists
    assert "10/0/0.png" in member_index
    assert "11/1/1.png" in member_index


def test_build_tar_index_with_base_path(temp_dir):
    """Test building index with base_path filtering"""
    # Create tar with nested structure
    tar_nested = temp_dir / "nested.tar"
    with tarfile.open(tar_nested, "w") as tar:
        from io import BytesIO

        tile_data = b"fake tile"

        # Add tile under tiles/10/0/0.png
        tile_info = tarfile.TarInfo(name="tiles/10/0/0.png")
        tile_info.size = len(tile_data)
        tar.addfile(tile_info, BytesIO(tile_data))

    member_index, zoom_levels = build_tar_index(tar_nested, base_path="tiles")

    # Should find the tile with normalized path
    assert "10/0/0.png" in member_index
    assert zoom_levels == {10}


@pytest.mark.asyncio
async def test_find_tile_in_tar_index(tar_file):
    """Test finding tiles in pre-built index"""
    tar_manager = TarManager()
    await tar_manager.initialize_tileset("test", tar_file)

    # Find existing tile
    tile_data, media_type, headers = await tar_manager.get_tile_from_tar(
        "test", 10, 0, "0.png"
    )
    assert tile_data is not None
    assert media_type == "image/png"

    # Find nonexistent tile
    with pytest.raises(TileNotFoundError):
        await tar_manager.get_tile_from_tar("test", 99, 0, "0.png")


def test_detect_nested_base_path(temp_dir):
    """Test detection of tiles in subdirectory"""
    tar_path = temp_dir / "nested.tar"
    with tarfile.open(tar_path, "w") as tar:
        from io import BytesIO

        tile_data = b"fake"

        # Add tiles under data/tiles/
        for i in range(5):
            tile_info = tarfile.TarInfo(name=f"data/tiles/10/{i}/0.png")
            tile_info.size = len(tile_data)
            tar.addfile(tile_info, BytesIO(tile_data))

    detected = auto_detect_base_path(tar_path, max_members=10)
    assert detected == "data/tiles"


def test_detect_root_level_tiles(temp_dir):
    """Test detection of tiles at root level"""
    tar_path = temp_dir / "root.tar"
    with tarfile.open(tar_path, "w") as tar:
        tile_data = b"fake"

        # Add tiles at root
        tile_info = tarfile.TarInfo(name="10/0/0.png")
        tile_info.size = len(tile_data)
        tar.addfile(tile_info, BytesIO(tile_data))

    detected = auto_detect_base_path(tar_path, max_members=10)
    assert detected is None


# ============================================================================
# Additional Tar Extension Tests
# ============================================================================


def test_is_tar_file_with_tgz(temp_dir):
    """Test detection of .tgz file"""
    tar_path = temp_dir / "test.tgz"
    tar_path.touch()
    assert is_tar_file(tar_path)


def test_is_tar_file_with_tbz2(temp_dir):
    """Test detection of .tbz2 file"""
    tar_path = temp_dir / "test.tbz2"
    tar_path.touch()
    assert is_tar_file(tar_path)


def test_is_tar_file_with_txz(temp_dir):
    """Test detection of .txz file"""
    tar_path = temp_dir / "test.txz"
    tar_path.touch()
    assert is_tar_file(tar_path)


def test_detect_compression_tgz():
    """Test detection of .tgz as gzip"""
    tar_path = Path("test.tgz")
    assert detect_tar_compression(tar_path) == "gzip"


def test_detect_compression_tbz2():
    """Test detection of .tbz2 as bzip2"""
    tar_path = Path("test.tbz2")
    assert detect_tar_compression(tar_path) == "bzip2"


def test_detect_compression_txz():
    """Test detection of .txz as xz"""
    tar_path = Path("test.txz")
    assert detect_tar_compression(tar_path) == "xz"


def test_detect_compression_unknown():
    """Test detection of unknown compression"""
    tar_path = Path("test.unknown")
    assert detect_tar_compression(tar_path) == "unknown"


# ============================================================================
# Exception Tests
# ============================================================================


def test_tile_server_error_base():
    """Test base TileServerError attributes."""
    error = TileServerError("Test message", status_code=500, error_code="TEST_ERROR")
    assert error.message == "Test message"
    assert error.status_code == 500
    assert error.error_code == "TEST_ERROR"
    assert str(error) == "Test message"


def test_tile_server_error_defaults():
    """Test TileServerError default values."""
    error = TileServerError("Test message")
    assert error.status_code == 500
    assert error.error_code is None


def test_tileset_not_found_error():
    """Test TilesetNotFoundError attributes and message."""
    error = TilesetNotFoundError("my_tileset", ["osm", "satellite"])
    assert error.tileset_name == "my_tileset"
    assert error.available_tilesets == ["osm", "satellite"]
    assert error.status_code == 404
    assert error.error_code == "TILESET_NOT_FOUND"
    assert "my_tileset" in error.message
    assert "osm" in error.message
    assert "satellite" in error.message


def test_invalid_zoom_level_error():
    """Test InvalidZoomLevelError attributes and message."""
    error = InvalidZoomLevelError(z=99, min_z=1, max_z=18, tileset_name="osm")
    assert error.z == 99
    assert error.min_z == 1
    assert error.max_z == 18
    assert error.status_code == 404
    assert error.error_code == "INVALID_ZOOM_LEVEL"
    assert "99" in error.message
    assert "1-18" in error.message


def test_invalid_coordinate_error():
    """Test InvalidCoordinateError attributes and message."""
    error = InvalidCoordinateError(coord_name="X", coord_value=9999, z=10)
    assert error.status_code == 404
    assert error.error_code == "INVALID_COORDINATE"
    assert "X" in error.message
    assert "9999" in error.message
    # At zoom 10, max is 1023
    assert "1023" in error.message


def test_tile_not_found_error():
    """Test TileNotFoundError attributes and message."""
    error = TileNotFoundError(
        "osm", 10, 5, "123.png", tried_extensions=[".png", ".jpg"]
    )
    assert error.status_code == 404
    assert error.error_code == "TILE_NOT_FOUND"
    assert "/osm/10/5/123.png" in error.message
    assert ".png" in error.message
    assert ".jpg" in error.message


def test_tile_not_found_error_without_extensions():
    """Test TileNotFoundError without tried_extensions."""
    error = TileNotFoundError("osm", 10, 5, "123.png")
    assert "/osm/10/5/123.png" in error.message
    assert "tried extensions" not in error.message


def test_tile_corrupted_error():
    """Test TileCorruptedError attributes and message."""
    error = TileCorruptedError("osm", 10, 5, "123.png", reason="Permission denied")
    assert error.status_code == 500
    assert error.error_code == "TILE_CORRUPTED"
    assert "/osm/10/5/123.png" in error.message
    assert "Permission denied" in error.message


def test_tar_index_unavailable_error():
    """Test TarIndexUnavailableError attributes and message."""
    error = TarIndexUnavailableError("my_tileset")
    assert error.status_code == 503
    assert error.error_code == "TAR_INDEX_UNAVAILABLE"
    assert "my_tileset" in error.message


# ============================================================================
# Config Loading Tests - Tar Format
# ============================================================================


def test_load_tar_config_simple(temp_dir):
    """Test loading configuration with tar file path (simple string format)."""
    # Create a minimal valid tar file
    tar_path = temp_dir / "tiles.tar"
    with tarfile.open(tar_path, "w") as tar:
        tile_data = b"fake tile"
        tile_info = tarfile.TarInfo(name="10/0/0.png")
        tile_info.size = len(tile_data)
        tar.addfile(tile_info, BytesIO(tile_data))

    # Create config
    config = {"tilesets": {"test_tar": str(tar_path)}}
    config_path = temp_dir / "config.json"
    config_path.write_text(json.dumps(config))

    # Load config
    result = load_tileset_config(str(config_path))

    assert "test_tar" in result
    assert result["test_tar"]["source_type"] == "tar"
    assert result["test_tar"]["source_path"] == tar_path.resolve()


def test_load_tar_config_with_base_path(temp_dir):
    """Test loading configuration with tar file using dict format with base_path."""
    # Create a tar with nested structure
    tar_path = temp_dir / "tiles.tar"
    with tarfile.open(tar_path, "w") as tar:
        tile_data = b"fake tile"
        tile_info = tarfile.TarInfo(name="map_data/tiles/10/0/0.png")
        tile_info.size = len(tile_data)
        tar.addfile(tile_info, BytesIO(tile_data))

    # Create config with explicit base_path
    config = {
        "tilesets": {
            "test_tar": {
                "source": str(tar_path),
                "base_path": "map_data/tiles",
            }
        }
    }
    config_path = temp_dir / "config.json"
    config_path.write_text(json.dumps(config))

    # Load config
    result = load_tileset_config(str(config_path))

    assert "test_tar" in result
    assert result["test_tar"]["source_type"] == "tar"
    assert result["test_tar"].get("base_path") == "map_data/tiles"


def test_load_config_invalid_json(temp_dir):
    """Test that invalid JSON raises ValueError."""
    config_path = temp_dir / "config.json"
    config_path.write_text("{ invalid json }")

    with pytest.raises(ValueError, match="Invalid JSON"):
        load_tileset_config(str(config_path))


def test_load_config_missing_tilesets_key(temp_dir):
    """Test that missing 'tilesets' key raises ValueError."""
    config_path = temp_dir / "config.json"
    config_path.write_text(json.dumps({"other_key": {}}))

    with pytest.raises(ValueError, match="must contain 'tilesets'"):
        load_tileset_config(str(config_path))


def test_load_config_empty_tilesets(temp_dir):
    """Test that empty tilesets dict raises ValueError."""
    config_path = temp_dir / "config.json"
    config_path.write_text(json.dumps({"tilesets": {}}))

    with pytest.raises(ValueError, match="At least one tileset"):
        load_tileset_config(str(config_path))


def test_load_config_multiple_errors(temp_dir):
    """Test that multiple config errors are collected and reported together."""
    config = {
        "tilesets": {
            "123invalid": "/fake/path1",  # Invalid name
            "also_invalid!": "/fake/path2",  # Invalid name (special char)
            "valid_name": "/nonexistent/path",  # Valid name, invalid path
        }
    }
    config_path = temp_dir / "config.json"
    config_path.write_text(json.dumps(config))

    with pytest.raises(ValueError) as exc_info:
        load_tileset_config(str(config_path))

    error_message = str(exc_info.value)
    # Should mention multiple errors
    assert "123invalid" in error_message
    assert "also_invalid" in error_message
    assert "does not exist" in error_message


def test_load_config_dict_missing_source(temp_dir):
    """Test that dict config without 'source' key raises error."""
    tiles_dir = temp_dir / "tiles"
    tiles_dir.mkdir()

    config = {
        "tilesets": {
            "test": {
                "base_path": "tiles",  # Missing 'source' key
            }
        }
    }
    config_path = temp_dir / "config.json"
    config_path.write_text(json.dumps(config))

    with pytest.raises(ValueError, match="must have 'source' key"):
        load_tileset_config(str(config_path))


def test_load_compressed_tar_logs_warning(temp_dir, caplog):
    """Test that loading compressed tar logs a performance warning."""
    # Create a gzip-compressed tar file
    tar_path = temp_dir / "tiles.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tile_data = b"fake tile"
        tile_info = tarfile.TarInfo(name="10/0/0.png")
        tile_info.size = len(tile_data)
        tar.addfile(tile_info, BytesIO(tile_data))

    config = {"tilesets": {"test_tar": str(tar_path)}}
    config_path = temp_dir / "config.json"
    config_path.write_text(json.dumps(config))

    with caplog.at_level(logging.WARNING):
        result = load_tileset_config(str(config_path))

    assert "test_tar" in result
    # Check warning was logged about compression
    assert any("gzip" in record.message.lower() for record in caplog.records) or any(
        "slower" in record.message.lower() for record in caplog.records
    )


# ============================================================================
# TarManager Tests
# ============================================================================


@pytest.mark.asyncio
async def test_tar_manager_initialize_and_close(temp_dir):
    """Test TarManager initialization and cleanup."""
    # Create tar file
    tar_path = temp_dir / "test.tar"
    with tarfile.open(tar_path, "w") as tar:
        tile_data = b"fake tile"
        tile_info = tarfile.TarInfo(name="10/0/0.png")
        tile_info.size = len(tile_data)
        tar.addfile(tile_info, BytesIO(tile_data))

    tar_manager = TarManager()
    await tar_manager.initialize_tileset("test", tar_path)

    # Check status
    assert "test" in tar_manager.index_status
    assert tar_manager.index_status["test"]["status"] == "ready"
    assert tar_manager.index_status["test"]["tile_count"] == 1

    # Clean up
    await tar_manager.close_all()

    # Handles should be closed (we can't easily verify this, but no error is good)


@pytest.mark.asyncio
async def test_tar_manager_rebuild_index(temp_dir):
    """Test TarManager.rebuild_index() hot reload functionality."""
    # Create initial tar file with 1 tile
    tar_path = temp_dir / "test.tar"
    with tarfile.open(tar_path, "w") as tar:
        tile_data = b"fake tile"
        tile_info = tarfile.TarInfo(name="10/0/0.png")
        tile_info.size = len(tile_data)
        tar.addfile(tile_info, BytesIO(tile_data))

    tar_manager = TarManager()
    await tar_manager.initialize_tileset("test", tar_path)

    assert tar_manager.index_status["test"]["tile_count"] == 1

    # Update tar file with more tiles
    with tarfile.open(tar_path, "w") as tar:
        tile_data = b"fake tile"
        for i in range(5):
            tile_info = tarfile.TarInfo(name=f"10/{i}/0.png")
            tile_info.size = len(tile_data)
            tar.addfile(tile_info, BytesIO(tile_data))

    # Rebuild index
    await tar_manager.rebuild_index("test", tar_path)

    # Check updated count
    assert tar_manager.index_status["test"]["status"] == "ready"
    assert tar_manager.index_status["test"]["tile_count"] == 5
    assert tar_manager.index_status["test"]["last_rebuilt"] is not None

    await tar_manager.close_all()


@pytest.mark.asyncio
async def test_tar_manager_get_tile_index_unavailable():
    """Test that getting tile from uninitialized tileset raises TarIndexUnavailableError."""
    tar_manager = TarManager()

    with pytest.raises(TarIndexUnavailableError):
        await tar_manager.get_tile_from_tar("nonexistent", 10, 0, "0.png")


@pytest.mark.asyncio
async def test_tar_manager_extension_probing(temp_dir):
    """Test that TarManager probes different extensions when finding tiles."""
    # Create tar with .jpg tile
    tar_path = temp_dir / "test.tar"
    with tarfile.open(tar_path, "w") as tar:
        tile_data = b"fake jpeg data"
        tile_info = tarfile.TarInfo(name="10/0/0.jpg")
        tile_info.size = len(tile_data)
        tar.addfile(tile_info, BytesIO(tile_data))

    tar_manager = TarManager()
    await tar_manager.initialize_tileset("test", tar_path)

    # Request .png but .jpg exists - should find via probing
    tile_data, media_type, headers = await tar_manager.get_tile_from_tar(
        "test", 10, 0, "0.png"
    )
    assert tile_data == b"fake jpeg data"
    assert media_type == "image/jpeg"

    await tar_manager.close_all()


# ============================================================================
# scan_tiles Tests
# ============================================================================


def test_scan_tiles_directory(temp_dir):
    """Test scanning tiles from directory."""
    # Create tile structure
    for z in [10, 11]:
        for x in [0, 1]:
            tile_dir = temp_dir / str(z) / str(x)
            tile_dir.mkdir(parents=True)
            (tile_dir / "0.png").touch()
            (tile_dir / "1.png").touch()

    tile_count, samples, zoom_levels, min_z, max_z = scan_tiles(
        temp_dir, "directory", max_samples=5
    )

    assert tile_count == 8  # 2 zooms * 2 x * 2 y
    assert len(samples) <= 5
    assert zoom_levels == [10, 11]
    assert min_z == 10
    assert max_z == 11


def test_scan_tiles_tar(temp_dir):
    """Test scanning tiles from tar archive."""
    tar_path = temp_dir / "tiles.tar"
    with tarfile.open(tar_path, "w") as tar:
        tile_data = b"fake"
        for z in [12, 13, 14]:
            for x in [0, 1]:
                tile_info = tarfile.TarInfo(name=f"{z}/{x}/0.png")
                tile_info.size = len(tile_data)
                tar.addfile(tile_info, BytesIO(tile_data))

    tile_count, samples, zoom_levels, min_z, max_z = scan_tiles(
        tar_path, "tar", max_samples=3
    )

    assert tile_count == 6  # 3 zooms * 2 x
    assert len(samples) <= 3
    assert zoom_levels == [12, 13, 14]
    assert min_z == 12
    assert max_z == 14


def test_scan_tiles_tar_with_base_path(temp_dir):
    """Test scanning tiles from tar archive with base_path."""
    tar_path = temp_dir / "tiles.tar"
    with tarfile.open(tar_path, "w") as tar:
        tile_data = b"fake"
        # Add tiles under nested/path/
        tile_info = tarfile.TarInfo(name="nested/path/10/0/0.png")
        tile_info.size = len(tile_data)
        tar.addfile(tile_info, BytesIO(tile_data))

    tile_count, samples, zoom_levels, min_z, max_z = scan_tiles(
        tar_path, "tar", base_path="nested/path"
    )

    assert tile_count == 1
    assert zoom_levels == [10]


# ============================================================================
# find_tile_in_tar_index Tests
# ============================================================================


def test_find_tile_in_tar_index_exact_match():
    """Test finding tile with exact extension match."""
    # Create mock index
    mock_member = tarfile.TarInfo(name="10/5/3.png")
    tar_index = {"10/5/3.png": mock_member}

    result, tried = find_tile_in_tar_index(tar_index, 10, 5, "3.png")
    assert result == mock_member
    assert ".png" in tried


def test_find_tile_in_tar_index_extension_fallback():
    """Test finding tile when requested extension doesn't match but another does."""
    # Create mock index with .jpg
    mock_member = tarfile.TarInfo(name="10/5/3.jpg")
    tar_index = {"10/5/3.jpg": mock_member}

    # Request .png, should find .jpg
    result, tried = find_tile_in_tar_index(tar_index, 10, 5, "3.png")
    assert result == mock_member
    assert ".png" in tried
    assert ".jpg" in tried


def test_find_tile_in_tar_index_not_found():
    """Test that missing tile returns None."""
    tar_index = {}

    result, tried = find_tile_in_tar_index(tar_index, 10, 5, "999.png")
    assert result is None
    assert len(tried) > 0  # Should have tried multiple extensions
