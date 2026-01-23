# MVP Tile Server

A high-performance FastAPI tile server supporting multiple tilesets from directories and tar archives.

## Overview

This is an MVP tile server designed to serve multiple tile sets locally. It supports:

- **Directories** - Traditional file system folders
- **Tar Archives** - Uncompressed or compressed tar files (no extraction required)

In the future, other formats may be supported (MBTiles, Cloud Optimised GeoTiffs), but this serves the immediate need.

## Key Features

- Multiple tileset support with independent configurations
- Serve tiles directly from tar archives without extraction
- Auto-detection of tile structure within tar files
- Support for uncompressed (.tar) and compressed (.tar.gz, .tar.bz2, .tar.xz) formats
- Admin endpoints for tar index management
- Fast(ish) tar inspection utility for configuration assistance

## Installation

```bash
# Using uv (recommended)
uv sync

# Or using pip
pip install -e .
```

## Usage

Run the tile server with a configuration file:

```bash
# Using the app module (recommended)
python -m app [config_file] -p [port] -b [address]

# Or using uv
uv run python -m app tilesets.json -p 8000
```

**Options:**
- `config_file` - Path to configuration JSON (default: `tilesets.json`)
- `-p, --port` - Port to bind (default: `8000`)
- `-b, --bind` - Address to bind (default: `0.0.0.0`)
- `-w, --workers` - Number of uvicorn workers (default: `4`)
- `--no-scan` - Skip initial tile scanning for faster startup
- `--reload` - Enable auto-reload for development

## Project Structure

```
mvp_tile_server/
├── app/                    # Main application package
│   ├── __init__.py         # Package exports
│   ├── __main__.py         # CLI entry point
│   ├── config.py           # Configuration loading and validation
│   ├── exceptions.py       # Custom exception classes
│   ├── main.py             # FastAPI application and routes
│   ├── tar_manager.py      # Tar archive handling
│   └── utils.py            # Utility functions
├── tests/                  # Test suite
│   ├── test_integration.py # API endpoint tests
│   ├── test_unit.py        # Unit tests
│   └── test_property.py    # Property-based tests
├── inspect_tar.py          # Tar inspection utility
└── tilesets.json           # Example configuration
```

## Configuration File

Create a JSON file defining your tilesets. The server supports both **directory** and **tar archive** sources.

### Directory-Based Configuration
```json
{
  "tilesets": {
    "osm": "/path/to/osm/tiles",
    "satellite": "/path/to/satellite/tiles",
    "topo": "./topographic_tiles"
  }
}
```

### Tar Archive Configuration

#### Simple (Auto-Detect Structure)
```json
{
  "tilesets": {
    "osm": "/path/to/osm_tiles.tar",
    "satellite": "/path/to/satellite.tar.gz"
  }
}
```

#### Explicit Base Path (For Nested Structures)
```json
{
  "tilesets": {
    "topo": {
      "source": "/path/to/topo.tar",
      "base_path": "tiles"
    },
    "aerial": {
      "source": "/path/to/aerial.tar.gz",
      "base_path": "data/map_tiles"
    }
  }
}
```

### Mixed Configuration (Both Types)
```json
{
  "tilesets": {
    "osm": "/path/to/osm/tiles",
    "satellite": "/path/to/satellite.tar",
    "topo": {
      "source": "/path/to/topo.tar.gz",
      "base_path": "tiles"
    }
  }
}
```

**Requirements:**

- Tileset names must be alphanumeric with hyphens/underscores, cannot start with a digit
- Paths must exist (directories or tar files)
- Each source must contain the standard `{z}/{x}/{y.ext}` structure
- For tar files, tiles can be at root or in a subdirectory (specify with `base_path`)

## API Endpoints

### Tile Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /tiles/{tileset}/{z}/{x}/{y.ext}` | Retrieve a tile |

### Information Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Server status and tileset overview |
| `GET /tilesets/{tileset_name}` | Detailed tileset information |
| `GET /health` | Health check |

### Admin Endpoints (Tar Archives)

| Endpoint | Description |
|----------|-------------|
| `POST /admin/rebuild/{tileset_name}` | Rebuild tar index |
| `GET /admin/status/{tileset_name}` | Get tar index status |

## Inspecting Tar Archives

Before configuring tar archives, use the inspection utility to understand the structure:

```bash
python inspect_tar.py /path/to/your/tiles.tar
```

**Options:**
- `--timeout 120` - Set inspection timeout in seconds (default: 60)
- `--max-members 5000` - Maximum archive members to scan (default: 1000)

