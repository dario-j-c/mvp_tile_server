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
import logging
import os
import sys

import uvicorn

from app.config import load_tileset_config

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
        default="tilesets.json",
        help="Path to tileset configuration JSON file (default: tilesets.json).",
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

    # Configure logging level
    if args.event_mode:
        logging.basicConfig(level=logging.WARNING)
    else:
        logging.basicConfig(level=logging.INFO)

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

    # Export env for worker factory to consume
    os.environ["CONFIG_PATH"] = args.config
    os.environ["TILE_SCAN"] = "0" if args.no_scan else "1"

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
        uvicorn.run(**uvicorn_config)
    except KeyboardInterrupt:
        print("\nEvent tile server stopped.")
    except Exception as e:
        print(f"Server encountered a critical error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
