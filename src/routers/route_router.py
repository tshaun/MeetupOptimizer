"""Compatibility shim: re-export the backend router module so imports that
use `from src.routers import route_router` continue to work.

This keeps the runtime stable while source was moved to `backend/routers`.
"""

from backend.routers import route_router as route_router

__all__ = ["route_router"]
