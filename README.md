# Introduction
This is a MVP to ensure we can serve multiple tile sets to another app locally.

Supports serving different tile sets from:
- **Directories** - Traditional file system folders
- **Tar Archives** - Uncompressed or compressed tar files (no extraction required)

In the future, another methodology may be used (for example serving MBTiles, Cloud Optimised GeoTiffs), but this serves the purpose of allowing work to be done while this occurs.

## Key Features
- Multiple tileset support with independent configurations
- Serve tiles directly from tar archives without extraction
- Auto-detection of tile structure within tar files
- Support for uncompressed (.tar) and compressed (.tar.gz, .tar.bz2, .tar.xz) formats
- Fast(ish) tar inspection utility for configuration assistance

# Usage

To setup the tile server, create a configuration file and run:

```bash
python3 main.py [config_file] -p [port to use] -b [address to bind]
```

- The config file defaults to `tilesets.json`
- The port defaults to 8000
- The address defaults to 0.0.0.0

## Configuration File

Create a JSON file defining your tilesets. The server supports both **directory** and **tar archive** sources.

### Directory-Based Configuration (Original Method)
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

## Inspecting Tar Archives

Before configuring tar archives, use the inspection utility to understand the structure:

```bash
python3 inspect_tar.py /path/to/your/tiles.tar
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
TOP-LEVEL STRUCTURE
----------------------------------------------------------------------
  tiles/

----------------------------------------------------------------------
TILE DETECTION
----------------------------------------------------------------------

✓ Found tiles matching {z}/{x}/{y.ext} pattern!
  Zoom levels detected: [10, 11, 12, 13]
  File extensions: ['.png', '.webp']

  Sample tile paths:
    tiles/10/512/256.png
    tiles/11/1024/512.png
    tiles/12/2048/1024.webp

----------------------------------------------------------------------
RECOMMENDED CONFIGURATION
----------------------------------------------------------------------

✓ Detected base path: 'tiles'

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

This utility helps you:
- Verify tar file contains valid tiles
- Identify the correct `base_path` for nested structures
- Detect compression type and get performance warnings
- Preview sample tiles before deployment

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
Tar archives must contain the same `{z}/{x}/{y.ext}` structure, either at the root or within a subdirectory:

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

**Important:** The server accommodates **one large tar file per tileset**. Do not split tiles across multiple tar files for a single tileset.

## URLs

Tiles are served at: `http://localhost:8000/{tileset_name}/{z}/{x}/{y.ext}`

Examples:
- `http://localhost:8000/osm/12/1234/5678.png`
- `http://localhost:8000/satellite/10/512/256.jpg`

## Additional Endpoints

- `GET /` - Server status and tileset overview
- `GET /tilesets/{tileset_name}` - Detailed tileset information
- `GET /health` - Health check

## Error Handling

The server validates all tileset configurations on startup and will show comprehensive error messages if paths don't exist or names are invalid.

Fix all reported issues before the server will start.

## Performance Considerations

### Tar Archive Performance

The server serves tiles **directly from tar archives without extraction**, streaming tile data on demand. Performance should vary by compression type.

**Key Points:**
- **Uncompressed `.tar` files are strongly recommended** for production use
- The performance overhead is likely minimal for uncompressed archives
- Compressed formats will require CPU-intensive decompression for each tile request
- The server warns on startup when using compressed tar files
- Startup time: Building tar indexes takes ~10-50ms per archive (one-time **per worker**)

### When to Use Tar Archives

**Use tar archives when:**
- Disk space is limited and you cannot extract tiles
- Filesystem has inode limits (tar = 1 file instead of thousands)
- Read-only environment where extraction is not possible
- Simplifying deployment (single file instead of directory tree)

**Use directories when:**
- Maximum performance is critical
- Disk space is plentiful
- Tiles need frequent updates (directories are easier to modify)

### Optimization Tips

1. **Use uncompressed tar files** for production deployments
2. **Disable startup scan** with `--no-scan` flag for faster boot (especially with large tar files)
3. **Use multiple workers** (`--workers 4`) to handle concurrent tile requests
4. **Pre-build tar files** with standard `tar -cf` command (no compression)

Example creating an optimized tar file:
```bash
# Create uncompressed tar (fast serving)
tar -cf tiles.tar -C /source/path .

# Avoid compression for production
# tar -czf tiles.tar.gz -C /source/path .  # Slower!
```
