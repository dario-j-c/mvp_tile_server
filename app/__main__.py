#!/usr/bin/env python3
"""
Entry point for running the tile server as a module.

Usage:
    python -m app [config_file] -p [port] -b [bind address]
    uv run python -m app [config_file] -p [port] -b [bind address]

    Or run directly with uvicorn (for production events):
    uvicorn app.main:get_app --factory --host 0.0.0.0 --port 8000 --workers 4
"""

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import uvicorn

from app.config import load_tileset_config, scan_all_tilesets
from app.tar_manager import load_or_build_tar_index

logger = logging.getLogger("event_tile_server")


def parse_arguments() -> argparse.Namespace:
    """
    Parse command-line arguments.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        description="Event-optimized multi-tileset tile server using FastAPI/Uvicorn."
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="config.json",
        help="Path to tileset configuration JSON file (default: config.json).",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=8000,
        help="Port to bind to (default: 8000).",
    )
    parser.add_argument(
        "-b",
        "--bind",
        default="0.0.0.0",
        help="Address to bind to (default: 0.0.0.0 for local events).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of worker processes for concurrent tile serving (default: 4).",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development (not recommended during events).",
    )
    parser.add_argument(
        "--event-mode",
        action="store_true",
        help="Enable event production mode with optimized logging and stability.",
    )
    parser.add_argument(
        "--no-scan",
        action="store_true",
        help="Skip startup scan for faster boot (uses default zoom bounds).",
    )
    return parser.parse_args()


def main() -> None:
    """
    Main entry point: validates config and starts the Uvicorn server.

    Exits with code 1 on configuration errors or server failures.
    """
    args = parse_arguments()

    # Configure logging with level prefix
    log_format = "%(levelname)s:\t[MAIN] %(message)s"
    if args.event_mode:
        logging.basicConfig(level=logging.WARNING, format=log_format)
    else:
        logging.basicConfig(level=logging.INFO, format=log_format)

    # Validate configuration early to avoid worker crashes
    try:
        logger.info("Validating configuration...")
        tilesets = load_tileset_config(args.config)
        logger.info(
            "Configuration valid: %d tilesets loaded successfully", len(tilesets)
        )
        for name, info in tilesets.items():
            logger.info(
                "  - %s: %s (%s)", name, info["source_path"], info["source_type"]
            )
    except ValueError as e:
        print("\nConfiguration Validation Failed:")
        print("=" * 60)
        print(str(e))
        print("=" * 60)
        print("\nPlease fix the above issues and try again.")
        print("Tip: Use absolute paths to avoid path resolution issues.")
        sys.exit(1)
    except Exception as e:
        print(f"\nUnexpected error loading configuration: {e}")
        sys.exit(1)

    # Pre-scan directory tilesets and build unified tar indexes in MAIN process.
    # By building tar indexes here, all workers can instantly load the cached .idx
    # files instead of parsing the tar archives redundantly.
    metadata_file = None
    if not args.no_scan:
        directory_tilesets = {
            name: info
            for name, info in tilesets.items()
            if info["source_type"] == "directory"
        }
        if directory_tilesets:
            logger.info(
                "Scanning %d directory tileset(s) for metadata...",
                len(directory_tilesets),
            )
            dir_metadata = scan_all_tilesets(directory_tilesets)
            try:
                fd, metadata_file = tempfile.mkstemp(
                    suffix=".json", prefix="tile_metadata_"
                )
                with os.fdopen(fd, "w") as f:
                    json.dump(dir_metadata, f)
                logger.info("Scan complete. Metadata saved for workers.")
            except OSError as e:
                logger.warning(
                    "Could not write pre-scan metadata to temp file (%s). "
                    "Workers will scan directory tilesets independently — startup will be slower.",
                    e,
                )
                metadata_file = None
        else:
            logger.info("No directory tilesets to pre-scan.")

        # Build tar indexes
        tar_tilesets = {
            name: info
            for name, info in tilesets.items()
            if info["source_type"] == "tar"
        }
        if tar_tilesets:
            unique_tars = set(info["source_path"] for info in tar_tilesets.values())
            logger.info(
                "Building/verifying unified cache for %d tar archive(s)...",
                len(unique_tars),
            )
            for tar_path in unique_tars:
                try:
                    load_or_build_tar_index(Path(tar_path))
                except Exception as e:
                    logger.warning(
                        "Failed to pre-build tar index for %s: %s", tar_path, e
                    )
        else:
            logger.debug("No tar tilesets to pre-build.")

    # Export env for worker factory to consume
    os.environ["CONFIG_PATH"] = args.config
    os.environ["TILE_SCAN"] = "0"  # Workers never scan, MAIN already did
    os.environ["EVENT_MODE"] = "1" if args.event_mode else "0"
    if metadata_file:
        os.environ["TILE_METADATA_FILE"] = metadata_file

    print("\n" + "=" * 50)
    print("Starting Multi-Tileset Event Tile Server")
    print(f"Loading configuration from: {os.path.abspath(args.config)}")
    print(f"Listening on: http://{args.bind}:{args.port}")
    print(f"Using {args.workers} worker processes for optimal tile serving")
    print("Startup scan: {}".format("disabled" if args.no_scan else "enabled"))
    print(
        "Optimized for: Multiple tilesets, zoom levels 1-25, looping displays, interactive maps"
    )

    if args.event_mode:
        print("Event production mode: Enhanced stability and minimal logging enabled.")

    print("=" * 50 + "\n")

    # Uvicorn configuration, tuned for event stability and performance
    uvicorn_config = {
        "app": "app.main:get_app",
        "factory": True,
        "host": args.bind,
        "port": args.port,
        "workers": args.workers,
        "reload": args.reload,
        "loop": "uvloop",
        "http": "httptools",
        "limit_concurrency": 100,
        "backlog": 256,
        "timeout_keep_alive": 300,
        "limit_max_requests": 50000,
    }

    if args.event_mode:
        uvicorn_config.update(
            {
                "log_level": "warning",
                "access_log": False,
            }
        )
    else:
        uvicorn_config.update(
            {
                "log_level": "info",
                "access_log": True,
            }
        )

    try:
        uvicorn.run(**uvicorn_config)  # pyrefly: ignore
    except KeyboardInterrupt:
        print("\nEvent tile server stopped.")
    except Exception as e:
        print(f"Server encountered a critical error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        # Clean up temp metadata file
        if metadata_file and os.path.exists(metadata_file):
            try:
                os.remove(metadata_file)
            except Exception:
                pass  # Best effort cleanup


if __name__ == "__main__":
    main()
