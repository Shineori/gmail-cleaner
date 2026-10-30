"""
Safety API Routes
-----------------
Protected senders, action history + undo, previews, and the pasted
OAuth redirect used when the app runs on a server.
"""

import logging

from fastapi import APIRouter, HTTPException, status

from app.models import OAuthPasteRequest, PreviewRequest, ProtectedSenderRequest
from app.services import history, protected
from app.services.auth import get_gmail_service, submit_pasted_callback
from app.services.gmail.preview import preview_action

router = APIRouter(prefix="/api", tags=["Safety"])
logger = logging.getLogger(__name__)


def _load_protected() -> list[str]:
    try:
        return protected.load()
    except protected.ProtectedListError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Protected senders list is unreadable: {e}",
        ) from e


# ----- Protected senders -----


@router.get("/protected")
def api_get_protected():
    return {"senders": _load_protected()}


@router.post("/protected")
def api_add_protected(request: ProtectedSenderRequest):
    try:
        return {"senders": protected.add(request.sender)}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except protected.ProtectedListError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        ) from e


@router.post("/protected/remove")
def api_remove_protected(request: ProtectedSenderRequest):
    try:
        return {"senders": protected.remove(request.sender)}
    except protected.ProtectedListError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        ) from e


# ----- History + undo -----


@router.get("/history")
def api_history():
    return {"entries": history.list_entries()}


@router.post("/history/{entry_id}/undo")
def api_undo(entry_id: str):
    if not entry_id.isalnum() or len(entry_id) > 32:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bad id")
    service, error = get_gmail_service()
    if error:
        return {"success": False, "message": error}
    try:
        return history.undo(service, entry_id)
    except Exception as e:
        logger.exception("Undo failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Undo failed, check the logs",
        ) from e


# ----- Preview -----


@router.post("/preview")
def api_preview(request: PreviewRequest):
    filters = request.filters.model_dump(exclude_none=True) if request.filters else None
    try:
        return preview_action(request.action, request.senders, request.count, filters)
    except Exception as e:
        logger.exception("Preview failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Preview failed, check the logs",
        ) from e


# ----- Remote sign-in -----


@router.post("/oauth-paste")
def api_oauth_paste(request: OAuthPasteRequest):
    return submit_pasted_callback(request.url)
