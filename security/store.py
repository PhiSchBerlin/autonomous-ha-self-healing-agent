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

# In-Memory-Cache für schnellen Zugriff
_latest_issues: list["SecurityIssue"] = []


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


def _restore_from_disk() -> None:
    """Lädt persistierte Issues beim Start in den In-Memory-Cache."""
    global _latest_issues
    from models.security_models import SecurityIssue

    raw = _load_from_disk()
    if not raw:
        return
    restored: list[SecurityIssue] = []
    for item in raw:
        try:
            restored.append(SecurityIssue.model_validate(item))
        except Exception:
            pass
    _latest_issues = restored
    logger.info("Security-Store: %d Issues aus /data geladen.", len(_latest_issues))


def set_latest_issues(issues: list["SecurityIssue"]) -> None:
    global _latest_issues
    _latest_issues = list(issues)
    _save_to_disk(_latest_issues)


async def get_latest_issues() -> list["SecurityIssue"]:
    return list(_latest_issues)


# Beim Import sofort aus Datei laden (läuft beim ersten Import durch den Router)
_restore_from_disk()
