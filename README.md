# Introduction
This is a MVP to ensure we can serve multiple tile sets to another app locally.

Supports serving different tile sets from separate directories via a JSON configuration file.

In the future, another methodology may be used (for example serving MBTiles, Cloud Optimised GeoTiffs), but this serves the purpose of allowing work to be done while this occurs.

# Usage

To setup the tile server, create a configuration file and run:

```bash
python3 main.py [config_file] -p [port to use] -b [address to bind]
```

- The config file defaults to `tilesets.json`
- The port defaults to 8000
- The address defaults to 0.0.0.0

## Configuration File

Create a JSON file defining your tilesets:
```json
{
  "tilesets": {
    "osm": "/path/to/osm/tiles",
    "satellite": "/path/to/satellite/tiles",
    "topo": "./topographic_tiles"
  }
}
```

**Requirements:**

- Tileset names must be alphanumeric with hyphens/underscores, cannot start with a digit
- Paths must exist and be directories
- Each tileset directory should contain the standard `{z}/{x}/{y.ext}` structure

## File Structure

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
