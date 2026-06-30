import os
import tarfile
from pathlib import Path
from io import BytesIO
import pytest
import tempfile
import shutil

from app.tar_manager import (
    load_or_build_tar_index,
    get_tar_cache_path,
    build_unified_tar_index,
    filter_index_for_tileset,
)


@pytest.fixture(scope="function")
def temp_dir():
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture(scope="function")
def tar_file(temp_dir):
    tar_path = temp_dir / "test.tar"
    with tarfile.open(tar_path, "w") as tar:
        for z in [10, 11]:
            for x in [0, 1]:
                for y in [0, 1]:
                    tile_data = b"fake tile data"
                    tile_info = tarfile.TarInfo(name=f"{z}/{x}/{y}.png")
                    tile_info.size = len(tile_data)
                    tar.addfile(tile_info, BytesIO(tile_data))
    return tar_path


def test_get_tar_cache_path(temp_dir, monkeypatch):
    tar_path = temp_dir / "test.tar"
    # Default is alongside tar file
    cache_path = get_tar_cache_path(tar_path)
    assert cache_path == tar_path.with_suffix(".tar.idx")

    # Test env var TAR_CACHE_DIR
    cache_dir = temp_dir / "cache"
    cache_dir.mkdir()
    monkeypatch.setenv("TAR_CACHE_DIR", str(cache_dir))
    assert get_tar_cache_path(tar_path) == cache_dir / "test.tar.idx"
    monkeypatch.delenv("TAR_CACHE_DIR")

    # Test read-only fallback
    monkeypatch.setattr(os, "access", lambda path, mode: False)
    fallback = get_tar_cache_path(tar_path)
    assert fallback == Path(tempfile.gettempdir()) / "test.tar.idx"


def test_load_or_build_tar_index_cache_hit(tar_file):
    # First call builds the cache
    index1 = load_or_build_tar_index(tar_file)
    cache_path = get_tar_cache_path(tar_file)
    assert cache_path.exists()

    # Second call uses cache and returns identical content
    index2 = load_or_build_tar_index(tar_file)
    assert index1 == index2


def test_load_or_build_tar_index_cache_invalidation(tar_file):
    # First call builds cache
    index1 = load_or_build_tar_index(tar_file)
    cache_path = get_tar_cache_path(tar_file)

    old_time = cache_path.stat().st_mtime - 10
    os.utime(cache_path, (old_time, old_time))

    # Rebuild — cache mtime should advance and content should match original
    index2 = load_or_build_tar_index(tar_file)
    assert cache_path.stat().st_mtime > old_time
    assert index1 == index2


def test_filter_index_for_tileset_no_base_path(tar_file):
    unified_index = build_unified_tar_index(tar_file)
    member_index, zoom_levels, sample_tiles = filter_index_for_tileset(unified_index)

    assert len(member_index) == 8  # 2 zoom * 2 x * 2 y
    assert zoom_levels == {10, 11}
    assert len(sample_tiles) <= 5
    assert all(k in member_index for k in sample_tiles)
    # Keys must be stripped tile paths, not raw archive paths
    for key in member_index:
        parts = key.split("/")
        assert len(parts) == 3  # z/x/y.ext


def test_filter_index_for_tileset_with_base_path(temp_dir):
    # Build a tar where tiles live under a "tiles/" prefix
    tar_path = temp_dir / "prefixed.tar"
    with tarfile.open(tar_path, "w") as tar:
        for z, x, y in [(10, 0, 0), (10, 0, 1)]:
            data = b"tile"
            info = tarfile.TarInfo(name=f"tiles/{z}/{x}/{y}.png")
            info.size = len(data)
            tar.addfile(info, BytesIO(data))
        # Add a non-tile file that should be ignored
        noise = b"readme"
        noise_info = tarfile.TarInfo(name="tiles/README.txt")
        noise_info.size = len(noise)
        tar.addfile(noise_info, BytesIO(noise))

    unified_index = build_unified_tar_index(tar_path)

    member_index, zoom_levels, sample_tiles = filter_index_for_tileset(
        unified_index, base_path="tiles"
    )

    assert len(member_index) == 2
    assert zoom_levels == {10}
    # Keys must have the "tiles/" prefix stripped
    assert "10/0/0.png" in member_index
    assert "10/0/1.png" in member_index


def test_filter_index_for_tileset_excludes_non_matching_base(temp_dir):
    tar_path = temp_dir / "multi.tar"
    with tarfile.open(tar_path, "w") as tar:
        for prefix in ("a", "b"):
            data = b"tile"
            info = tarfile.TarInfo(name=f"{prefix}/10/0/0.png")
            info.size = len(data)
            tar.addfile(info, BytesIO(data))

    unified_index = build_unified_tar_index(tar_path)

    index_a, _, _ = filter_index_for_tileset(unified_index, base_path="a")
    index_b, _, _ = filter_index_for_tileset(unified_index, base_path="b")

    assert set(index_a.keys()) == {"10/0/0.png"}
    assert set(index_b.keys()) == {"10/0/0.png"}
    assert index_a != index_b  # Same key, different TileEntry offsets
