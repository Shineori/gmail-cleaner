"""
JSON Storage Helpers
--------------------
Small atomic read/write helpers for files in the data directory.
"""

import json
import os
import tempfile

from app.core import settings


def data_path(filename: str) -> str:
    """Absolute path of a file inside the data directory."""
    return os.path.join(settings.data_dir, filename)


def read_json(filename: str, default):
    """Read a JSON file from the data dir.

    Returns ``default`` when the file doesn't exist. Raises ValueError when the
    file exists but can't be parsed, so callers can fail closed instead of
    silently treating a corrupted file as empty.
    """
    path = data_path(filename)
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"Could not read {filename}: {e}") from e


def write_json(filename: str, data) -> None:
    """Atomically write JSON to the data dir (temp file + rename)."""
    os.makedirs(settings.data_dir, exist_ok=True)
    path = data_path(filename)
    fd, tmp = tempfile.mkstemp(dir=settings.data_dir, prefix=f".{filename}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
