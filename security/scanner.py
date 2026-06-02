"""
Security-Scanner für Home Assistant Konfigurationen.

Führt statische Analyse auf YAML, Python, Shell und Dockerfiles durch.
Erkennt: Secrets, Injections, unsichere Konfigurationen, CVE-betroffene Pakete.

Alle Checks sind deterministisch (regex/AST-basiert) und benötigen kein LLM.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from models.enums import FindingSeverity, SecurityCheckType
from models.security_models import SecurityIssue

# ---------------------------------------------------------------------------
# Secrets-Detection: Regex-Patterns für häufige Credentials
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str, FindingSeverity]] = [
    # HA Long-Lived Access Tokens (eyJ... JWT-Format)
    (
        re.compile(r'(?i)(token|access_token|api_key|secret)\s*[:=]\s*["\']?(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)["\']?'),
        "JWT-Token im Klartext",
        FindingSeverity.CRITICAL,
    ),
    # Allgemeine Passwörter
    (
        re.compile(r'(?i)(password|passwd|pwd)\s*[:=]\s*["\']?(?!{{\s*)((?!\!secret)[^\s"\'{}]{6,})["\']?'),
        "Passwort im Klartext",
        FindingSeverity.CRITICAL,
    ),
    # AWS-Keys
    (
        re.compile(r"(?i)AKIA[0-9A-Z]{16}"),
        "AWS Access Key ID",
        FindingSeverity.CRITICAL,
    ),
    # Anthropic/OpenAI API Keys
    (
        re.compile(r"(?i)(sk-[A-Za-z0-9]{32,}|sk-ant-[A-Za-z0-9\-]{32,})"),
        "LLM API Key",
        FindingSeverity.CRITICAL,
    ),
    # Hardcoded IPs in nicht-lokalen Bereichen
    (
        re.compile(r'(?i)(host|server|broker)\s*[:=]\s*["\']?(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})["\']?'),
        "Hardcoded IP-Adresse",
        FindingSeverity.LOW,
    ),
    # SSH Private Keys
    (
        re.compile(r"-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----"),
        "SSH Private Key",
        FindingSeverity.CRITICAL,
    ),
    # Webhook URLs mit Tokens
    (
        re.compile(r'https?://[^\s"\']*/(webhook|hook)/[A-Za-z0-9_\-]{16,}'),
        "Webhook-URL mit Token",
        FindingSeverity.HIGH,
    ),
    # MQTT Passwörter
    (
        re.compile(r'(?i)mqtt.*password\s*[:=]\s*["\']?(?!{{\s*)([^\s"\'{}]{4,})["\']?'),
        "MQTT-Passwort im Klartext",
        FindingSeverity.HIGH,
    ),
]

# ---------------------------------------------------------------------------
# YAML-Injection-Patterns
# ---------------------------------------------------------------------------

_YAML_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Python-Object-Tags (YAML Deserialization) — echtes RCE-Risiko
    (re.compile(r"!!python/(object|module|name):"), "YAML Python-Object-Tag (Deserialization RCE)"),
    # YAML Merge-Keys (<<: *anchor) sind in ESPHome/HA-Config normale Syntax — kein Finding
]

# ---------------------------------------------------------------------------
# Jinja2-Injection-Patterns (in HA-Automations/Templates)
# ---------------------------------------------------------------------------

_JINJA2_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str, FindingSeverity]] = [
    (
        re.compile(r"\{\{.*__class__.*\}\}|\{\{.*__mro__.*\}\}|\{\{.*__subclasses__.*\}\}"),
        "Jinja2 SSTI (Server-Side Template Injection): Python-Interna-Zugriff",
        FindingSeverity.CRITICAL,
    ),
    (
        re.compile(r"\{\{.*\|.*attr\s*\(.*__.*__"),
        "Jinja2 SSTI: attr()-Filter auf Dunder-Attribut",
        FindingSeverity.CRITICAL,
    ),
    (
        re.compile(r"\{\{.*config\s*\["),
        "Jinja2: Zugriff auf config-Objekt (potenzielle Daten-Exfiltration)",
        FindingSeverity.HIGH,
    ),
    (
        re.compile(r"shell_command.*\{\{"),
        "Shell-Command mit Jinja2-Template (Injection-Risiko)",
        FindingSeverity.HIGH,
    ),
]

# ---------------------------------------------------------------------------
# Shell-Injection-Patterns
# ---------------------------------------------------------------------------

_SHELL_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"shell_command\s*:"), "shell_command-Integration (prüfen auf Injection)"),
    (re.compile(r"command\s*[:=]\s*['\"]?.*\$\{"), "Shell-Variable in Command (Injection-Risiko)"),
    (re.compile(r"(?i)(eval|exec)\s*\("), "eval()/exec() Aufruf"),
    (re.compile(r"os\.system\s*\(|subprocess\.call\s*\(.*shell\s*=\s*True"), "Unsicherer Subprocess-Aufruf"),
]

# ---------------------------------------------------------------------------
# Docker-Security-Patterns
# ---------------------------------------------------------------------------

_DOCKER_SECURITY_PATTERNS: list[tuple[re.Pattern[str], str, FindingSeverity]] = [
    (
        re.compile(r"(?im)^FROM\s+[^\n]+:latest\s*$"),
        "Docker: FROM mit :latest-Tag (kein pinned Image)",
        FindingSeverity.MEDIUM,
    ),
    (
        re.compile(r"(?im)^USER\s+root\s*$"),
        "Docker: Container läuft als root",
        FindingSeverity.HIGH,
    ),
    (
        re.compile(r"(?im)^RUN\s+.*curl\s+.*\|\s*(bash|sh)"),
        "Docker: curl | bash (Supply-Chain-Risiko)",
        FindingSeverity.CRITICAL,
    ),
    (
        re.compile(r"(?im)^RUN\s+.*--no-check-certificate"),
        "Docker: SSL-Zertifikatsprüfung deaktiviert",
        FindingSeverity.HIGH,
    ),
    (
        re.compile(r"(?im)^ENV\s+.*(PASSWORD|SECRET|TOKEN|KEY)\s*=\s*\S+"),
        "Docker: Secret als ENV-Variable",
        FindingSeverity.CRITICAL,
    ),
    (
        re.compile(r"(?im)privileged\s*:\s*true"),
        "Docker Compose: privileged: true",
        FindingSeverity.CRITICAL,
    ),
]

# ---------------------------------------------------------------------------
# Python-Code-Security-Patterns (Custom Components)
# ---------------------------------------------------------------------------

_PYTHON_SECURITY_PATTERNS: list[tuple[re.Pattern[str], str, FindingSeverity]] = [
    (
        re.compile(r"\beval\s*\("),
        "Python eval() — beliebige Code-Ausführung möglich",
        FindingSeverity.CRITICAL,
    ),
    (
        re.compile(r"\bexec\s*\("),
        "Python exec() — beliebige Code-Ausführung möglich",
        FindingSeverity.CRITICAL,
    ),
    (
        re.compile(r"pickle\.loads?\s*\("),
        "Python pickle.load() — Deserialization RCE",
        FindingSeverity.CRITICAL,
    ),
    (
        re.compile(r"subprocess\.(call|run|Popen)\s*\(.*shell\s*=\s*True"),
        "subprocess mit shell=True — Shell-Injection möglich",
        FindingSeverity.HIGH,
    ),
    (
        re.compile(r"import\s+requests\b"),
        "Blocking requests-Library in Custom Component (soll httpx/aiohttp sein)",
        FindingSeverity.MEDIUM,
    ),
    (
        re.compile(r"hass\.data\["),
        "Direkter hass.data-Zugriff (deprecated, entry.runtime_data nutzen)",
        FindingSeverity.LOW,
    ),
    (
        re.compile(r"verify\s*=\s*False"),
        "SSL-Zertifikatsprüfung deaktiviert (verify=False)",
        FindingSeverity.HIGH,
    ),
]


# ---------------------------------------------------------------------------
# Scanner-Funktionen
# ---------------------------------------------------------------------------

def scan_file_for_secrets(file_path: str, content: str) -> list[SecurityIssue]:
    """Scannt eine Datei auf Secrets und Credentials im Klartext."""
    issues: list[SecurityIssue] = []
    lines = content.splitlines()

    for lineno, line in enumerate(lines, 1):
        # Zeilen die explizit !secret nutzen überspringen (HA-Best-Practice)
        if "!secret" in line:
            continue
        # Kommentare überspringen
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue

        for pattern, title, severity in _SECRET_PATTERNS:
            m = pattern.search(line)
            if m:
                # Wert für Code-Snippet maskieren
                snippet = line.strip()[:120]
                issues.append(
                    SecurityIssue(
                        check_type=SecurityCheckType.SECRETS_DETECTION,
                        title=title,
                        description=f"{title} in {Path(file_path).name} (Zeile {lineno})",
                        severity=severity,
                        file_path=file_path,
                        line_number=lineno,
                        code_snippet=_redact_secret(snippet),
                        risk_score=_severity_to_score(severity),
                        remediation="Verwende HA !secret oder Umgebungsvariablen statt Klartext-Credentials",
                    )
                )

    return issues


def scan_file_for_yaml_injection(file_path: str, content: str) -> list[SecurityIssue]:
    """Prüft YAML-Dateien auf Injection-Muster."""
    issues: list[SecurityIssue] = []
    for pattern, description in _YAML_INJECTION_PATTERNS:
        for m in pattern.finditer(content):
            lineno = content[: m.start()].count("\n") + 1
            issues.append(
                SecurityIssue(
                    check_type=SecurityCheckType.YAML_INJECTION,
                    title=description,
                    description=f"{description} in {Path(file_path).name} (Zeile {lineno})",
                    severity=FindingSeverity.CRITICAL,
                    file_path=file_path,
                    line_number=lineno,
                    code_snippet=m.group(0)[:120],
                    risk_score=9.5,
                    remediation="YAML-Deserialization-Tags entfernen; niemals externe YAML-Quellen ohne Sanitisierung einbinden",
                )
            )
    return issues


def scan_file_for_jinja2_injection(file_path: str, content: str) -> list[SecurityIssue]:
    """Prüft Templates auf Jinja2 SSTI-Muster."""
    issues: list[SecurityIssue] = []
    for pattern, description, severity in _JINJA2_DANGEROUS_PATTERNS:
        for m in pattern.finditer(content):
            lineno = content[: m.start()].count("\n") + 1
            issues.append(
                SecurityIssue(
                    check_type=SecurityCheckType.JINJA2_INJECTION,
                    title=description,
                    description=f"{description} in {Path(file_path).name} (Zeile {lineno})",
                    severity=severity,
                    file_path=file_path,
                    line_number=lineno,
                    code_snippet=m.group(0)[:120],
                    risk_score=_severity_to_score(severity),
                    remediation="Jinja2-Templates niemals mit Benutzereingaben unkontrolliert verwenden",
                )
            )
    return issues


def scan_file_for_shell_injection(file_path: str, content: str) -> list[SecurityIssue]:
    """Prüft Dateien auf Shell-Injection-Risiken."""
    issues: list[SecurityIssue] = []
    for pattern, description in _SHELL_INJECTION_PATTERNS:
        for m in pattern.finditer(content):
            lineno = content[: m.start()].count("\n") + 1
            issues.append(
                SecurityIssue(
                    check_type=SecurityCheckType.SHELL_INJECTION,
                    title=description,
                    description=f"{description} in {Path(file_path).name} (Zeile {lineno})",
                    severity=FindingSeverity.HIGH,
                    file_path=file_path,
                    line_number=lineno,
                    code_snippet=m.group(0)[:120],
                    risk_score=7.5,
                    remediation="Shell-Commands validieren und sanitisieren; shell=False nutzen",
                )
            )
    return issues


def scan_dockerfile(file_path: str, content: str) -> list[SecurityIssue]:
    """Scannt Dockerfiles und docker-compose.yml auf Security-Probleme."""
    issues: list[SecurityIssue] = []
    for pattern, description, severity in _DOCKER_SECURITY_PATTERNS:
        for m in pattern.finditer(content):
            lineno = content[: m.start()].count("\n") + 1
            issues.append(
                SecurityIssue(
                    check_type=SecurityCheckType.DOCKER_SECURITY,
                    title=description,
                    description=f"{description} in {Path(file_path).name}",
                    severity=severity,
                    file_path=file_path,
                    line_number=lineno,
                    code_snippet=m.group(0).strip()[:120],
                    risk_score=_severity_to_score(severity),
                    remediation="Docker-Best-Practices anwenden: pinned Images, non-root User, Secrets nicht als ENV",
                )
            )
    return issues


def scan_python_file(file_path: str, content: str) -> list[SecurityIssue]:
    """Scannt Python-Dateien auf unsichere Patterns (regex + AST)."""
    issues: list[SecurityIssue] = []

    # Regex-basierte Checks
    for pattern, description, severity in _PYTHON_SECURITY_PATTERNS:
        for m in pattern.finditer(content):
            lineno = content[: m.start()].count("\n") + 1
            issues.append(
                SecurityIssue(
                    check_type=SecurityCheckType.PYTHON_EVAL,
                    title=description,
                    description=f"{description} in {Path(file_path).name} (Zeile {lineno})",
                    severity=severity,
                    file_path=file_path,
                    line_number=lineno,
                    code_snippet=m.group(0)[:120],
                    risk_score=_severity_to_score(severity),
                    remediation="Unsichere Python-Patterns ersetzen durch sichere Alternativen",
                )
            )

    # AST-basierte Checks: assert-Statements (bypassbar mit -O)
    try:
        tree = ast.parse(content, filename=file_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                issues.append(
                    SecurityIssue(
                        check_type=SecurityCheckType.PYTHON_EVAL,
                        title="assert-Statement für Sicherheitsprüfung",
                        description=(
                            f"assert in {Path(file_path).name} (Zeile {node.lineno}): "
                            "Kann mit -O deaktiviert werden"
                        ),
                        severity=FindingSeverity.LOW,
                        file_path=file_path,
                        line_number=node.lineno,
                        risk_score=1.5,
                        remediation="Sicherheitsprüfungen als if/raise statt assert implementieren",
                    )
                )
    except SyntaxError:
        pass  # Syntaxfehler werden vom python_syntax_check erfasst

    return issues


def scan_manifest_json(file_path: str, content: str) -> list[SecurityIssue]:
    """Prüft manifest.json von Custom Components auf bekannte Risiken."""
    import json

    issues: list[SecurityIssue] = []
    try:
        manifest = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return issues

    # Fehlende Felder die Sicherheitsrelevanz haben
    if not manifest.get("version"):
        issues.append(
            SecurityIssue(
                check_type=SecurityCheckType.INSECURE_COMPONENT,
                title="Custom Component ohne Versionsangabe",
                description=f"manifest.json in {Path(file_path).parent.name} hat kein 'version'-Feld",
                severity=FindingSeverity.LOW,
                file_path=file_path,
                risk_score=1.0,
                remediation="version-Feld in manifest.json hinzufügen (SemVer empfohlen)",
            )
        )

    # Anforderungen ohne gepinnte Versionen
    requirements: list[str] = manifest.get("requirements", [])
    unpinned = [r for r in requirements if not any(op in r for op in ("==", ">=", "<=", "~="))]
    if unpinned:
        issues.append(
            SecurityIssue(
                check_type=SecurityCheckType.SUPPLY_CHAIN,
                title="Ungepinnte Abhängigkeiten in Custom Component",
                description=(
                    f"Unpinned requirements in {Path(file_path).parent.name}: {', '.join(unpinned[:5])}"
                ),
                severity=FindingSeverity.MEDIUM,
                file_path=file_path,
                risk_score=4.0,
                remediation="Versionen in requirements pinnen (z.B. 'httpx>=0.27.0,<1.0.0')",
            )
        )

    # iot_class prüfen — cloud_polling/cloud_push erhöhen Angriffsfläche
    iot_class: str = manifest.get("iot_class", "")
    if iot_class in ("cloud_polling", "cloud_push"):
        issues.append(
            SecurityIssue(
                check_type=SecurityCheckType.INSECURE_COMPONENT,
                title="Custom Component mit Cloud-Abhängigkeit",
                description=(
                    f"{Path(file_path).parent.name} nutzt iot_class='{iot_class}': "
                    "Daten verlassen das lokale Netzwerk."
                ),
                severity=FindingSeverity.INFO,
                file_path=file_path,
                risk_score=1.0,
                remediation=(
                    "Prüfen ob eine lokale Alternative (local_polling/local_push) verfügbar ist. "
                    "Cloud-Integrationen nur aus vertrauenswürdigen Quellen installieren."
                ),
            )
        )

    # Fehlende codeowners — kein verantwortlicher Maintainer bekannt
    if not manifest.get("codeowners"):
        issues.append(
            SecurityIssue(
                check_type=SecurityCheckType.INSECURE_COMPONENT,
                title="Custom Component ohne Codeowner",
                description=(
                    f"{Path(file_path).parent.name} hat keinen 'codeowners'-Eintrag: "
                    "Kein verantwortlicher Maintainer dokumentiert."
                ),
                severity=FindingSeverity.INFO,
                file_path=file_path,
                risk_score=0.5,
                remediation="codeowners-Feld in manifest.json mit GitHub-Handles befüllen.",
            )
        )

    return issues


def _scan_directory_sync(
    directory: str,
    check_types: list[SecurityCheckType] | None,
) -> list[SecurityIssue]:
    """Synchroner Scan-Kern — wird via asyncio.to_thread() aufgerufen."""
    import glob

    all_issues: list[SecurityIssue] = []
    all_checks = check_types is None

    yaml_files = glob.glob(f"{directory}/**/*.yaml", recursive=True)
    py_files = glob.glob(f"{directory}/**/*.py", recursive=True)
    docker_files = glob.glob(f"{directory}/**/Dockerfile", recursive=True)
    docker_files += glob.glob(f"{directory}/**/docker-compose*.yml", recursive=True)
    manifest_files = glob.glob(f"{directory}/**/manifest.json", recursive=True)

    def _read(path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    for fp in yaml_files:
        content = _read(fp)
        if not content:
            continue
        if all_checks or SecurityCheckType.SECRETS_DETECTION in (check_types or []):
            all_issues.extend(scan_file_for_secrets(fp, content))
        if all_checks or SecurityCheckType.YAML_INJECTION in (check_types or []):
            all_issues.extend(scan_file_for_yaml_injection(fp, content))
        if all_checks or SecurityCheckType.JINJA2_INJECTION in (check_types or []):
            all_issues.extend(scan_file_for_jinja2_injection(fp, content))
        if all_checks or SecurityCheckType.SHELL_INJECTION in (check_types or []):
            all_issues.extend(scan_file_for_shell_injection(fp, content))

    for fp in py_files:
        content = _read(fp)
        if content:
            if all_checks or SecurityCheckType.PYTHON_EVAL in (check_types or []):
                all_issues.extend(scan_python_file(fp, content))
            if all_checks or SecurityCheckType.SECRETS_DETECTION in (check_types or []):
                all_issues.extend(scan_file_for_secrets(fp, content))

    for fp in docker_files:
        content = _read(fp)
        if content:
            if all_checks or SecurityCheckType.DOCKER_SECURITY in (check_types or []):
                all_issues.extend(scan_dockerfile(fp, content))

    for fp in manifest_files:
        content = _read(fp)
        if content:
            all_issues.extend(scan_manifest_json(fp, content))

    return all_issues


async def scan_directory(
    directory: str,
    check_types: list[SecurityCheckType] | None = None,
) -> list[SecurityIssue]:
    """
    Scannt ein vollständiges Verzeichnis auf alle konfigurierten Security-Issues.

    check_types=None → alle Checks aktiv.
    Datei-I/O läuft in einem Thread-Pool um den Event-Loop nicht zu blockieren.
    """
    import asyncio
    return await asyncio.to_thread(_scan_directory_sync, directory, check_types)


async def lookup_cve_osv(package_name: str, version: str | None = None) -> list[dict[str, Any]]:
    """
    Sucht CVEs für ein Python-Paket via OSV.dev API (Google Open Source Vulnerabilities).

    Kein API-Key erforderlich. Rate-Limit: 1000 Anfragen/Minute.
    """
    import httpx

    payload: dict[str, Any] = {"package": {"name": package_name, "ecosystem": "PyPI"}}
    if version:
        payload["version"] = version

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://api.osv.dev/v1/query",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("vulns", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _severity_to_score(severity: FindingSeverity) -> float:
    return {
        FindingSeverity.CRITICAL: 9.5,
        FindingSeverity.HIGH: 7.5,
        FindingSeverity.MEDIUM: 5.0,
        FindingSeverity.LOW: 2.5,
        FindingSeverity.INFO: 0.5,
    }.get(severity, 5.0)


def _redact_secret(text: str) -> str:
    """Maskiert erkannte Secrets im Code-Snippet für sichere Ausgabe."""
    # Alle Strings > 8 Zeichen nach = oder : maskieren
    return re.sub(
        r'([:=]\s*["\']?)([A-Za-z0-9_\-\.]{8,})(["\']?)',
        r"\1[REDACTED]\3",
        text,
    )
