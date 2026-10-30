"""
Gmail Safety Helpers
--------------------
Shared helpers for the safety features: listing message IDs for a query,
double-checking that no protected sender slipped through, and fetching a
few sample messages for previews.
"""

from __future__ import annotations

import logging

from app.services.gmail.helpers import get_sender_info, get_subject
from app.services.protected import is_protected

logger = logging.getLogger(__name__)

BATCH_SIZE = 100


def list_message_ids(service, query: str, limit: int | None = None) -> list[str]:
    """Return message IDs matching a Gmail query (up to ``limit`` if given)."""
    ids: list[str] = []
    page_token = None
    while True:
        page_size = 500 if limit is None else max(1, min(500, limit - len(ids)))
        result = (
            service.users()
            .messages()
            .list(userId="me", q=query or None, maxResults=page_size, pageToken=page_token)
            .execute()
        )
        ids.extend(m["id"] for m in result.get("messages", []))
        if limit is not None and len(ids) >= limit:
            return ids[:limit]
        page_token = result.get("nextPageToken")
        if not page_token:
            return ids


def _fetch_from_headers(service, ids: list[str]) -> dict[str, str | None]:
    """Map message id -> sender address. None means the lookup failed."""
    senders: dict[str, str | None] = {}

    def callback(request_id, response, exception):
        if exception or not response:
            return
        headers = response.get("payload", {}).get("headers", [])
        _, email = get_sender_info(headers)
        senders[response.get("id", request_id)] = email

    for i in range(0, len(ids), BATCH_SIZE):
        chunk = ids[i : i + BATCH_SIZE]
        batch = service.new_batch_http_request(callback=callback)
        for msg_id in chunk:
            batch.add(
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="metadata", metadataHeaders=["From"]),
                request_id=msg_id,
            )
        batch.execute()

    return {msg_id: senders.get(msg_id) for msg_id in ids}


def filter_protected_ids(
    service, ids: list[str], entries: list[str]
) -> tuple[list[str], int]:
    """Second safety net after the search-level exclusion.

    Looks up the real From address of every message and drops anything from a
    protected sender. Messages whose sender can't be checked are dropped too
    (fail closed). Returns (ids_to_act_on, number_skipped).

    With an empty protected list this returns the IDs untouched and makes no
    API calls.
    """
    if not entries or not ids:
        return list(ids), 0

    senders = _fetch_from_headers(service, ids)
    keep = [
        msg_id
        for msg_id in ids
        if senders.get(msg_id) and not is_protected(senders[msg_id], entries)
    ]
    skipped = len(ids) - len(keep)
    if skipped:
        logger.info("Safety check skipped %d protected/unverifiable messages", skipped)
    return keep, skipped


def fetch_samples(service, ids: list[str], limit: int = 5) -> list[dict]:
    """Sender, subject and date for the first few messages (for previews)."""
    samples: dict[str, dict] = {}
    chosen = ids[:limit]
    if not chosen:
        return []

    def callback(request_id, response, exception):
        if exception or not response:
            return
        headers = response.get("payload", {}).get("headers", [])
        name, email = get_sender_info(headers)
        date = next(
            (h["value"] for h in headers if h.get("name", "").lower() == "date"), ""
        )
        samples[response.get("id", request_id)] = {
            "from": email,
            "name": name,
            "subject": get_subject(headers),
            "date": date,
        }

    batch = service.new_batch_http_request(callback=callback)
    for msg_id in chosen:
        batch.add(
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ),
            request_id=msg_id,
        )
    batch.execute()
    return [samples[m] for m in chosen if m in samples]
