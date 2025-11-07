"""Compatibility shim: import planner functionality from backend package.

This keeps existing importers that do `from src.planner import plan_route` working.
"""

from backend.planner import plan_route

__all__ = ["plan_route"]
