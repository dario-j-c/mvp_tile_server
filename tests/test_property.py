"""Property-based tests for the tile server using Hypothesis."""

from hypothesis import given, settings
from hypothesis import strategies as st

# Note: The 'client' fixture is provided by conftest.py


# Strategy for generating valid zoom levels (based on test data)
zoom_strategy = st.integers(min_value=10, max_value=14)

# A strategy for tilesets defined in the test config
tileset_strategy = st.sampled_from(
    [
        "test_directory",
        "test_tar_uncompressed",
        "test_tar_compressed",
        "test_tar_nested",
    ]
)


@given(
    tileset=tileset_strategy,
    z=zoom_strategy,
    x=st.integers(min_value=0, max_value=(1 << 14) - 1),
    y=st.integers(min_value=0, max_value=(1 << 14) - 1),
)
@settings(max_examples=50, deadline=None)
def test_get_tile_property(client, tileset, z, x, y):
    """
    Test that valid tile requests either return a tile or a 404, but never a server error.
    """
    # Constrain x and y to be valid for the generated zoom level z
    max_coord = (1 << z) - 1
    if x > max_coord or y > max_coord:
        # If generated coordinates are out of bounds for this z, skip the test
        return

    response = client.get(f"/{tileset}/{z}/{x}/{y}.png")

    # A valid request should either find a tile (200) or not (404),
    # but it should not cause a server error (500).
    assert response.status_code in [200, 404]


@given(
    tileset=tileset_strategy,
    z=zoom_strategy,
    x=st.integers(min_value=1 << 14, max_value=(1 << 16)),  # Out of bounds for z
    y=st.integers(min_value=0, max_value=(1 << 14) - 1),
)
@settings(max_examples=20, deadline=None)
def test_get_invalid_tile_coords(client, tileset, z, x, y):
    """
    Test that requests with invalid coordinates consistently return 404.
    """
    max_coord = (1 << z) - 1
    if x <= max_coord:
        # Ensure x is actually out of bounds for this z
        return

    response = client.get(f"/{tileset}/{z}/{x}/{y}.png")
    assert response.status_code == 404
