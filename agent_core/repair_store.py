"""Persistenter Speicher für RepairAction-Objekte (JSON auf /data-Volume)."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

_DATA_DIR = Path(os.environ.get("DATA_PATH", "/data"))
_STORE_FILE = _DATA_DIR / "repair_actions.json"

_actions: dict[str, dict[str, Any]] = {}


def _load_from_disk() -> dict[str, dict[str, Any]]:
    try:
        if _STORE_FILE.exists():
            raw = json.loads(_STORE_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return {item["id"]: item for item in raw if "id" in item}
            if isinstance(raw, dict):
                return raw
    except Exception as exc:
        logger.warning("Repair-Store konnte nicht geladen werden: %s", exc)
    return {}


def _save_to_disk() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = list(_actions.values())
        _STORE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error("Repair-Store konnte nicht gespeichert werden: %s", exc)


def add_action(action: dict[str, Any]) -> str:
    """Fügt eine neue RepairAction ein. Gibt die ID zurück."""
    if "id" not in action:
        action["id"] = str(uuid4())
    action.setdefault("created_at", datetime.now(UTC).isoformat())
    action.setdefault("updated_at", datetime.now(UTC).isoformat())
    action.setdefault("status", "proposed")
    _actions[action["id"]] = action
    _save_to_disk()
    return action["id"]


def update_action(action_id: str, updates: dict[str, Any]) -> bool:
    """Aktualisiert eine bestehende RepairAction."""
    if action_id not in _actions:
        return False
    _actions[action_id].update(updates)
    _actions[action_id]["updated_at"] = datetime.now(UTC).isoformat()
    _save_to_disk()
    return True


def get_action(action_id: str) -> dict[str, Any] | None:
    return _actions.get(action_id)


def get_all_actions() -> list[dict[str, Any]]:
    return sorted(_actions.values(), key=lambda a: a.get("created_at", ""), reverse=True)


def get_pending_actions() -> list[dict[str, Any]]:
    """Ausstehende Aktionen: proposed, simulated, validated."""
    pending_statuses = {"proposed", "simulated", "validated"}
    return [a for a in get_all_actions() if a.get("status") in pending_statuses]


def get_history_actions() -> list[dict[str, Any]]:
    """Abgeschlossene Aktionen: applied, approved, rejected, failed, rolled_back."""
    history_statuses = {"applied", "approved", "rejected", "failed", "rolled_back"}
    return [a for a in get_all_actions() if a.get("status") in history_statuses]


# Beim Import aus Datei laden
_actions = _load_from_disk()
logger.info("Repair-Store: %d Aktionen aus /data geladen.", len(_actions))
