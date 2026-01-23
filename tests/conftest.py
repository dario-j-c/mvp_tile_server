"""
Pytest configuration and shared fixtures for tile server tests.

This module generates the test configuration dynamically to avoid
hardcoded absolute paths that break when the repo is cloned elsewhere.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

# Paths relative to project root
PROJECT_ROOT = Path(__file__).parent.parent
TEST_DATA_DIR = PROJECT_ROOT / "test_data"
TEST_CONFIG_PATH = TEST_DATA_DIR / "test_config.json"


def generate_test_config() -> Path:
    """
    Generate test_config.json with correct absolute paths for current environment.

    Returns:
        Path to the generated config file.
    """
    if not TEST_DATA_DIR.exists():
        pytest.fail(
            f"Test data directory not found at {TEST_DATA_DIR}. "
            "Ensure test_data/ is present in the repository."
        )

    # Build config with resolved absolute paths
    config = {
        "tilesets": {
            "test_directory": str((TEST_DATA_DIR / "directory_tiles").resolve()),
            "test_tar_uncompressed": str(
                (TEST_DATA_DIR / "tiles_uncompressed.tar").resolve()
            ),
            "test_tar_compressed": str(
                (TEST_DATA_DIR / "tiles_compressed.tar.gz").resolve()
            ),
            "test_tar_nested": {
                "source": str((TEST_DATA_DIR / "tiles_nested.tar").resolve()),
                "base_path": "map_data/tiles",
            },
            "test_directory_2": str((TEST_DATA_DIR / "directory_tiles_2").resolve()),
        }
    }

    # Validate that all paths exist
    for name, value in config["tilesets"].items():
        if isinstance(value, dict):
            path = Path(value["source"])
        else:
            path = Path(value)

        if not path.exists():
            pytest.fail(f"Test data missing: {path}.")

    # Write config
    with open(TEST_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

    return TEST_CONFIG_PATH


@pytest.fixture(scope="session", autouse=True)
def setup_test_config():
    """
    Session-scoped fixture that generates test config before any tests run.

    This ensures the config has correct paths for the current environment.
    """
    generate_test_config()
    yield
    # Optionally clean up the generated config after tests
    # TEST_CONFIG_PATH.unlink(missing_ok=True)


@pytest.fixture(scope="module")
def client():
    """
    Create a TestClient instance for the FastAPI app.

    This fixture is shared across all tests in a module.
    """
    app = create_app(config_path=str(TEST_CONFIG_PATH), do_scan=True)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def client_no_scan():
    """
    Create a TestClient without initial tile scanning.

    Useful for testing startup behavior and admin endpoints.
    """
    app = create_app(config_path=str(TEST_CONFIG_PATH), do_scan=False)
    with TestClient(app) as test_client:
        yield test_client
