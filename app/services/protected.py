"""
Protected Senders
-----------------
Senders on this list are never trashed, archived, marked read or
unsubscribed from, no matter what gets selected in the UI.

Entries are either a full address (boss@company.com) or a domain
(njit.edu). A domain entry also covers its subdomains (cs.njit.edu).
"""

from __future__ import annotations

import re
import threading

from app.services.storage import read_json, write_json

FILENAME = "protected_senders.json"
MAX_ENTRIES = 300

_lock = threading.Lock()

_EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$"
)


class ProtectedListError(Exception):
    """The protected list exists but can't be read. Actions must not run."""


def normalize_entry(raw: str) -> str | None:
    """Turn user input into a canonical entry, or None if it isn't valid.

    Accepts "user@x.com", "x.com", "@x.com", "*@x.com" and "*.x.com".
    """
    if not isinstance(raw, str):
        return None
    entry = raw.strip().lower()
    for prefix in ("*@", "@", "*."):
        if entry.startswith(prefix):
            entry = entry[len(prefix) :]
            break
    if _EMAIL_RE.match(entry) or _DOMAIN_RE.match(entry):
        return entry
    return None


def load() -> list[str]:
    """Return the protected list. Raises ProtectedListError if the file is broken."""
    try:
        data = read_json(FILENAME, {"senders": []})
    except ValueError as e:
        raise ProtectedListError(str(e)) from e
    raw = data.get("senders", []) if isinstance(data, dict) else []
    entries: list[str] = []
    for item in raw:
        entry = normalize_entry(item)
        if entry and entry not in entries:
            entries.append(entry)
    return entries


def add(raw: str) -> list[str]:
    """Add an entry. Returns the updated list. Raises ValueError on bad input."""
    entry = normalize_entry(raw)
    if not entry:
        raise ValueError("Enter an email address (name@example.com) or a domain (example.com)")
    with _lock:
        entries = load()
        if entry not in entries:
            if len(entries) >= MAX_ENTRIES:
                raise ValueError(f"The protected list is full ({MAX_ENTRIES} entries)")
            entries.append(entry)
            entries.sort()
            write_json(FILENAME, {"senders": entries})
        return entries


def remove(raw: str) -> list[str]:
    """Remove an entry. Returns the updated list."""
    entry = normalize_entry(raw) or (raw or "").strip().lower()
    with _lock:
        entries = [e for e in load() if e != entry]
        write_json(FILENAME, {"senders": entries})
        return entries


def _domain_of(sender: str) -> str:
    return sender.rsplit("@", 1)[-1] if "@" in sender else sender


def is_protected(sender: str, entries: list[str]) -> bool:
    """True if this sender (an address or a domain) is covered by the list."""
    if not sender:
        return False
    s = sender.strip().lower()
    domain = _domain_of(s)
    for entry in entries:
        if "@" in entry:
            if s == entry:
                return True
        elif domain == entry or domain.endswith("." + entry):
            return True
    return False


def exclusion_query(entries: list[str]) -> str:
    """Gmail search terms that leave protected senders out of a search.

    Entries are validated against strict patterns, so they can't inject
    other search operators.
    """
    return " ".join(f"-from:{e}" for e in entries)


def with_protection(query: str, entries: list[str]) -> str:
    """Append the exclusion terms to a Gmail query."""
    excl = exclusion_query(entries)
    if not excl:
        return query
    return f"{query} {excl}".strip()
