"""Persistenter Speicher für Full-Audit-Ergebnisse (JSON auf /data-Volume)."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_DIR = Path(os.environ.get("DATA_PATH", "/data"))
_AUDIT_FILE = _DATA_DIR / "audit_results.json"

# None bedeutet "noch nicht geladen"
_latest_audit: dict[str, Any] | None = None
_loaded: bool = False


def _load_from_disk() -> dict[str, Any] | None:
    try:
        if _AUDIT_FILE.exists():
            return json.loads(_AUDIT_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Audit-Ergebnisse konnten nicht geladen werden: %s", exc)
    return None


def _save_to_disk(result: dict[str, Any]) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _AUDIT_FILE.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error("Audit-Ergebnisse konnten nicht gespeichert werden: %s", exc)


def _ensure_loaded() -> None:
    """Lädt Daten von Disk beim ersten Zugriff (lazy initialization)."""
    global _latest_audit, _loaded
    if _loaded:
        return
    _loaded = True
    _latest_audit = _load_from_disk()
    if _latest_audit:
        logger.info(
            "Audit-Store: letztes Ergebnis von %s geladen.",
            _latest_audit.get("saved_at", "?"),
        )


def set_latest_audit(result: dict[str, Any]) -> None:
    global _latest_audit, _loaded
    result["saved_at"] = datetime.now(UTC).isoformat()
    _latest_audit = result
    _loaded = True
    _save_to_disk(result)


def get_latest_audit() -> dict[str, Any] | None:
    _ensure_loaded()
    return _latest_audit
