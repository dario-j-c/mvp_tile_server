import asyncio
import os
import tarfile
from pathlib import Path
from io import BytesIO
import pytest
import tempfile
import shutil

from app.tar_manager import (
    TarManager,
    _touch_sentinel,
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
    monkeypatch.setattr(os, "access", lambda path, _mode: False)
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


# ============================================================================
# Path B: Sentinel-based cross-worker reload
# ============================================================================


def test_touch_sentinel_creates_file(temp_dir):
    sentinel = temp_dir / ".tar_reload"
    assert not sentinel.exists()
    _touch_sentinel(sentinel)
    assert sentinel.exists()


def test_touch_sentinel_updates_mtime(temp_dir):
    sentinel = temp_dir / ".tar_reload"
    sentinel.touch()
    old_mtime = sentinel.stat().st_mtime - 10
    os.utime(sentinel, (old_mtime, old_mtime))
    _touch_sentinel(sentinel)
    assert sentinel.stat().st_mtime > old_mtime


def test_touch_sentinel_non_writable_dir(temp_dir, monkeypatch):
    """_touch_sentinel logs a warning and does not raise when the path is unwritable."""
    sentinel = temp_dir / "no_write" / ".tar_reload"

    def fail_touch(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "touch", fail_touch)
    # Should not raise
    _touch_sentinel(sentinel)


def test_set_sentinel_path_existing_file(temp_dir):
    sentinel = temp_dir / ".tar_reload"
    sentinel.touch()
    mtime = sentinel.stat().st_mtime

    manager = TarManager()
    manager.set_sentinel_path(sentinel)

    assert manager._sentinel_path == sentinel
    assert manager._sentinel_mtime == mtime


def test_set_sentinel_path_missing_file(temp_dir):
    sentinel = temp_dir / ".tar_reload_missing"
    manager = TarManager()
    manager.set_sentinel_path(sentinel)

    assert manager._sentinel_path == sentinel
    assert manager._sentinel_mtime == 0.0


def test_rebuild_index_touches_sentinel(tar_file, temp_dir):
    """POST /admin/rebuild path: rebuild_index writes .idx and touches sentinel."""
    sentinel = temp_dir / ".tar_reload"

    manager = TarManager()
    asyncio.run(manager.initialize_tileset("test", tar_file, ""))
    manager.set_sentinel_path(sentinel)

    asyncio.run(manager.rebuild_index("test", tar_file, ""))

    assert sentinel.exists(), "sentinel should be created by rebuild_index"


def test_rebuild_index_updates_own_cache_mtime(tar_file, temp_dir):
    """After rebuild, the rebuilding worker's _last_loaded_cache_mtime is current
    so its own sentinel watcher does not trigger a self-reload."""
    sentinel = temp_dir / ".tar_reload"

    async def run():
        manager = TarManager()
        await manager.initialize_tileset("test", tar_file, "")
        manager.set_sentinel_path(sentinel)
        await manager.rebuild_index("test", tar_file, "")
        cache_path = get_tar_cache_path(tar_file)
        assert cache_path.exists()
        assert manager._last_loaded_cache_mtime["test"] == cache_path.stat().st_mtime
        await manager.close_all()

    asyncio.run(run())


def test_reload_stale_indexes_reloads_when_cache_is_newer(tar_file):
    """_reload_stale_indexes replaces the index when the .idx is newer than last load."""

    async def run():
        manager = TarManager()
        await manager.initialize_tileset("test", tar_file, "")

        # Backdate last-loaded mtime to force a reload
        manager._last_loaded_cache_mtime["test"] = 0.0
        original_index = manager.tar_indexes["test"]

        await manager._reload_stale_indexes()

        reloaded_index = manager.tar_indexes["test"]
        # Content must be identical even though it's a fresh load
        assert reloaded_index == original_index
        # The cache mtime record must be updated
        cache_path = get_tar_cache_path(tar_file)
        assert manager._last_loaded_cache_mtime["test"] == cache_path.stat().st_mtime

        await manager.close_all()

    asyncio.run(run())


def test_reload_stale_indexes_skips_when_cache_unchanged(tar_file):
    """_reload_stale_indexes leaves the index dict unchanged when cache is not newer."""

    async def run():
        manager = TarManager()
        await manager.initialize_tileset("test", tar_file, "")

        # Set last-loaded mtime to far future so the cache always appears older
        manager._last_loaded_cache_mtime["test"] = float("inf")
        index_id_before = id(manager.tar_indexes["test"])

        await manager._reload_stale_indexes()

        # The dict object must be the same — no reload happened
        assert id(manager.tar_indexes["test"]) == index_id_before

        await manager.close_all()

    asyncio.run(run())


def test_sentinel_watcher_task_lifecycle(tar_file, temp_dir):
    """Watcher task is created by start_sentinel_watcher and cancelled by close_all."""

    async def run():
        manager = TarManager()
        await manager.initialize_tileset("test", tar_file, "")
        manager.set_sentinel_path(temp_dir / ".tar_reload")
        manager.start_sentinel_watcher()

        assert manager._sentinel_task is not None
        assert not manager._sentinel_task.done()

        await manager.close_all()

        assert manager._sentinel_task.done()

    asyncio.run(run())


def test_sentinel_watcher_detects_change_and_reloads(tar_file, temp_dir):
    """When the sentinel mtime advances and the .idx is newer, watcher reloads index."""

    async def run():
        manager = TarManager()
        manager._sentinel_poll_interval = 0.05  # fast poll for test
        await manager.initialize_tileset("test", tar_file, "")

        sentinel = temp_dir / ".tar_reload"
        manager.set_sentinel_path(sentinel)

        # Backdate last-loaded mtime so the existing .idx appears newer
        manager._last_loaded_cache_mtime["test"] = 0.0

        manager.start_sentinel_watcher()

        # Touch sentinel to signal other workers that a reload is available
        sentinel.touch()

        # Give the watcher two poll cycles to detect and act
        await asyncio.sleep(0.15)

        # Index should have been reloaded (cache mtime record updated from 0.0)
        cache_path = get_tar_cache_path(tar_file)
        assert manager._last_loaded_cache_mtime["test"] == cache_path.stat().st_mtime

        await manager.close_all()

    asyncio.run(run())
