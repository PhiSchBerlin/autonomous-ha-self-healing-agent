"""Persistenter Speicher für Security-Scan-Ergebnisse (JSON auf /data-Volume)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from models.security_models import SecurityIssue

logger = logging.getLogger(__name__)

_DATA_DIR = Path(os.environ.get("DATA_PATH", "/data"))
_ISSUES_FILE = _DATA_DIR / "security_issues.json"

# In-Memory-Cache — None bedeutet "noch nicht geladen"
_latest_issues: list["SecurityIssue"] | None = None


def _load_from_disk() -> list[dict[str, Any]]:
    try:
        if _ISSUES_FILE.exists():
            return json.loads(_ISSUES_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Security-Issues konnten nicht geladen werden: %s", exc)
    return []


def _save_to_disk(issues: list["SecurityIssue"]) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = [i.model_dump(mode="json") for i in issues]
        _ISSUES_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error("Security-Issues konnten nicht gespeichert werden: %s", exc)


def _ensure_loaded() -> None:
    """Lädt Daten von Disk beim ersten Zugriff (lazy initialization)."""
    global _latest_issues
    if _latest_issues is not None:
        return
    from models.security_models import SecurityIssue

    raw = _load_from_disk()
    restored: list[SecurityIssue] = []
    for item in raw:
        try:
            restored.append(SecurityIssue.model_validate(item))
        except Exception:
            pass
    _latest_issues = restored
    if restored:
        logger.info("Security-Store: %d Issues aus /data geladen.", len(restored))


def set_latest_issues(issues: list["SecurityIssue"]) -> None:
    global _latest_issues
    _latest_issues = list(issues)
    _save_to_disk(_latest_issues)


def get_latest_issues() -> list["SecurityIssue"]:
    _ensure_loaded()
    return list(_latest_issues or [])
