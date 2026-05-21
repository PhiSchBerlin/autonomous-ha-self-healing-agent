"""Unit-Tests für die Analyse-Tools."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from agent_core.tools.analysis_tools import (
    grep_pattern,
    jinja2_syntax_check,
    list_ha_files,
    python_syntax_check,
    read_file,
    yamllint_check,
)


@pytest.mark.asyncio
class TestPythonSyntaxCheck:
    async def test_valid_python(self, tmp_path: Path) -> None:
        f = tmp_path / "valid.py"
        f.write_text("def hello():\n    return 42\n")
        result = await python_syntax_check(str(f))
        assert result["success"] is True
        assert result["issue_count"] == 0

    async def test_invalid_python(self, tmp_path: Path) -> None:
        f = tmp_path / "broken.py"
        f.write_text("def hello(\n    return 42\n")
        result = await python_syntax_check(str(f))
        assert result["success"] is True
        assert result["issue_count"] == 1
        assert result["issues"][0]["level"] == "error"

    async def test_missing_file(self) -> None:
        result = await python_syntax_check("/nonexistent/path/file.py")
        assert result["success"] is False
        assert "nicht gefunden" in result["error"]


@pytest.mark.asyncio
class TestReadFile:
    async def test_reads_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.yaml"
        f.write_text("key: value\n")
        result = await read_file(str(f))
        assert result["success"] is True
        assert "key: value" in result["content"]

    async def test_missing_file_returns_error(self) -> None:
        result = await read_file("/nonexistent/file.txt")
        assert result["success"] is False

    async def test_truncates_at_max_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"line {i}" for i in range(200)))
        result = await read_file(str(f), max_lines=50)
        assert result["success"] is True
        assert result["truncated"] is True
        assert result["content"].count("\n") <= 50

    async def test_rejects_forbidden_path(self) -> None:
        result = await read_file("/etc/shadow")
        assert result["success"] is False
        # Auf macOS existiert /etc/shadow nicht → "nicht gefunden"
        # Auf Linux existiert es → "Zugriff verweigert"
        assert result["error"] is not None


@pytest.mark.asyncio
class TestJinja2SyntaxCheck:
    async def test_valid_template(self) -> None:
        result = await jinja2_syntax_check("{{ states('sensor.temp') | float }}")
        assert result["valid"] is True

    async def test_invalid_template(self) -> None:
        result = await jinja2_syntax_check("{{ states('sensor.temp' | float }}")
        assert result["valid"] is False
        assert result["error"] is not None

    async def test_plain_text_is_valid(self) -> None:
        result = await jinja2_syntax_check("just plain text")
        assert result["valid"] is True

    async def test_complex_valid_template(self) -> None:
        template = (
            "{% if states('sensor.temp') | float > 25 %}"
            "hot{% else %}cold{% endif %}"
        )
        result = await jinja2_syntax_check(template)
        assert result["valid"] is True


@pytest.mark.asyncio
class TestGrepPattern:
    async def test_finds_pattern(self, tmp_path: Path) -> None:
        (tmp_path / "test.yaml").write_text("platform: mqtt\nkey: value\n")
        result = await grep_pattern(
            pattern="platform: mqtt",
            directory=str(tmp_path),
            file_glob="**/*.yaml",
        )
        assert result["success"] is True
        assert result["match_count"] >= 1

    async def test_no_match_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "test.yaml").write_text("key: value\n")
        result = await grep_pattern(
            pattern="nonexistent_pattern_xyz_123",
            directory=str(tmp_path),
        )
        assert result["success"] is True
        assert result["match_count"] == 0

    async def test_respects_max_results(self, tmp_path: Path) -> None:
        # Eine einzelne Datei mit vielen Treffern — max_results gilt pro Datei bei ripgrep,
        # aber der Python-Fallback begrenzt global. Wir testen mit einer Datei.
        content = "platform: mqtt\n" * 30
        (tmp_path / "big.yaml").write_text(content)
        result = await grep_pattern(
            pattern="platform: mqtt",
            directory=str(tmp_path),
            max_results=5,
        )
        assert result["success"] is True
        assert result["match_count"] <= 5


@pytest.mark.asyncio
class TestListHAFiles:
    async def test_discovers_yaml_files(self, tmp_path: Path) -> None:
        (tmp_path / "configuration.yaml").write_text("homeassistant:\n")
        (tmp_path / "automations.yaml").write_text("- id: '1'\n")
        result = await list_ha_files(str(tmp_path))
        assert result["success"] is True
        assert result["total_files"] >= 2

    async def test_missing_path_returns_error(self) -> None:
        result = await list_ha_files("/nonexistent/config/path")
        assert result["success"] is False
