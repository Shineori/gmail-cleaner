"""
Gmail Archive Operations
------------------------
Functions for archiving emails (removing from inbox).
"""

import time

from app.core import state
from app.services import history, protected
from app.services.auth import get_gmail_service
from app.services.gmail.safety import filter_protected_ids


def archive_emails_background(senders: list[str]):
    """Archive emails from selected senders (remove INBOX label)."""
    state.reset_archive()

    # Validate input
    if not senders or not isinstance(senders, list):
        state.archive_status["done"] = True
        state.archive_status["error"] = "No senders specified"
        return

    try:
        protected_entries = protected.load()
    except protected.ProtectedListError as e:
        state.archive_status["done"] = True
        state.archive_status["error"] = (
            f"Protected senders list is unreadable, nothing was archived ({e})"
        )
        return

    skipped_senders = [s for s in senders if protected.is_protected(s, protected_entries)]
    senders = [s for s in senders if s not in skipped_senders]
    state.archive_status["skipped_senders"] = skipped_senders
    if not senders:
        state.archive_status["progress"] = 100
        state.archive_status["done"] = True
        state.archive_status["message"] = (
            "All selected senders are protected, nothing was archived"
        )
        return

    state.archive_status["total_senders"] = len(senders)
    state.archive_status["message"] = "Starting archive..."
    archived_ids: list[str] = []

    try:
        service, error = get_gmail_service()
        if error:
            state.archive_status["error"] = error
            state.archive_status["done"] = True
            return

        total_archived = 0
        total_skipped = 0

        for i, sender in enumerate(senders):
            state.archive_status["current_sender"] = i + 1
            state.archive_status["message"] = f"Archiving emails from {sender}..."
            state.archive_status["progress"] = int((i / len(senders)) * 100)

            # Find all emails from this sender in INBOX
            query = protected.with_protection(
                f"from:{sender} in:inbox", protected_entries
            )
            message_ids = []
            page_token = None

            while True:
                result = (
                    service.users()
                    .messages()
                    .list(userId="me", q=query, maxResults=500, pageToken=page_token)
                    .execute()
                )

                messages = result.get("messages", [])
                message_ids.extend([m["id"] for m in messages])

                page_token = result.get("nextPageToken")
                if not page_token:
                    break

            message_ids, skipped = filter_protected_ids(
                service, message_ids, protected_entries
            )
            total_skipped += skipped
            if not message_ids:
                continue

            # Archive in batches (remove INBOX label)
            for j in range(0, len(message_ids), 100):
                batch_ids = message_ids[j : j + 100]
                service.users().messages().batchModify(
                    userId="me", body={"ids": batch_ids, "removeLabelIds": ["INBOX"]}
                ).execute()
                total_archived += len(batch_ids)
                archived_ids.extend(batch_ids)

                # Throttle every 500 emails (check at 100, 600, 1100, etc.)
                if (j + 100) % 500 == 0:
                    time.sleep(0.5)

        entry = history.record("archive", archived_ids, senders)
        state.archive_status["history_id"] = entry["id"] if entry else None
        state.archive_status["skipped_protected"] = total_skipped

        state.archive_status["progress"] = 100
        state.archive_status["done"] = True
        state.archive_status["archived_count"] = total_archived
        state.archive_status["message"] = (
            f"Archived {total_archived} emails from {len(senders)} senders"
        )

    except Exception as e:
        # Keep whatever was archived before the error undoable
        if archived_ids and not state.archive_status.get("history_id"):
            history.record("archive", archived_ids, senders, detail="partial (error)")
        state.archive_status["error"] = f"{e!s}"
        state.archive_status["done"] = True
        state.archive_status["message"] = f"Error: {e!s}"


def get_archive_status() -> dict:
    """Get archive operation status."""
    return state.archive_status.copy()
