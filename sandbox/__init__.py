"""
Sandbox-Modul — isolierte Ausführungsumgebung für Reparatur-Validierungen.

Stellt SandboxManager bereit: erstellt temporäre Verzeichnisse mit den
vorgeschlagenen Änderungen und räumt sie danach zuverlässig auf.
Dateien werden nie ins Produktivsystem geschrieben.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Any

logger = logging.getLogger(__name__)


class SandboxManager:
    """
    Context-Manager für isolierte Datei-Sandbox.

    Erstellt ein temporäres Verzeichnis, schreibt vorgeschlagene Änderungen
    dorthin und räumt beim Verlassen automatisch auf.

    Verwendung:
        async with SandboxManager(repair_action) as sandbox:
            yaml_path = sandbox.path_for("/config/configuration.yaml")
            # Prüfungen auf yaml_path durchführen ...
    """

    def __init__(self, changes: list[dict[str, Any]]) -> None:
        """
        Args:
            changes: Liste von FileChange-Dicts mit file_path und proposed_content.
        """
        self._changes = changes
        self._tmpdir: str | None = None
        self._file_map: dict[str, str] = {}  # original_path → sandbox_path

    def initialize(self) -> "SandboxManager":
        """Initialisiert die Sandbox ohne Context-Manager-Protokoll.

        Verwende dies wenn die Sandbox Knotengrenzen eines LangGraph-Workflows
        überleben muss. Cleanup muss dann explizit via cleanup() erfolgen.
        """
        self._tmpdir = tempfile.mkdtemp(prefix="ha_agent_sandbox_")
        for change in self._changes:
            orig_path = change.get("file_path", "")
            proposed = change.get("proposed_content", "")
            original = change.get("original_content", "")
            if not orig_path:
                continue

            # Vollständigen Dateiinhalt aufbauen: Snippet in Originaldatei einsetzen.
            # Nur wenn original_content bekannt und in der echten Datei vorhanden ist,
            # wenden wir den Patch an — sonst schreiben wir proposed_content direkt
            # (Fallback für neue Dateien oder change_type=create).
            full_content = proposed
            real_path = Path(orig_path)
            if original and real_path.exists():
                try:
                    disk_content = real_path.read_text(encoding="utf-8")
                    if original in disk_content:
                        full_content = disk_content.replace(original, proposed, 1)
                    else:
                        logger.debug(
                            "Sandbox: original_snippet nicht in %s gefunden — schreibe proposed direkt",
                            orig_path,
                        )
                except OSError:
                    pass

            fname = Path(orig_path).name
            sandbox_path = Path(self._tmpdir) / fname
            sandbox_path.write_text(full_content, encoding="utf-8")
            self._file_map[orig_path] = str(sandbox_path)
        logger.debug("Sandbox erstellt: %s (%d Dateien)", self._tmpdir, len(self._file_map))
        return self

    def cleanup(self) -> None:
        """Räumt die Sandbox auf — Gegenstück zu initialize()."""
        self._cleanup()

    def __enter__(self) -> "SandboxManager":
        return self.initialize()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._cleanup()

    async def __aenter__(self) -> "SandboxManager":
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            logger.debug("Sandbox aufgeräumt: %s", self._tmpdir)
            self._tmpdir = None
            self._file_map = {}

    def path_for(self, original_path: str) -> str | None:
        """Gibt den Sandbox-Pfad für einen Original-Pfad zurück."""
        return self._file_map.get(original_path)

    @property
    def directory(self) -> str:
        """Sandbox-Verzeichnis (nur innerhalb des Context-Managers gültig)."""
        if not self._tmpdir:
            raise RuntimeError("SandboxManager außerhalb des Context-Managers verwendet")
        return self._tmpdir

    @property
    def files(self) -> dict[str, str]:
        """Mapping: original_path → sandbox_path."""
        return dict(self._file_map)

    @classmethod
    def from_repair_action(cls, repair_action: Any) -> "SandboxManager":
        """Erstellt SandboxManager aus einem RepairAction-Objekt oder -Dict."""
        if isinstance(repair_action, dict):
            changes = repair_action.get("changes", [])
            normalized = [
                {
                    "file_path": c.get("file_path", ""),
                    "original_content": c.get("original_content", c.get("original_snippet", "")),
                    "proposed_content": c.get("proposed_content", c.get("fixed_snippet", "")),
                }
                for c in changes
            ]
        else:
            normalized = [
                {
                    "file_path": c.file_path,
                    "original_content": c.original_content,
                    "proposed_content": c.proposed_content,
                }
                for c in getattr(repair_action, "changes", [])
            ]
        return cls(normalized)
