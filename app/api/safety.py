"""
Safety API Routes
-----------------
Protected senders, action history + undo, previews, and the pasted
OAuth redirect used when the app runs on a server.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status

from app.models import (
    OAuthPasteRequest,
    PreviewRequest,
    ProtectedSenderRequest,
    UnsubscribedRequest,
)
from app.services import history, protected, unsubscribed
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


# ----- Unsubscribe tracker -----


@router.get("/unsubscribed")
def api_unsubscribed():
    """Tracked unsubscribes without checking Gmail (fast)."""
    return {
        "grace_days": unsubscribed.GRACE_DAYS,
        "senders": [{"sender": k, **v} for k, v in unsubscribed.load().items()],
    }


@router.get("/unsubscribed/check")
def api_unsubscribed_check():
    """Check Gmail for mail that arrived after each unsubscribe."""
    service, error = get_gmail_service()
    if error:
        return {"success": False, "message": error, "senders": []}
    return {
        "success": True,
        "grace_days": unsubscribed.GRACE_DAYS,
        "senders": unsubscribed.check_all(service),
    }


@router.post("/unsubscribed")
def api_track_unsubscribed(request: UnsubscribedRequest):
    """Track a sender (opened an unsubscribe page, or unsubscribed elsewhere)."""
    if not unsubscribed.normalize_sender(request.sender):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Enter a sender domain (news.example.com) or address",
        )
    when = None
    if request.date:
        try:
            when = datetime.strptime(request.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bad date") from e
        if when > datetime.now(timezone.utc):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Date is in the future")
    entry = unsubscribed.record(request.sender, request.method, when)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not save"
        )
    return {"success": True, "entry": entry}


@router.post("/unsubscribed/forget")
def api_forget_unsubscribed(request: ProtectedSenderRequest):
    return {"success": unsubscribed.forget(request.sender)}


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
