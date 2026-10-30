"""
Action History and Undo
-----------------------
Every trash / archive / mark-read run is recorded with the exact message IDs
it touched, so it can be reversed later.

    trash      -> undo removes the TRASH label (Gmail keeps Trash for 30 days)
    archive    -> undo puts the INBOX label back
    mark_read  -> undo puts the UNREAD label back
"""

from __future__ import annotations

import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError

from app.services.storage import read_json, write_json

logger = logging.getLogger(__name__)

FILENAME = "history.json"
MAX_ENTRIES = 50
MAX_IDS_PER_ENTRY = 100_000
TRASH_RETENTION_DAYS = 30

ACTION_LABELS = {
    "trash": "Moved to Trash",
    "archive": "Archived",
    "mark_read": "Marked as read",
}

UNDO_BODY = {
    "trash": {"removeLabelIds": ["TRASH"]},
    "archive": {"addLabelIds": ["INBOX"]},
    "mark_read": {"addLabelIds": ["UNREAD"]},
}

_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load() -> list[dict]:
    try:
        data = read_json(FILENAME, {"entries": []})
    except ValueError:
        logger.exception("history.json is unreadable; starting a fresh history")
        return []
    entries = data.get("entries", []) if isinstance(data, dict) else []
    return [e for e in entries if isinstance(e, dict) and e.get("id")]


def _save(entries: list[dict]) -> None:
    write_json(FILENAME, {"entries": entries[:MAX_ENTRIES]})


def record(
    action: str,
    message_ids: list[str],
    senders: list[str] | None = None,
    detail: str = "",
) -> dict | None:
    """Store a finished action. Never raises: history must not break the action."""
    if action not in UNDO_BODY or not message_ids:
        return None
    try:
        ids = list(dict.fromkeys(message_ids))
        entry = {
            "id": secrets.token_hex(6),
            "action": action,
            "timestamp": _now().isoformat(timespec="seconds"),
            "count": len(ids),
            "senders": (senders or [])[:50],
            "sender_count": len(senders or []),
            "detail": detail,
            "message_ids": ids[:MAX_IDS_PER_ENTRY],
            "truncated": len(ids) > MAX_IDS_PER_ENTRY,
            "undone": False,
            "undone_at": None,
            "undo_result": None,
        }
        with _lock:
            entries = _load()
            entries.insert(0, entry)
            _save(entries)
        return entry
    except Exception:
        logger.exception("Failed to record history entry")
        return None


def _is_expired(entry: dict) -> bool:
    if entry.get("action") != "trash":
        return False
    try:
        ts = datetime.fromisoformat(entry["timestamp"])
    except (KeyError, ValueError):
        return False
    return _now() - ts > timedelta(days=TRASH_RETENTION_DAYS)


def public_view(entry: dict) -> dict:
    """Entry without the (potentially huge) ID list, plus display fields."""
    view = {k: v for k, v in entry.items() if k != "message_ids"}
    view["label"] = ACTION_LABELS.get(entry.get("action"), entry.get("action"))
    view["expired"] = _is_expired(entry)
    view["can_undo"] = not entry.get("undone") and not view["expired"]
    return view


def list_entries() -> list[dict]:
    with _lock:
        return [public_view(e) for e in _load()]


def _untrash_individually(service, ids: list[str]) -> tuple[int, int]:
    """Fallback: messages.untrash one by one in batches. Returns (ok, failed)."""
    ok = 0
    failed = 0

    def callback(request_id, response, exception):
        nonlocal ok, failed
        if exception:
            failed += 1
        else:
            ok += 1

    for i in range(0, len(ids), 100):
        batch = service.new_batch_http_request(callback=callback)
        for msg_id in ids[i : i + 100]:
            batch.add(service.users().messages().untrash(userId="me", id=msg_id))
        batch.execute()
    return ok, failed


def _apply_undo(service, action: str, ids: list[str]) -> tuple[int, int]:
    """Reverse an action for the given IDs. Returns (restored, failed)."""
    body = UNDO_BODY[action]
    restored = 0
    failed = 0
    for i in range(0, len(ids), 1000):
        chunk = ids[i : i + 1000]
        try:
            service.users().messages().batchModify(
                userId="me", body={"ids": chunk, **body}
            ).execute()
            restored += len(chunk)
        except HttpError as e:
            # A chunk can fail if some messages were permanently deleted.
            # For trash we retry message by message so the rest still come back.
            if action == "trash":
                ok, bad = _untrash_individually(service, chunk)
                restored += ok
                failed += bad
            else:
                logger.warning("Undo chunk failed: %s", e)
                failed += len(chunk)
    return restored, failed


def undo(service, entry_id: str) -> dict:
    """Undo one history entry."""
    with _lock:
        entries = _load()
        entry = next((e for e in entries if e.get("id") == entry_id), None)
        if entry is None:
            return {"success": False, "message": "History entry not found"}
        if entry.get("undone"):
            return {"success": False, "message": "This action was already undone"}
        if _is_expired(entry):
            return {
                "success": False,
                "message": "Too old to undo: Gmail empties Trash after 30 days",
            }

        restored, failed = _apply_undo(service, entry["action"], entry["message_ids"])

        entry["undone"] = True
        entry["undone_at"] = _now().isoformat(timespec="seconds")
        entry["undo_result"] = {"restored": restored, "failed": failed}
        _save(entries)

    verb = {"trash": "Restored", "archive": "Moved back to inbox", "mark_read": "Marked unread"}[
        entry["action"]
    ]
    message = f"{verb}: {restored} emails"
    if failed:
        message += f" ({failed} could not be restored, probably already deleted forever)"
    return {
        "success": restored > 0 or failed == 0,
        "restored": restored,
        "failed": failed,
        "message": message,
        "entry": public_view(entry),
    }
