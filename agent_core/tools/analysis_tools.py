"""
Toolformer-Tools für die Agenten: yamllint, Python-Syntax, HA-Config-Check,
File-Reader, Pattern-Grep — alle als async Funktionen mit strukturiertem Output.

Diese Tools werden als LangGraph-Tool-Calls im Agenten-Workflow eingesetzt.
"""

from __future__ import annotations

import ast
import asyncio
import re
import subprocess
from pathlib import Path
from typing import Any


async def _run_subprocess(
    cmd: list[str],
    cwd: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Führt einen Subprocess aus und gibt stdout/stderr/returncode zurück."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "returncode": proc.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "success": proc.returncode == 0,
        }
    except asyncio.TimeoutError:
        return {"returncode": -1, "stdout": "", "stderr": "Timeout", "success": False}
    except FileNotFoundError as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc), "success": False}


async def yamllint_check(file_path: str) -> dict[str, Any]:
    """
    Führt yamllint auf einer YAML-Datei aus.

    Gibt strukturierte Fehlerliste zurück: [{line, col, level, message}]
    """
    result = await _run_subprocess(["yamllint", "-f", "parsable", file_path])
    issues: list[dict[str, Any]] = []

    if not result["success"] and result["returncode"] != 1:
        # Nicht installiert oder anderer Fehler
        return {"success": False, "error": result["stderr"], "issues": []}

    # Parsable-Format: "file:line:col: [level] message"
    pattern = re.compile(r":(\d+):(\d+):\s+\[(warning|error)\]\s+(.+)$")
    for line in result["stdout"].splitlines():
        m = pattern.search(line)
        if m:
            issues.append({
                "line": int(m.group(1)),
                "col": int(m.group(2)),
                "level": m.group(3),
                "message": m.group(4).strip(),
            })

    return {"success": True, "issues": issues, "issue_count": len(issues)}


async def python_syntax_check(file_path: str) -> dict[str, Any]:
    """
    Prüft Python-Dateien auf Syntaxfehler mit ast.parse (kein Subprocess).

    Schneller als pylint, reicht für Syntax-Validierung.
    """
    path = Path(file_path)
    if not path.exists():
        return {"success": False, "error": f"Datei nicht gefunden: {file_path}", "issues": []}

    source = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
    try:
        ast.parse(source, filename=file_path)
        return {"success": True, "issues": [], "issue_count": 0}
    except SyntaxError as exc:
        return {
            "success": True,
            "issues": [{
                "line": exc.lineno or 0,
                "col": exc.offset or 0,
                "level": "error",
                "message": str(exc.msg),
                "text": exc.text or "",
            }],
            "issue_count": 1,
        }


async def read_file(file_path: str, max_lines: int = 500) -> dict[str, Any]:
    """
    Liest eine Datei und gibt den Inhalt zurück.

    Auf max_lines begrenzt um den Kontext nicht zu überlasten.
    Verweigert Dateien außerhalb erlaubter Verzeichnisse.
    """
    path = Path(file_path).resolve()

    # Sicherheit: keine Systemdateien lesen
    forbidden_prefixes = ["/etc/shadow", "/etc/passwd", "/proc", "/sys"]
    if any(str(path).startswith(p) for p in forbidden_prefixes):
        return {"success": False, "error": "Zugriff verweigert", "content": ""}

    if not path.exists():
        return {"success": False, "error": f"Datei nicht gefunden: {file_path}", "content": ""}
    if not path.is_file():
        return {"success": False, "error": f"Kein reguläres File: {file_path}", "content": ""}
    if path.stat().st_size > 5 * 1024 * 1024:  # 5 MB Limit
        return {"success": False, "error": "Datei zu groß (>5 MB)", "content": ""}

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    truncated = len(lines) > max_lines
    content = "\n".join(lines[:max_lines])

    return {
        "success": True,
        "content": content,
        "total_lines": len(lines),
        "truncated": truncated,
        "file_path": str(path),
    }


