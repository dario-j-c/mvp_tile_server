"""Integration tests for the tile server API endpoints."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

# Note: The 'client' fixture is provided by conftest.py
# TEST_CONFIG_PATH is also available from conftest.py
from tests.conftest import TEST_CONFIG_PATH


def test_health_endpoint(client):
    """Test /health endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


def test_root_endpoint(client):
    """Test / endpoint returns server info."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "tilesets" in data
    assert "version" in data
    assert "2.5.0-event" in data["version"]
    optimizations = " ".join(data.get("optimizations", []))
    assert "tar" in optimizations.lower()


@pytest.mark.parametrize(
    "tileset_name, expected_type",
    [
        ("test_directory", "directory"),
        ("test_tar_uncompressed", "tar"),
        ("test_tar_nested", "tar"),
    ],
)
def test_tileset_info(client, tileset_name, expected_type):
    """Test /tilesets/{name} endpoint."""
    response = client.get(f"/tilesets/{tileset_name}")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == tileset_name
    assert data["source_type"] == expected_type


@pytest.mark.parametrize(
    "tileset_name, z, x, y, expected_type",
    [
        ("test_directory", 10, 0, "0.png", "directory"),
        ("test_tar_uncompressed", 10, 0, "0.png", "tar"),
        ("test_tar_compressed", 10, 0, "0.png", "tar"),
        ("test_tar_nested", 10, 0, "0.png", "tar"),
    ],
)
def test_serve_tile(client, tileset_name, z, x, y, expected_type):
    """Test serving a specific tile."""
    response = client.get(f"/{tileset_name}/{z}/{x}/{y}")
    assert response.status_code == 200
    assert "X-Tileset" in response.headers
    assert response.headers["X-Tileset"] == tileset_name
    assert "X-Source-Type" in response.headers
    assert response.headers["X-Source-Type"] == expected_type
    assert "Cache-Control" in response.headers
    content_type = response.headers.get("Content-Type", "")
    assert "image/" in content_type
    assert len(response.content) > 0


def test_tile_not_found(client):
    """Test that nonexistent tiles return 404."""
    response = client.get("/test_directory/99/9999/9999.png")
    assert response.status_code == 404


def test_invalid_tileset(client):
    """Test that invalid tileset names return 404."""
    response = client.get("/nonexistent_tileset/10/0/0.png")
    assert response.status_code == 404
    data = response.json()
    assert "not found" in data["message"].lower()


# ============================================================================
# Admin Endpoint Tests
# ============================================================================


def test_admin_status_tar_tileset(client):
    """Test GET /admin/status/{name} for a tar-based tileset."""
    response = client.get("/admin/status/test_tar_uncompressed")
    assert response.status_code == 200
    data = response.json()
    assert data["tileset"] == "test_tar_uncompressed"
    assert data["status"] == "ready"
    assert "tile_count" in data
    assert "zoom_levels" in data


def test_admin_status_directory_tileset_rejected(client):
    """Test that /admin/status returns 400 for directory-based tilesets."""
    response = client.get("/admin/status/test_directory")
    assert response.status_code == 400
    assert "not a tar-based tileset" in response.json()["detail"]


def test_admin_status_nonexistent_tileset(client):
    """Test that /admin/status returns 404 for nonexistent tileset."""
    response = client.get("/admin/status/nonexistent")
    assert response.status_code == 404


def test_admin_rebuild_tar_tileset(client):
    """Test POST /admin/rebuild/{name} for a tar-based tileset."""
    response = client.post("/admin/rebuild/test_tar_uncompressed")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "test_tar_uncompressed" in data["message"]
    assert "index_status" in data
    assert data["index_status"]["status"] == "ready"


def test_admin_rebuild_directory_tileset_rejected(client):
    """Test that /admin/rebuild returns 400 for directory-based tilesets."""
    response = client.post("/admin/rebuild/test_directory")
    assert response.status_code == 400
    assert "not a tar-based tileset" in response.json()["detail"]


def test_admin_rebuild_nonexistent_tileset(client):
    """Test that /admin/rebuild returns 404 for nonexistent tileset."""
    response = client.post("/admin/rebuild/nonexistent")
    assert response.status_code == 404


# ============================================================================
# Error Response Tests
# ============================================================================


def test_invalid_zoom_level_error(client):
    """Test that requesting a tile with invalid zoom level returns 404 with proper error."""
    # Zoom level 99 is outside the valid range (typically 1-25)
    response = client.get("/test_directory/99/0/0.png")
    assert response.status_code == 404
    data = response.json()
    assert data["error"] == "INVALID_ZOOM_LEVEL"
    assert "99" in data["message"]


def test_invalid_x_coordinate_error(client):
    """Test that requesting a tile with invalid X coordinate returns 404."""
    # At zoom 10, max coordinate is 2^10 - 1 = 1023
    response = client.get("/test_directory/10/9999/0.png")
    assert response.status_code == 404
    data = response.json()
    assert data["error"] == "INVALID_COORDINATE"
    assert "9999" in data["message"]


def test_invalid_y_coordinate_error(client):
    """Test that requesting a tile with invalid Y coordinate returns 404."""
    # At zoom 10, max coordinate is 2^10 - 1 = 1023
    response = client.get("/test_directory/10/0/9999.png")
    assert response.status_code == 404
    data = response.json()
    assert data["error"] == "INVALID_COORDINATE"
    assert "9999" in data["message"]


def test_tile_not_found_error_format(client):
    """Test that tile not found returns proper error format."""
    response = client.get("/test_directory/10/0/999.png")
    assert response.status_code == 404
    data = response.json()
    assert data["error"] == "TILE_NOT_FOUND"
    assert "path" in data


def test_tileset_not_found_error_format(client):
    """Test that tileset not found returns proper error format with available tilesets."""
    response = client.get("/nonexistent/10/0/0.png")
    assert response.status_code == 404
    data = response.json()
    assert data["error"] == "TILESET_NOT_FOUND"
    assert "Available tilesets" in data["message"]


# ============================================================================
# Header Tests
# ============================================================================


def test_security_headers(client):
    """Test that security headers are present in responses."""
    response = client.get("/health")
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("Referrer-Policy") == "no-referrer"


def test_cache_headers_on_tile(client):
    """Test that proper cache headers are set on tile responses."""
    response = client.get("/test_directory/10/0/0.png")
    assert response.status_code == 200
    assert "public" in response.headers.get("Cache-Control", "")
    assert "max-age" in response.headers.get("Cache-Control", "")
    assert "ETag" in response.headers
    assert "Last-Modified" in response.headers


# ============================================================================
# App Configuration Tests
# ============================================================================


def test_create_app_without_scan():
    """Test that app can be created with do_scan=False."""
    if not TEST_CONFIG_PATH.exists():
        pytest.skip("Test config not available")

    app = create_app(config_path=str(TEST_CONFIG_PATH), do_scan=False)
    with TestClient(app) as test_client:
        # Health check should still work
        response = test_client.get("/health")
        assert response.status_code == 200

        # Tileset info should have default zoom values
        response = test_client.get("/tilesets/test_directory")
        assert response.status_code == 200
        data = response.json()
        # When scanning is disabled, zoom_range uses defaults
        assert "zoom_range" in data
