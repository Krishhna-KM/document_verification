"""Profiles for known kinds of document, backed by backend/document_types.json.

The API has no "document type" parameter. A profile is picked automatically from the keys the caller sends
(see resolver.py) and only supplies prompt instructions, field meanings / aliases and post-processing.
Adding a new kind = adding an entry to that file; it is reloaded automatically when the file changes.
"""
import json
import re
import threading
from typing import Dict, Optional

from backend.config import settings

FIELD_TYPES = {"name", "text", "id", "number", "percentage", "date", "table"}
NUMERIC_OPTIONS = ("tolerance", "threshold", "decay", "weight")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")


class ConfigError(ValueError):
    pass


def validate_config(key: str, cfg: dict) -> dict:
    if not KEY_RE.match(key or ""):
        raise ConfigError("Document type key must be lowercase letters/digits/underscore, 2-49 chars, starting with a letter.")
    if not isinstance(cfg, dict) or not isinstance(cfg.get("fields"), dict) or not cfg["fields"]:
        raise ConfigError("Config needs a non-empty 'fields' object.")
    for fkey, spec in cfg["fields"].items():
        if not KEY_RE.match(fkey):
            raise ConfigError(f"Invalid field key '{fkey}'.")
        if not isinstance(spec, dict) or spec.get("type") not in FIELD_TYPES:
            raise ConfigError(f"Field '{fkey}' needs a 'type' from {sorted(FIELD_TYPES)}.")
        if not isinstance(spec.get("aliases", []), list):
            raise ConfigError(f"Field '{fkey}': 'aliases' must be a list.")
        for opt in NUMERIC_OPTIONS:
            if opt in spec and (isinstance(spec[opt], bool) or not isinstance(spec[opt], (int, float)) or spec[opt] < 0):
                raise ConfigError(f"Field '{fkey}': '{opt}' must be a non-negative number.")
        if not isinstance(spec.get("needs", []), list):
            raise ConfigError(f"Field '{fkey}': 'needs' must be a list.")
    cfg.setdefault("label", key)
    cfg.setdefault("description", "")
    return cfg


class Registry:
    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self._types: Dict[str, dict] = {}
        self._mtime = 0.0
        self.reload()

    def reload(self):
        with self._lock:
            mtime = self._path.stat().st_mtime
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for key, cfg in data.items():
                validate_config(key, cfg)
            self._types, self._mtime = data, mtime

    def _refresh_if_changed(self):
        """Pick up hand-edits of document_types.json without restarting the server."""
        try:
            if self._path.stat().st_mtime != self._mtime:
                self.reload()
        except (OSError, ValueError):  # file mid-write or invalid JSON: keep serving the last good config
            pass

    def get(self, key: str) -> Optional[dict]:
        self._refresh_if_changed()
        return self._types.get((key or "").strip().lower())

    def all(self) -> Dict[str, dict]:
        self._refresh_if_changed()
        return self._types

registry = Registry(settings.DOCUMENT_TYPES_FILE)
