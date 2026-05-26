"""Unit-Tests für den Security-Scanner."""

from __future__ import annotations

import pytest

from security.scanner import (
    _redact_secret,
    _severity_to_score,
    scan_dockerfile,
    scan_file_for_jinja2_injection,
    scan_file_for_secrets,
    scan_file_for_shell_injection,
    scan_file_for_yaml_injection,
    scan_manifest_json,
    scan_python_file,
)
from models.enums import FindingSeverity, SecurityCheckType


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

class TestSeverityToScore:
    def test_critical_is_highest(self):
        assert _severity_to_score(FindingSeverity.CRITICAL) == 9.5

    def test_high(self):
        assert _severity_to_score(FindingSeverity.HIGH) == 7.5

    def test_medium(self):
        assert _severity_to_score(FindingSeverity.MEDIUM) == 5.0

    def test_low(self):
        assert _severity_to_score(FindingSeverity.LOW) == 2.5

    def test_info_is_lowest(self):
        assert _severity_to_score(FindingSeverity.INFO) == 0.5


class TestRedactSecret:
    def test_redacts_password(self):
        result = _redact_secret("password: supersecret123")
        assert "supersecret123" not in result
        assert "REDACTED" in result

    def test_redacts_token(self):
        result = _redact_secret("token: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
        assert "eyJ" not in result or "REDACTED" in result

    def test_short_strings_not_redacted(self):
        result = _redact_secret("key: abc")
        assert "abc" in result  # Zu kurz um redacted zu werden


# ---------------------------------------------------------------------------
# Secrets-Detection
# ---------------------------------------------------------------------------

class TestScanFileForSecrets:
    def test_detects_plaintext_password(self):
        content = 'password: mysecretpassword123\nother: value'
        issues = scan_file_for_secrets("test.yaml", content)
        assert len(issues) > 0
        titles = [i.title for i in issues]
        assert any("Passwort" in t for t in titles)

    def test_detects_jwt_token(self):
        content = 'token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0LXVzZXItZmFrZSJ9.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA0'
        issues = scan_file_for_secrets("test.yaml", content)
        assert len(issues) > 0

    def test_detects_aws_key(self):
        content = 'AKIAIOSFODNN7EXAMPLE'
        issues = scan_file_for_secrets("test.yaml", content)
        assert len(issues) > 0
        assert any("AWS" in i.title for i in issues)

    def test_detects_ssh_private_key(self):
        content = '-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----'
        issues = scan_file_for_secrets("test.yaml", content)
        assert len(issues) > 0
        assert any("SSH" in i.title for i in issues)

    def test_skips_ha_secret_reference(self):
        content = 'password: !secret my_password'
        issues = scan_file_for_secrets("test.yaml", content)
        assert len(issues) == 0

    def test_skips_comments(self):
        content = '# password: hardcoded123456\nother: value'
        issues = scan_file_for_secrets("test.yaml", content)
        assert len(issues) == 0

    def test_detects_llm_api_key(self):
        content = 'api_key: sk-abcdefghijklmnopqrstuvwxyz1234567890ABCD'
        issues = scan_file_for_secrets("config.yaml", content)
        assert len(issues) > 0

    def test_check_type_is_secrets_detection(self):
        content = 'password: supersecretvalue123'
        issues = scan_file_for_secrets("test.yaml", content)
        for issue in issues:
            assert issue.check_type == SecurityCheckType.SECRETS_DETECTION

    def test_file_path_preserved(self):
        content = 'password: secretvalue12345'
        issues = scan_file_for_secrets("/config/secrets.yaml", content)
        if issues:
            assert issues[0].file_path == "/config/secrets.yaml"

    def test_clean_content_returns_empty(self):
        content = 'domain: homeassistant\nport: 8123'
        issues = scan_file_for_secrets("test.yaml", content)
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# YAML-Injection
# ---------------------------------------------------------------------------

class TestScanFileForYamlInjection:
    def test_detects_python_object_tag(self):
        content = "exploit: !!python/object:subprocess.Popen"
        issues = scan_file_for_yaml_injection("test.yaml", content)
        assert len(issues) > 0
        assert issues[0].severity == FindingSeverity.CRITICAL

    def test_detects_merge_key_injection(self):
        content = "<<: *anchor_ref\nkey: value"
        issues = scan_file_for_yaml_injection("test.yaml", content)
        assert len(issues) > 0

    def test_clean_yaml_no_issues(self):
        content = "homeassistant:\n  name: Test\n  unit_system: metric"
        issues = scan_file_for_yaml_injection("test.yaml", content)
        assert len(issues) == 0

    def test_check_type_is_yaml_injection(self):
        content = "evil: !!python/module:os"
        issues = scan_file_for_yaml_injection("test.yaml", content)
        for issue in issues:
            assert issue.check_type == SecurityCheckType.YAML_INJECTION


# ---------------------------------------------------------------------------
# Jinja2-Injection
# ---------------------------------------------------------------------------

class TestScanFileForJinja2Injection:
    def test_detects_ssti_class(self):
        content = "value_template: \"{{ ''.__class__.__mro__[1].__subclasses__() }}\""
        issues = scan_file_for_jinja2_injection("test.yaml", content)
        assert len(issues) > 0
        assert issues[0].severity == FindingSeverity.CRITICAL

    def test_detects_config_access(self):
        content = "value_template: \"{{ config['api_password'] }}\""
        issues = scan_file_for_jinja2_injection("test.yaml", content)
        assert len(issues) > 0

    def test_detects_shell_command_with_template(self):
        # Das Regex sucht 'shell_command' und '{{' auf derselben Zeile
        content = "shell_command: /bin/bash {{ states('input_text.cmd') }}"
        issues = scan_file_for_jinja2_injection("test.yaml", content)
        assert len(issues) > 0

    def test_normal_template_no_issue(self):
        content = "value_template: \"{{ states('sensor.temperature') }}\""
        issues = scan_file_for_jinja2_injection("test.yaml", content)
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# Shell-Injection
# ---------------------------------------------------------------------------

class TestScanFileForShellInjection:
    def test_detects_shell_command_integration(self):
        content = "shell_command:\n  run: /usr/bin/python3 script.py"
        issues = scan_file_for_shell_injection("test.yaml", content)
        assert len(issues) > 0

    def test_detects_shell_variable_injection(self):
        content = "command: /bin/bash -c \"${USER_INPUT}\""
        issues = scan_file_for_shell_injection("test.yaml", content)
        assert len(issues) > 0

    def test_detects_eval_call(self):
        content = "eval(user_input)"
        issues = scan_file_for_shell_injection("test.py", content)
        assert len(issues) > 0

    def test_detects_unsafe_subprocess(self):
        content = "subprocess.call(cmd, shell=True)"
        issues = scan_file_for_shell_injection("test.py", content)
        assert len(issues) > 0

    def test_clean_content_no_issues(self):
        content = "sensor:\n  - platform: template\n    sensors:\n      test: value"
        issues = scan_file_for_shell_injection("test.yaml", content)
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# Dockerfile-Scan
# ---------------------------------------------------------------------------

class TestScanDockerfile:
    def test_detects_latest_tag(self):
        content = "FROM python:latest\nRUN pip install requests"
        issues = scan_dockerfile("Dockerfile", content)
        assert any("latest" in i.title.lower() or ":latest" in i.title for i in issues)

    def test_detects_root_user(self):
        content = "FROM python:3.12\nUSER root\nRUN pip install requests"
        issues = scan_dockerfile("Dockerfile", content)
        assert any("root" in i.title.lower() for i in issues)

    def test_detects_curl_pipe_bash(self):
        content = "FROM ubuntu:22.04\nRUN curl https://example.com/install.sh | bash"
        issues = scan_dockerfile("Dockerfile", content)
        assert len(issues) > 0
        assert any(i.severity == FindingSeverity.CRITICAL for i in issues)

    def test_detects_secret_as_env(self):
        content = "FROM python:3.12\nENV API_PASSWORD=mysecret123"
        issues = scan_dockerfile("Dockerfile", content)
        assert any("Secret" in i.title or "ENV" in i.title for i in issues)

    def test_detects_privileged_compose(self):
        content = "services:\n  app:\n    image: myapp\n    privileged: true"
        issues = scan_dockerfile("docker-compose.yml", content)
        assert any("privileged" in i.title.lower() for i in issues)

    def test_safe_dockerfile_no_critical(self):
        content = "FROM python:3.12-slim\nUSER 1000\nCOPY . /app\nCMD [\"python\", \"main.py\"]"
        issues = scan_dockerfile("Dockerfile", content)
        critical = [i for i in issues if i.severity == FindingSeverity.CRITICAL]
        assert len(critical) == 0


# ---------------------------------------------------------------------------
# Python-Code-Scan
# ---------------------------------------------------------------------------

class TestScanPythonFile:
    def test_detects_eval(self):
        content = "result = eval(user_input)"
        issues = scan_python_file("custom_component.py", content)
        assert any("eval" in i.title.lower() for i in issues)
        assert any(i.severity == FindingSeverity.CRITICAL for i in issues)

    def test_detects_exec(self):
        content = "exec(compile(code, '<string>', 'exec'))"
        issues = scan_python_file("sensor.py", content)
        assert any("exec" in i.title.lower() for i in issues)

    def test_detects_pickle(self):
        content = "import pickle\ndata = pickle.loads(raw_bytes)"
        issues = scan_python_file("storage.py", content)
        assert any("pickle" in i.title.lower() for i in issues)

    def test_detects_subprocess_shell_true(self):
        content = "import subprocess\nsubprocess.run(cmd, shell=True)"
        issues = scan_python_file("helper.py", content)
        assert any("subprocess" in i.title.lower() or "shell=True" in i.title for i in issues)

    def test_detects_blocking_requests(self):
        content = "import requests\nresp = requests.get(url)"
        issues = scan_python_file("sensor.py", content)
        assert any("requests" in i.title.lower() for i in issues)

    def test_detects_verify_false(self):
        content = "session.get(url, verify=False)"
        issues = scan_python_file("api.py", content)
        assert any("verify=False" in i.title or "SSL" in i.title for i in issues)

    def test_detects_assert_via_ast(self):
        content = "def check_auth(token):\n    assert token is not None\n    return True"
        issues = scan_python_file("auth.py", content)
        assert any("assert" in i.title.lower() for i in issues)

    def test_clean_python_no_issues(self):
        content = (
            "import httpx\n\n"
            "async def fetch(url: str) -> dict:\n"
            "    async with httpx.AsyncClient() as client:\n"
            "        resp = await client.get(url)\n"
            "        return resp.json()\n"
        )
        issues = scan_python_file("clean.py", content)
        assert len(issues) == 0

    def test_syntax_error_handled_gracefully(self):
        content = "def broken(\n    return None"
        issues = scan_python_file("broken.py", content)
        # Kein Crash erwartet, nur evtl. regex-Treffer
        assert isinstance(issues, list)


# ---------------------------------------------------------------------------
# manifest.json-Scan
# ---------------------------------------------------------------------------

class TestScanManifestJson:
    def test_detects_missing_version(self):
        content = '{"domain": "my_component", "name": "My Component"}'
        issues = scan_manifest_json("custom_components/my_component/manifest.json", content)
        assert any("version" in i.title.lower() for i in issues)

    def test_detects_unpinned_requirements(self):
        content = '{"domain": "my_component", "version": "1.0.0", "requirements": ["httpx", "pydantic"]}'
        issues = scan_manifest_json("custom_components/my_component/manifest.json", content)
        assert any("Abhängigkeiten" in i.title or "requirement" in i.title.lower() for i in issues)

    def test_pinned_requirements_no_issue(self):
        content = '{"domain": "my_component", "version": "1.0.0", "requirements": ["httpx>=0.27.0,<1.0.0"]}'
        issues = scan_manifest_json("custom_components/my_component/manifest.json", content)
        supply_chain = [i for i in issues if i.check_type == SecurityCheckType.SUPPLY_CHAIN]
        assert len(supply_chain) == 0

    def test_invalid_json_returns_empty(self):
        content = "not valid json at all {"
        issues = scan_manifest_json("manifest.json", content)
        assert len(issues) == 0

    def test_complete_valid_manifest_minimal_issues(self):
        content = '{"domain": "test", "version": "2.0.0", "name": "Test", "requirements": ["httpx>=0.27.0"]}'
        issues = scan_manifest_json("manifest.json", content)
        # Keine Critical-Probleme erwartet
        assert all(i.severity != FindingSeverity.CRITICAL for i in issues)
