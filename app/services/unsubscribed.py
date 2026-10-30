"""
Unsubscribe Tracker
-------------------
Remembers who I unsubscribed from and when, then checks Gmail for mail that
arrived afterwards, so I can tell whether a sender actually stopped.

    waiting        still inside the grace period (senders get up to
                   10 business days by law, ~14 calendar days)
    worked         grace period over and nothing new arrived after it
    still_sending  mail arrived after the grace period ended

Stored in data/unsubscribed.json as {"senders": {sender: {...}}}.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from app.services.storage import read_json, write_json

logger = logging.getLogger(__name__)

FILENAME = "unsubscribed.json"
GRACE_DAYS = 14
MAX_ENTRIES = 500
METHODS = ("one-click", "link", "manual")

_lock = threading.Lock()

_SENDER_RE = re.compile(
    r"^([a-z0-9._%+-]+@)?([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_sender(raw: str) -> str | None:
    """A domain (m1.panerabread.com) or an address. None if invalid."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower().lstrip("@")
    return s if _SENDER_RE.match(s) else None


def _load() -> dict:
    try:
        data = read_json(FILENAME, {"senders": {}})
    except ValueError:
        logger.exception("unsubscribed.json is unreadable; starting fresh")
        return {}
    senders = data.get("senders", {}) if isinstance(data, dict) else {}
    return {k: v for k, v in senders.items() if isinstance(v, dict) and normalize_sender(k)}


def load() -> dict:
    with _lock:
        return _load()


def record(sender: str, method: str = "one-click", when: datetime | None = None) -> dict | None:
    """Remember an unsubscribe. Never raises (must not break the unsubscribe)."""
    try:
        key = normalize_sender(sender)
        if not key:
            return None
        method = method if method in METHODS else "manual"
        ts = (when or _now()).astimezone(timezone.utc).isoformat(timespec="seconds")
        with _lock:
            senders = _load()
            if key not in senders and len(senders) >= MAX_ENTRIES:
                oldest = min(senders, key=lambda k: senders[k].get("unsubscribed_at", ""))
                senders.pop(oldest, None)
            senders[key] = {
                "unsubscribed_at": ts,
                "method": method,
                "last_check": senders.get(key, {}).get("last_check"),
            }
            write_json(FILENAME, {"senders": senders})
            return {"sender": key, **senders[key]}
    except Exception:
        logger.exception("Failed to record unsubscribe for %s", sender)
        return None


def forget(sender: str) -> bool:
    key = normalize_sender(sender) or (sender or "").strip().lower()
    with _lock:
        senders = _load()
        existed = senders.pop(key, None) is not None
        write_json(FILENAME, {"senders": senders})
        return existed


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def _count(service, query: str, cap: int = 100) -> int:
    """Number of messages matching a query (exact up to ``cap``)."""
    total = 0
    page_token = None
    while total < cap:
        res = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=min(100, cap - total), pageToken=page_token)
            .execute()
        )
        total += len(res.get("messages", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return total


def status_for(entry: dict, since: int | None, late: int | None, now: datetime | None = None) -> dict:
    """Work out the status for one tracked sender from its Gmail counts."""
    now = now or _now()
    unsub_at = _parse(entry.get("unsubscribed_at")) or now
    grace_end = unsub_at + timedelta(days=GRACE_DAYS)
    day = max(1, (now - unsub_at).days + 1)

    if late:
        status = "still_sending"
    elif now >= grace_end:
        status = "worked"
    else:
        status = "waiting"
    return {
        "status": status,
        "since_count": since,
        "late_count": late,
        "day": min(day, GRACE_DAYS) if status == "waiting" else day,
        "grace_days": GRACE_DAYS,
        "grace_ends": grace_end.isoformat(timespec="seconds"),
    }


def check_all(service, now: datetime | None = None) -> list[dict]:
    """Check Gmail for every tracked sender. Newest unsubscribes first."""
    now = now or _now()
    with _lock:
        senders = _load()

    results = []
    for sender, entry in senders.items():
        unsub_at = _parse(entry.get("unsubscribed_at")) or now
        grace_end = unsub_at + timedelta(days=GRACE_DAYS)
        base = f"from:{sender} -in:trash -in:spam"
        try:
            since = _count(service, f"{base} after:{int(unsub_at.timestamp())}")
            # Mail inside the grace period is normal; only count what came after it
            late = (
                _count(service, f"{base} after:{int(grace_end.timestamp())}")
                if now >= grace_end and since
                else 0
            )
            info = status_for(entry, since, late, now)
            error = None
        except Exception as e:  # noqa: BLE001
            logger.warning("Unsubscribe check failed for %s: %s", sender, e)
            info = status_for(entry, None, None, now)
            info["status"] = "unknown"
            error = str(e)[:200]
        results.append(
            {
                "sender": sender,
                "unsubscribed_at": entry.get("unsubscribed_at"),
                "method": entry.get("method", "manual"),
                "error": error,
                **info,
            }
        )

    with _lock:
        stored = _load()
        stamp = now.isoformat(timespec="seconds")
        for r in results:
            if r["sender"] in stored:
                stored[r["sender"]]["last_check"] = stamp
        write_json(FILENAME, {"senders": stored})

    results.sort(key=lambda r: r.get("unsubscribed_at") or "", reverse=True)
    return results


def annotate_rows(rows: list, key: str) -> list:
    """Add unsubscribed_at to scan rows for senders we've unsubscribed from."""
    senders = load()
    if not senders:
        return rows
    out = []
    for r in rows:
        if isinstance(r, dict):
            s = str(r.get(key, "")).lower()
            entry = senders.get(s)
            if entry:
                r = {**r, "unsubscribed_at": entry.get("unsubscribed_at"),
                     "unsubscribe_method": entry.get("method")}
        out.append(r)
    return out
