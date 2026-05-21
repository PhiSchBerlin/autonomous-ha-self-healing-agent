"""Unit-Tests für die GitOps-Engine."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from agent_core.tools.gitops import GitOpsEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_repo(tmp_path: Path) -> tuple[Path, GitOpsEngine]:
    """Erstellt ein temporäres Verzeichnis mit einigen YAML-Dateien für GitOps-Tests."""
    # Mindestens eine Datei anlegen, damit der Initial-Commit etwas zu committen hat
    config_file = tmp_path / "configuration.yaml"
    config_file.write_text("homeassistant:\n  name: Test Home\n")

    automation_file = tmp_path / "automations.yaml"
    automation_file.write_text("- id: '1'\n  alias: Test\n  trigger: []\n")

    engine = GitOpsEngine(str(tmp_path))
    return tmp_path, engine


# ---------------------------------------------------------------------------
# Initialisierung
# ---------------------------------------------------------------------------

class TestGitOpsInit:
    def test_creates_git_repo_if_missing(self, tmp_path: Path):
        engine = GitOpsEngine(str(tmp_path))
        (tmp_path / "config.yaml").write_text("key: value\n")
        repo = engine._get_repo()
        assert (tmp_path / ".git").exists()
        assert repo is not None

    def test_uses_existing_repo(self, tmp_repo: tuple[Path, GitOpsEngine]):
        repo_path, engine = tmp_repo
        # Zweiter Aufruf gibt dasselbe Repo zurück
        repo = engine._get_repo()
        assert repo is not None
        assert (repo_path / ".git").exists()

    def test_no_gitpython_raises_import_error(self, tmp_path: Path, monkeypatch):
        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "git":
                raise ImportError("gitpython not installed")
            return original_import(name, *args, **kwargs)

        engine = GitOpsEngine(str(tmp_path))
        engine._repo = None  # Cache leeren

        monkeypatch.setattr(builtins, "__import__", mock_import)
        with pytest.raises(ImportError, match="gitpython"):
            engine._get_repo()


# ---------------------------------------------------------------------------
# Commit-Message-Builder
# ---------------------------------------------------------------------------

class TestBuildCommitMessage:
    def test_minimal_message(self, tmp_repo: tuple[Path, GitOpsEngine]):
        _, engine = tmp_repo
        msg = engine._build_commit_message("Fix YAML error", None, None)
        assert msg.startswith("fix: Fix YAML error")

    def test_with_finding_title(self, tmp_repo: tuple[Path, GitOpsEngine]):
        _, engine = tmp_repo
        msg = engine._build_commit_message("Update config", None, "MQTT-Fehler")
        assert "Fixes finding: MQTT-Fehler" in msg

    def test_with_repair_action_id(self, tmp_repo: tuple[Path, GitOpsEngine]):
        _, engine = tmp_repo
        uid = uuid4()
        msg = engine._build_commit_message("Update config", uid, None)
        assert f"Repair-Action-ID: {uid}" in msg
        assert "Applied-by: HA Self-Healing Agent" in msg

    def test_with_all_fields(self, tmp_repo: tuple[Path, GitOpsEngine]):
        _, engine = tmp_repo
        uid = uuid4()
        msg = engine._build_commit_message("Fix sensor", uid, "Sensor broken")
        assert "fix: Fix sensor" in msg
        assert "Fixes finding: Sensor broken" in msg
        assert f"Repair-Action-ID: {uid}" in msg
        assert "Timestamp:" in msg


# ---------------------------------------------------------------------------
# Backup und Tags
# ---------------------------------------------------------------------------

class TestCreateBackup:
    def test_creates_backup_tag(self, tmp_repo: tuple[Path, GitOpsEngine]):
        repo_path, engine = tmp_repo
        uid = uuid4()
        tag_name = engine.create_backup(["configuration.yaml"], uid)
        assert tag_name.startswith("backup/repair-")
        assert tag_name != ""

    def test_tag_visible_in_get_tags(self, tmp_repo: tuple[Path, GitOpsEngine]):
        repo_path, engine = tmp_repo
        uid = uuid4()
        engine.create_backup(["configuration.yaml"], uid)
        tags = engine.get_tags()
        assert len(tags) >= 1
        assert all("name" in t and "commit" in t for t in tags)

    def test_backup_tag_name_contains_repair_id_prefix(self, tmp_repo: tuple[Path, GitOpsEngine]):
        _, engine = tmp_repo
        uid = uuid4()
        tag_name = engine.create_backup([], uid)
        short_id = str(uid)[:8]
        assert short_id in tag_name


# ---------------------------------------------------------------------------
# Commit-Changes
# ---------------------------------------------------------------------------

class TestCommitChanges:
    def test_commits_existing_file(self, tmp_repo: tuple[Path, GitOpsEngine]):
        repo_path, engine = tmp_repo
        config = repo_path / "configuration.yaml"
        config.write_text("homeassistant:\n  name: Updated\n")
        commit_hash = engine.commit_changes(
            changed_files=["configuration.yaml"],
            message="Update home name",
        )
        assert commit_hash != ""
        assert len(commit_hash) == 40  # Vollständiger SHA

    def test_returns_empty_for_nonexistent_files(self, tmp_repo: tuple[Path, GitOpsEngine]):
        _, engine = tmp_repo
        commit_hash = engine.commit_changes(
            changed_files=["does_not_exist.yaml"],
            message="Nonexistent",
        )
        assert commit_hash == ""

    def test_commit_appears_in_history(self, tmp_repo: tuple[Path, GitOpsEngine]):
        repo_path, engine = tmp_repo
        config = repo_path / "configuration.yaml"
        config.write_text("homeassistant:\n  name: HistoryTest\n")
        engine.commit_changes(["configuration.yaml"], "History-Test-Commit")
        history = engine.get_history(max_commits=5)
        messages = [h["message"] for h in history]
        assert any("History-Test-Commit" in m for m in messages)

    def test_commit_message_uses_conventional_commits(self, tmp_repo: tuple[Path, GitOpsEngine]):
        repo_path, engine = tmp_repo
        config = repo_path / "configuration.yaml"
        config.write_text("homeassistant:\n  name: ConventionalTest\n")
        uid = uuid4()
        engine.commit_changes(
            ["configuration.yaml"],
            "Fix broken sensor",
            repair_action_id=uid,
            finding_title="Sensor defekt",
        )
        history = engine.get_history(max_commits=3)
        assert any("fix: Fix broken sensor" in h["message"] for h in history)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

class TestGetHistory:
    def test_returns_list(self, tmp_repo: tuple[Path, GitOpsEngine]):
        _, engine = tmp_repo
        history = engine.get_history()
        assert isinstance(history, list)

    def test_history_entry_has_required_fields(self, tmp_repo: tuple[Path, GitOpsEngine]):
        _, engine = tmp_repo
        history = engine.get_history(max_commits=5)
        if history:
            entry = history[0]
            assert "hash" in entry
            assert "short_hash" in entry
            assert "message" in entry
            assert "author" in entry
            assert "timestamp" in entry

    def test_max_commits_respected(self, tmp_repo: tuple[Path, GitOpsEngine]):
        repo_path, engine = tmp_repo
        config = repo_path / "configuration.yaml"
        for i in range(5):
            config.write_text(f"homeassistant:\n  name: Version{i}\n")
            engine.commit_changes(["configuration.yaml"], f"Update {i}")
        history = engine.get_history(max_commits=3)
        assert len(history) <= 3


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

class TestRollback:
    def test_rollback_to_tag_creates_new_commit(self, tmp_repo: tuple[Path, GitOpsEngine]):
        repo_path, engine = tmp_repo
        uid = uuid4()

        # Backup erstellen (aktueller Zustand)
        tag_name = engine.create_backup(["configuration.yaml"], uid)

        # Änderung vornehmen
        config = repo_path / "configuration.yaml"
        config.write_text("homeassistant:\n  name: Changed After Backup\n")
        engine.commit_changes(["configuration.yaml"], "Change after backup")

        # Rollback
        success = engine.rollback_to_tag(tag_name)
        assert success is True

        # Neue Commits in History
        history = engine.get_history(max_commits=5)
        assert any("rollback" in h["message"].lower() for h in history)

    def test_rollback_to_nonexistent_tag_returns_false(self, tmp_repo: tuple[Path, GitOpsEngine]):
        _, engine = tmp_repo
        success = engine.rollback_to_tag("backup/nonexistent-tag-12345678-20990101-000000")
        assert success is False

    def test_rollback_to_commit_hash(self, tmp_repo: tuple[Path, GitOpsEngine]):
        repo_path, engine = tmp_repo
        config = repo_path / "configuration.yaml"

        # Ursprünglichen Stand ermitteln
        history_before = engine.get_history(max_commits=1)
        original_hash = history_before[0]["hash"] if history_before else None

        if original_hash:
            # Änderung vornehmen
            config.write_text("homeassistant:\n  name: Changed\n")
            engine.commit_changes(["configuration.yaml"], "Change")

            # Rollback auf ursprünglichen Commit
            success = engine.rollback_to_commit(original_hash)
            assert success is True

    def test_rollback_to_invalid_commit_returns_false(self, tmp_repo: tuple[Path, GitOpsEngine]):
        _, engine = tmp_repo
        success = engine.rollback_to_commit("0000000000000000000000000000000000000000")
        assert success is False
