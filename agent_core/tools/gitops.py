"""
GitOps-Engine für automatische Versionierung aller HA-Konfigurationsänderungen.

Jede Änderung wird:
- automatisch committed (mit semantischer Commit-Message)
- diffbar gemacht
- rollbackfähig gespeichert
- auditierbar geloggt

Verwendet GitPython. Falls kein Git-Repo vorhanden, wird eines initialisiert.
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


class GitOpsEngine:
    """
    Verwaltet alle Git-Operationen für HA-Konfigurationsänderungen.

    Garantien:
    - Backup vor jeder Änderung
    - Atomic Commit nach erfolgreicher Anwendung
    - Rollback auf beliebigen früheren Commit
    - Vollständiges Audit-Log über Git-History
    """

    COMMIT_AUTHOR_NAME = "HA Self-Healing Agent"
    COMMIT_AUTHOR_EMAIL = "ha-agent@localhost"

    def __init__(self, repo_path: str) -> None:
        self.repo_path = Path(repo_path)
        self._repo: Any = None

    def _get_repo(self) -> Any:
        """Gibt das Git-Repo zurück, initialisiert es falls nötig."""
        if self._repo is not None:
            return self._repo

        try:
            import git
        except ImportError as exc:
            raise ImportError(
                "gitpython nicht installiert. Installation: pip install gitpython"
            ) from exc

        # Git's "dubious ownership"-Sicherheitscheck umgehen: Im HA-Container
        # läuft der Prozess oft als anderer User als der Owner von /config.
        self._configure_safe_directory()

        if not (self.repo_path / ".git").exists():
            logger.info("Initialisiere Git-Repo in %s", self.repo_path)
            self._repo = git.Repo.init(str(self.repo_path))
            try:
                files = self._get_all_tracked_files()
                if files:
                    self._repo.index.add(files)
                else:
                    # Leeres Repo braucht mindestens eine Datei für den ersten Commit
                    placeholder = self.repo_path / ".ha-agent-tracking"
                    placeholder.write_text("HA Self-Healing Agent tracking\n")
                    self._repo.index.add([".ha-agent-tracking"])
                self._initial_commit()
            except Exception as exc:
                logger.warning("Initialer Git-Commit fehlgeschlagen: %s", exc)
        else:
            self._repo = git.Repo(str(self.repo_path))
            # Sicherstellen, dass HEAD existiert (Repo könnte leer sein)
            if not self._repo.head.is_valid():
                try:
                    files = self._get_all_tracked_files()
                    if files:
                        self._repo.index.add(files)
                    else:
                        placeholder = self.repo_path / ".ha-agent-tracking"
                        placeholder.write_text("HA Self-Healing Agent tracking\n")
                        self._repo.index.add([".ha-agent-tracking"])
                    self._initial_commit()
                except Exception as exc:
                    logger.warning("Initialer Git-Commit für bestehendes Repo fehlgeschlagen: %s", exc)

        return self._repo

    def _configure_safe_directory(self) -> None:
        """Trägt repo_path in git safe.directory ein, damit Git auch bei
        abweichendem Datei-Owner im Container nicht mit 'dubious ownership'
        abbricht."""
        import subprocess
        try:
            subprocess.run(
                ["git", "config", "--global", "--add", "safe.directory", str(self.repo_path)],
                check=True,
                capture_output=True,
            )
            logger.debug("safe.directory gesetzt: %s", self.repo_path)
        except Exception as exc:
            logger.debug("safe.directory konnte nicht gesetzt werden: %s", exc)

    def _get_all_tracked_files(self) -> list[str]:
        """Gibt alle relevanten Dateien für Git-Tracking zurück."""
        tracked_extensions = {".yaml", ".yml", ".json", ".py", ".toml"}
        files = []
        for f in self.repo_path.rglob("*"):
            if f.is_file() and f.suffix in tracked_extensions and f.exists():
                rel = str(f.relative_to(self.repo_path))
                if not any(part.startswith(".") for part in f.parts):
                    files.append(rel)
        return files

    def _initial_commit(self) -> None:
        """Erstellt den initialen Commit falls nötig."""
        import git

        repo = self._repo
        if not repo.head.is_valid():
            repo.index.commit(
                "chore: initial snapshot by HA Self-Healing Agent",
                author=git.Actor(self.COMMIT_AUTHOR_NAME, self.COMMIT_AUTHOR_EMAIL),
                committer=git.Actor(self.COMMIT_AUTHOR_NAME, self.COMMIT_AUTHOR_EMAIL),
            )

    def create_backup(self, file_paths: list[str], repair_action_id: UUID) -> str:
        """
        Erstellt einen Git-Tag als Backup-Snapshot vor einer Reparatur.

        Gibt den Tag-Namen zurück (verwendbar für Rollback).
        """
        repo = self._get_repo()
        tag_name = f"backup/repair-{str(repair_action_id)[:8]}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"

        try:
            repo.create_tag(tag_name, message=f"Backup vor RepairAction {repair_action_id}")
            logger.info("Backup-Tag erstellt: %s", tag_name)
            return tag_name
        except Exception as exc:
            logger.warning("Backup-Tag konnte nicht erstellt werden: %s", exc)
            return ""

    def commit_changes(
        self,
        changed_files: list[str],
        message: str,
        repair_action_id: UUID | None = None,
        finding_title: str | None = None,
    ) -> str:
        """
        Staged und committet geänderte Dateien mit semantischer Commit-Message.

        Gibt den Commit-Hash zurück.
        """
        import git

        repo = self._get_repo()

        # Nur existierende Dateien stagen
        existing = [f for f in changed_files if (self.repo_path / f).exists()]
        if not existing:
            logger.warning("Keine existierenden Dateien zum Committen")
            return ""

        repo.index.add(existing)

        # Semantische Commit-Message
        full_message = self._build_commit_message(message, repair_action_id, finding_title)

        commit = repo.index.commit(
            full_message,
            author=git.Actor(self.COMMIT_AUTHOR_NAME, self.COMMIT_AUTHOR_EMAIL),
            committer=git.Actor(self.COMMIT_AUTHOR_NAME, self.COMMIT_AUTHOR_EMAIL),
        )

        logger.info("Commit erstellt: %s — %s", commit.hexsha[:8], message)
        return commit.hexsha

    def get_diff(self, file_path: str, commit_hash: str | None = None) -> str:
        """
        Gibt den Git-Diff für eine Datei zurück.

        commit_hash=None → Diff gegen HEAD (nicht-committete Änderungen).
        """
        repo = self._get_repo()
        rel_path = str(Path(file_path).relative_to(self.repo_path))

        try:
            if commit_hash:
                return repo.git.diff(commit_hash, "--", rel_path)
            return repo.git.diff("HEAD", "--", rel_path)
        except Exception as exc:
            logger.warning("Diff-Fehler für %s: %s", file_path, exc)
            return ""

    def rollback_to_tag(self, tag_name: str) -> bool:
        """
        Setzt den Working Tree auf den Stand eines Backup-Tags zurück.

        Erstellt einen neuen Commit (kein force-push, kein History-Rewrite).
        """
        import git

        repo = self._get_repo()
        try:
            # Tag-Commit ermitteln
            tag_ref = repo.tags[tag_name]
            target_commit = tag_ref.commit

            # Nur Dateien zurücksetzen die im Tag-Commit tatsächlich existieren —
            # kein "." checkout, der auch gelöschte/umbenannte Dateien anfassen würde.
            restored: list[str] = []
            for blob in target_commit.tree.traverse():
                if blob.type != "blob":
                    continue
                rel = blob.path
                try:
                    repo.git.checkout(str(target_commit.hexsha), "--", rel)
                    restored.append(rel)
                except Exception as file_exc:
                    logger.warning("Rollback: Datei '%s' konnte nicht wiederhergestellt werden: %s", rel, file_exc)

            if restored:
                repo.index.add(restored)
            else:
                logger.warning("Rollback: Keine Dateien wiederhergestellt für Tag '%s'", tag_name)
                return False

            rollback_msg = (
                f"revert: rollback to backup '{tag_name}'\n\n"
                f"Rolled back to commit {target_commit.hexsha[:8]} "
                f"({target_commit.committed_datetime.isoformat()})"
            )
            repo.index.commit(
                rollback_msg,
                author=git.Actor(self.COMMIT_AUTHOR_NAME, self.COMMIT_AUTHOR_EMAIL),
                committer=git.Actor(self.COMMIT_AUTHOR_NAME, self.COMMIT_AUTHOR_EMAIL),
            )
            logger.info("Rollback auf Tag '%s' erfolgreich (%d Dateien)", tag_name, len(restored))
            return True
        except Exception as exc:
            logger.error("Rollback auf '%s' fehlgeschlagen: %s", tag_name, exc)
            return False

    def rollback_to_commit(self, commit_hash: str) -> bool:
        """Rollback auf einen spezifischen Commit-Hash."""
        import git

        repo = self._get_repo()
        try:
            target_commit = repo.commit(commit_hash)
            restored: list[str] = []
            for blob in target_commit.tree.traverse():
                if blob.type != "blob":
                    continue
                try:
                    repo.git.checkout(commit_hash, "--", blob.path)
                    restored.append(blob.path)
                except Exception as file_exc:
                    logger.warning("Rollback: Datei '%s' übersprungen: %s", blob.path, file_exc)
            if restored:
                repo.index.add(restored)
            repo.index.commit(
                f"revert: rollback to commit {commit_hash[:8]}",
                author=git.Actor(self.COMMIT_AUTHOR_NAME, self.COMMIT_AUTHOR_EMAIL),
                committer=git.Actor(self.COMMIT_AUTHOR_NAME, self.COMMIT_AUTHOR_EMAIL),
            )
            return True
        except Exception as exc:
            logger.error("Rollback auf Commit '%s' fehlgeschlagen: %s", commit_hash, exc)
            return False

    def get_history(self, max_commits: int = 20) -> list[dict[str, Any]]:
        """Gibt die letzten Commits als strukturierte Liste zurück."""
        repo = self._get_repo()
        history = []
        try:
            if not repo.head.is_valid():
                return history
            for commit in list(repo.iter_commits(repo.head.commit, max_count=max_commits)):
                history.append({
                    "hash": commit.hexsha,
                    "short_hash": commit.hexsha[:8],
                    "message": commit.message.strip(),
                    "author": str(commit.author),
                    "timestamp": commit.committed_datetime.isoformat(),
                    "files_changed": list(commit.stats.files.keys()),
                })
        except Exception as exc:
            logger.warning("History-Abruf fehlgeschlagen: %s", exc)
        return history

    def get_tags(self) -> list[dict[str, str]]:
        """Gibt alle Backup-Tags zurück."""
        repo = self._get_repo()
        try:
            return [
                {
                    "name": tag.name,
                    "commit": tag.commit.hexsha[:8],
                    "timestamp": tag.commit.committed_datetime.isoformat(),
                }
                for tag in repo.tags
                if tag.name.startswith("backup/")
            ]
        except Exception:
            return []

    def _build_commit_message(
        self,
        message: str,
        repair_action_id: UUID | None,
        finding_title: str | None,
    ) -> str:
        """Baut eine semantische Commit-Message im Conventional-Commits-Format."""
        lines = [f"fix: {message}"]
        if finding_title or repair_action_id:
            lines.append("")
            if finding_title:
                lines.append(f"Fixes finding: {finding_title}")
            if repair_action_id:
                lines.append(f"Repair-Action-ID: {repair_action_id}")
            lines.append("Applied-by: HA Self-Healing Agent")
            lines.append(f"Timestamp: {datetime.now(UTC).isoformat()}")
        return "\n".join(lines)
