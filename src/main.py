"""Compat shim: keep `src.main` importable while the code lives in `backend`.
"""

from backend.main import app  # re-export FastAPI app

__all__ = ["app"]
