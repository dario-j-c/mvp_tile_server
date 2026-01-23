"""
MVP Tile Server - Event-optimized high-performance tile server.

A FastAPI-based tile server supporting both directory and tar archive sources,
optimized for local event deployment with looping displays and interactive maps.

Usage:
    # As a module
    python -m app config.json -p 8000

    # With uvicorn directly
    uvicorn app.main:get_app --factory --host 0.0.0.0 --port 8000

    # Programmatically
    from app import create_app
    app = create_app("config.json")
"""

from app.main import create_app, get_app

__all__ = ["create_app", "get_app"]