**Example Output:**
```
TAR ARCHIVE INSPECTION RESULTS
======================================================================

File: /path/to/tiles.tar
Compression: uncompressed
Size: 2.3 GB
Members scanned: 1000
Scan time: 2.45s

----------------------------------------------------------------------
TILE DETECTION
----------------------------------------------------------------------

Found tiles matching {z}/{x}/{y.ext} pattern!
  Zoom levels detected: [10, 11, 12, 13]
  File extensions: ['.png', '.webp']

----------------------------------------------------------------------
RECOMMENDED CONFIGURATION
----------------------------------------------------------------------

Detected base path: 'tiles'

Add to your tilesets.json:

{
  "tilesets": {
    "your_tileset_name": {
      "source": "/path/to/tiles.tar",
      "base_path": "tiles"
    }
  }
}
```

## File Structure

### Directory Structure
Each tileset directory should follow the standard tile layout:

```
tileset_directory/
├── 1/
│   └── 0/
│       └── 0.png
├── 2/
│   ├── 0/
│   └── 1/
└── ...
```

### Tar Archive Structure
Tar archives must contain the same `{z}/{x}/{y.ext}` structure:

**Root-level tiles:**
```
tiles.tar
├── 10/
│   └── 512/
│       └── 256.png
├── 11/
│   └── 1024/
│       └── 512.png
└── ...
```

**Nested tiles (requires base_path):**
```
tiles.tar
├── README.txt
├── metadata/
│   └── info.json
└── tiles/              # <- This is your base_path
    ├── 10/
    │   └── 512/
    │       └── 256.png
    └── ...
```

**Note:** The server accommodates **one large tar file per tileset**. Do not split tiles across multiple tar files for a single tileset.

## Testing

Run the test suite:

```bash
# Run all tests
uv run pytest tests/ -v

# Run with coverage
uv run pytest tests/ --cov=app --cov-report=term-missing
```

The test suite includes:
- **Integration tests** - API endpoint testing with TestClient
- **Unit tests** - Individual function and class testing
- **Property-based tests** - Hypothesis-powered fuzzing

## Error Handling

The server validates all tileset configurations on startup and displays comprehensive error messages if paths don't exist or names are invalid.

**Error Response Format:**
```json
{
  "error": "tile_not_found",
  "message": "Tile not found at coordinates",
  "tileset": "osm",
  "z": 10,
  "x": 512,
  "y": 256,
  "extensions_tried": [".png", ".jpg", ".webp", ".pbf"]
}
```

## Performance Considerations

### Tar Archive Performance

The server serves tiles **directly from tar archives without extraction**, streaming tile data on demand.

**Recommendations:**
- **Uncompressed `.tar` files are strongly recommended** for production
- Compressed formats require CPU-intensive decompression per tile request
- The server warns on startup when using compressed tar files
- Startup time: Building tar indexes takes ~10-50ms per archive (one-time per worker)

### When to Use Tar Archives

**Use tar archives when:**
- Disk space is limited
- Filesystem has inode limits (tar = 1 file instead of thousands)
- Read-only environment where extraction is not possible
- Simplifying deployment (single file instead of directory tree)

**Use directories when:**
- Maximum performance is critical
- Tiles need frequent updates

### Optimization Tips

1. **Use uncompressed tar files** for production deployments
2. **Disable startup scan** with `--no-scan` flag for faster boot
3. **Use multiple workers** (`--workers 4`) to handle concurrent requests
4. **Pre-build tar files** with standard `tar -cf` command (no compression)

Example creating an optimized tar file:
```bash
# Create uncompressed tar (fast serving)
tar -cf tiles.tar -C /source/path .

# Avoid compression for production
# tar -czf tiles.tar.gz -C /source/path .  # Slower!
```

## Docker

The server can be run with Docker for containerized deployments.

### Quick Start with Docker Compose

```bash
# Using test data (default)
docker-compose up

# With custom tile data path
TILE_DATA_PATH=/path/to/your/tiles docker-compose up
```

### Building the Image

```bash
docker build -t tile-server .
```

### Running with Docker

```bash
# Run with mounted tile data and config
docker run -d \
  -p 8000:8000 \
  -v /path/to/tiles:/app/data:ro \
  -v /path/to/config.json:/app/config.json:ro \
  tile-server
```

### Docker Configuration

The container expects:
- **Config file** mounted at `/app/config.json`
- **Tile data** mounted under `/app/data/`

Edit `config.json` in the project root to define your tilesets. This file is mounted into the container and should reference container paths (`/app/data/...`). See `config_example.json` for a documented template.

Example `config.json`:

```json
{
  "tilesets": {
    "osm": "/app/data/osm",
    "satellite": "/app/data/satellite.tar"
  }
}
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TILE_SERVER_PORT` | `8000` | Host port mapping |
| `TILE_DATA_PATH` | `./test_data` | Host path to tile data |
| `LOG_LEVEL` | `info` | Logging verbosity |

### Production Example

```yaml
# docker-compose.override.yml
services:
  tile-server:
    ports:
      - "8000:8000"
    volumes:
      - ./production-config.json:/app/config.json:ro
      - /data/tiles:/app/data:ro
    restart: always
```

## License

MIT
