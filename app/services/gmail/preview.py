"""
Action Preview
--------------
Dry run for trash / archive / mark-read: counts exactly what would be touched,
which senders are protected and skipped, and shows a few sample emails.
Nothing is modified.
"""

from __future__ import annotations

from app.services import protected
from app.services.auth import get_gmail_service
from app.services.gmail.helpers import build_gmail_query
from app.services.gmail.safety import fetch_samples, list_message_ids

MAX_SENDERS = 500


def preview_action(
    action: str,
    senders: list[str] | None = None,
    count: int = 0,
    filters: dict | None = None,
) -> dict:
    """Return what an action would do, without doing it."""
    if action not in ("trash", "archive", "mark_read"):
        return {"success": False, "message": f"Unknown action: {action}"}

    try:
        entries = protected.load()
    except protected.ProtectedListError as e:
        return {"success": False, "message": f"Protected senders list is unreadable: {e}"}

    service, error = get_gmail_service()
    if error:
        return {"success": False, "message": error}

    per_sender: list[dict] = []
    skipped_senders: list[str] = []
    all_ids: list[str] = []

    if action == "mark_read":
        query = "is:unread"
        if fq := build_gmail_query(filters):
            query = f"{query} {fq}"
        query = protected.with_protection(query, entries)
        all_ids = list_message_ids(service, query, limit=count if count > 0 else None)
    else:
        senders = [s for s in (senders or []) if isinstance(s, str) and s.strip()]
        if not senders:
            return {"success": False, "message": "No senders selected"}
        if len(senders) > MAX_SENDERS:
            return {"success": False, "message": f"Too many senders (max {MAX_SENDERS})"}
        for sender in senders:
            if protected.is_protected(sender, entries):
                skipped_senders.append(sender)
                continue
            base = f"from:{sender}" + (" in:inbox" if action == "archive" else "")
            ids = list_message_ids(service, protected.with_protection(base, entries))
            per_sender.append({"sender": sender, "count": len(ids)})
            all_ids.extend(ids)
        all_ids = list(dict.fromkeys(all_ids))
        per_sender.sort(key=lambda r: r["count"], reverse=True)

    return {
        "success": True,
        "action": action,
        "total": len(all_ids),
        "per_sender": per_sender,
        "skipped_senders": skipped_senders,
        "protected_count": len(entries),
        "samples": fetch_samples(service, all_ids, limit=5),
    }
