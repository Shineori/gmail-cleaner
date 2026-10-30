"""API routes module."""

from .status import router as status_router
from .actions import router as actions_router
from .safety import router as safety_router

__all__ = ["status_router", "actions_router", "safety_router"]