async def grep_pattern(
    pattern: str,
    directory: str,
    file_glob: str = "**/*.yaml",
    max_results: int = 50,
) -> dict[str, Any]:
    """
    Durchsucht Dateien nach einem Regex-Pattern (ripgrep wenn verfügbar, sonst Python).

    Gibt [{file, line_number, line_content}] zurück.
    """
    # Versuche ripgrep (viel schneller)
    rg_result = await _run_subprocess(
        ["rg", "--json", "-m", str(max_results), pattern, directory],
        timeout=15,
    )

    if rg_result["success"] or (rg_result["returncode"] == 1 and not rg_result["stderr"]):
        import json

        matches: list[dict[str, Any]] = []
        for line in rg_result["stdout"].splitlines():
            try:
                data = json.loads(line)
                if data.get("type") == "match":
                    sub = data["data"]
                    matches.append({
                        "file": sub["path"]["text"],
                        "line_number": sub["line_number"],
                        "line_content": sub["lines"]["text"].rstrip(),
                    })
            except (json.JSONDecodeError, KeyError):
                continue
        return {"success": True, "matches": matches, "match_count": len(matches), "tool": "ripgrep"}

    # Fallback: Python glob + re
    import glob

    matches = []
    compiled = re.compile(pattern)
    for file_path in glob.glob(f"{directory}/{file_glob}", recursive=True):
        if len(matches) >= max_results:
            break
        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                for lineno, content in enumerate(f, 1):
                    if compiled.search(content):
                        matches.append({
                            "file": file_path,
                            "line_number": lineno,
                            "line_content": content.rstrip(),
                        })
                        if len(matches) >= max_results:
                            break
        except OSError:
            continue

    return {"success": True, "matches": matches, "match_count": len(matches), "tool": "python-re"}


async def ha_config_check(config_path: str) -> dict[str, Any]:
    """
    Führt 'ha core check' bzw. 'hass --script check_config' aus.

    Gibt strukturierten Report mit Fehlern und Warnungen zurück.
    """
    # Versuche zuerst das HA-CLI
    for cmd in [
        ["ha", "core", "check"],
        ["hass", "--script", "check_config", "-c", config_path],
    ]:
        result = await _run_subprocess(cmd, timeout=60)
        if result["returncode"] != 127:  # 127 = command not found
            issues = _parse_ha_check_output(result["stdout"] + result["stderr"])
            return {
                "success": True,
                "command": " ".join(cmd),
                "returncode": result["returncode"],
                "issues": issues,
                "issue_count": len(issues),
                "raw_output": result["stdout"][:2000],
            }

    return {
        "success": False,
        "error": "weder 'ha' noch 'hass' CLI verfügbar",
        "issues": [],
    }


def _parse_ha_check_output(output: str) -> list[dict[str, Any]]:
    """Parst die Ausgabe des HA Config Checks in strukturierte Issues."""
    issues: list[dict[str, Any]] = []
    error_pattern = re.compile(r"(ERROR|WARNING)\s+(.+?)(?=ERROR|WARNING|$)", re.S)
    for m in error_pattern.finditer(output):
        issues.append({
            "level": m.group(1),
            "message": m.group(2).strip(),
        })
    return issues


async def jinja2_syntax_check(template: str) -> dict[str, Any]:
    """
    Prüft einen Jinja2-Template-String auf Syntaxfehler.

    Verwendet die HA-kompatible Jinja2-Umgebung (ohne HA-Extensions).
    """
    try:
        from jinja2 import Environment, TemplateSyntaxError

        env = Environment(autoescape=False)  # noqa: S701
        env.parse(template)
        return {"success": True, "valid": True, "error": None}
    except Exception as exc:  # jinja2 not installed or other error
        error_msg = str(exc)
        return {
            "success": True,
            "valid": False,
            "error": error_msg,
            "line": getattr(exc, "lineno", None),
        }


async def list_ha_files(config_path: str) -> dict[str, Any]:
    """
    Listet alle relevanten HA-Konfigurationsdateien in einem Verzeichnis.

    Kategorisiert nach: core, automations, scripts, blueprints, custom_components, esphome
    """
    import glob

    base = Path(config_path)
    if not base.exists():
        return {"success": False, "error": f"Pfad nicht gefunden: {config_path}"}

    categories: dict[str, list[str]] = {
        "core": [],
        "automations": [],
        "scripts": [],
        "scenes": [],
        "blueprints": [],
        "custom_components": [],
        "esphome": [],
        "lovelace": [],
        "packages": [],
    }

    patterns = {
        "core": ["configuration.yaml", "secrets.yaml", "known_devices.yaml"],
        "automations": ["automations.yaml", "automation/**/*.yaml"],
        "scripts": ["scripts.yaml", "script/**/*.yaml"],
        "scenes": ["scenes.yaml", "scene/**/*.yaml"],
        "blueprints": ["blueprints/**/*.yaml"],
        "custom_components": ["custom_components/**/*.py", "custom_components/**/manifest.json"],
        "esphome": ["esphome/**/*.yaml"],
        "lovelace": ["ui-lovelace.yaml", "lovelace/**/*.yaml", ".storage/lovelace*"],
        "packages": ["packages/**/*.yaml"],
    }

    for category, globs in patterns.items():
        for g in globs:
            for f in glob.glob(str(base / g), recursive=True):
                if f not in categories[category]:
                    categories[category].append(f)

    total = sum(len(v) for v in categories.values())
    return {"success": True, "categories": categories, "total_files": total}
