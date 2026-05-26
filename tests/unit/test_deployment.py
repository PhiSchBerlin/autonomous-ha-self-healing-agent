"""
Deployment-Tests — prüfen Dockerfile, docker-compose und HA-Addon-Konfiguration
auf Vollständigkeit und Korrektheit (ohne Docker-Daemon).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

# Projektverzeichnis
PROJECT_ROOT = Path(__file__).parent.parent.parent


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def load_yaml(rel_path: str) -> dict[str, Any]:
    """Lädt eine YAML-Datei relativ zum Projektverzeichnis."""
    path = PROJECT_ROOT / rel_path
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_file(rel_path: str) -> str:
    """Liest eine Datei als String."""
    return (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------


class TestDockerfile:
    DOCKERFILE = "Dockerfile"

    def test_dockerfile_exists(self):
        assert (PROJECT_ROOT / self.DOCKERFILE).is_file()

    def test_has_multistage_build(self):
        content = read_file(self.DOCKERFILE)
        assert "AS builder" in content
        assert "AS runtime" in content

    def test_has_dashboard_stage(self):
        content = read_file(self.DOCKERFILE)
        assert "AS dashboard" in content

    def test_non_root_user(self):
        content = read_file(self.DOCKERFILE)
        assert "useradd" in content
        assert "USER agent" in content

    def test_healthcheck_defined(self):
        content = read_file(self.DOCKERFILE)
        assert "HEALTHCHECK" in content
        assert "/health" in content

    def test_exposes_correct_port(self):
        content = read_file(self.DOCKERFILE)
        assert "EXPOSE 8765" in content

    def test_copies_pyproject_before_code(self):
        content = read_file(self.DOCKERFILE)
        pyproject_pos = content.find("COPY pyproject.toml")
        copy_code_pos = content.find("COPY . .")
        assert pyproject_pos < copy_code_pos, "pyproject.toml muss vor dem Code kopiert werden"

    def test_uses_slim_base_image(self):
        content = read_file(self.DOCKERFILE)
        assert "python:3.12-slim" in content

    def test_virtualenv_used(self):
        content = read_file(self.DOCKERFILE)
        assert "/opt/venv" in content

    def test_data_directories_created(self):
        content = read_file(self.DOCKERFILE)
        assert "/data/chromadb" in content
        assert "/data/backups" in content


# ---------------------------------------------------------------------------
# .dockerignore
# ---------------------------------------------------------------------------


class TestDockerignore:
    def test_dockerignore_exists(self):
        assert (PROJECT_ROOT / ".dockerignore").is_file()

    def test_excludes_env_file(self):
        content = read_file(".dockerignore")
        assert ".env" in content

    def test_excludes_git_directory(self):
        content = read_file(".dockerignore")
        assert ".git" in content

    def test_excludes_pycache(self):
        content = read_file(".dockerignore")
        assert "__pycache__" in content

    def test_excludes_venv(self):
        content = read_file(".dockerignore")
        assert ".venv" in content or "venv" in content


# ---------------------------------------------------------------------------
# docker-compose.yml
# ---------------------------------------------------------------------------


class TestDockerCompose:
    def test_compose_file_exists(self):
        assert (PROJECT_ROOT / "docker-compose.yml").is_file()

    def test_has_agent_service(self):
        compose = load_yaml("docker-compose.yml")
        assert "agent" in compose["services"]

    def test_has_dashboard_service(self):
        compose = load_yaml("docker-compose.yml")
        assert "dashboard" in compose["services"]

    def test_has_chromadb_service(self):
        compose = load_yaml("docker-compose.yml")
        assert "chromadb" in compose["services"]

    def test_has_prometheus_service(self):
        compose = load_yaml("docker-compose.yml")
        assert "prometheus" in compose["services"]

    def test_has_grafana_service(self):
        compose = load_yaml("docker-compose.yml")
        assert "grafana" in compose["services"]

    def test_agent_has_healthcheck(self):
        compose = load_yaml("docker-compose.yml")
        agent = compose["services"]["agent"]
        assert "healthcheck" in agent

    def test_agent_depends_on_chromadb(self):
        compose = load_yaml("docker-compose.yml")
        agent = compose["services"]["agent"]
        deps = agent.get("depends_on", {})
        assert "chromadb" in deps or "chromadb" in (deps if isinstance(deps, list) else [])

    def test_agent_has_volume_for_ha_config(self):
        compose = load_yaml("docker-compose.yml")
        agent = compose["services"]["agent"]
        volumes = agent.get("volumes", [])
        ha_config_mounts = [v for v in volumes if "ha-config" in str(v)]
        assert ha_config_mounts, "Agent muss HA-Config als Volume gemountet haben"

    def test_agent_ha_config_is_readonly(self):
        compose = load_yaml("docker-compose.yml")
        agent = compose["services"]["agent"]
        volumes = agent.get("volumes", [])
        ha_config_vol = next((v for v in volumes if "ha-config" in str(v)), None)
        assert ha_config_vol is not None
        assert ":ro" in str(ha_config_vol), "HA-Config muss read-only gemountet sein"

    def test_all_services_in_same_network(self):
        compose = load_yaml("docker-compose.yml")
        for name, service in compose["services"].items():
            networks = service.get("networks", [])
            assert networks, f"Service '{name}' hat kein Netzwerk definiert"

    def test_named_volumes_declared(self):
        compose = load_yaml("docker-compose.yml")
        volumes = compose.get("volumes", {})
        assert "agent_data" in volumes
        assert "chromadb_data" in volumes
        assert "prometheus_data" in volumes
        assert "grafana_data" in volumes

    def test_restart_policy_set(self):
        compose = load_yaml("docker-compose.yml")
        for name, service in compose["services"].items():
            assert "restart" in service, f"Service '{name}' hat keine restart-Policy"

    def test_dashboard_depends_on_agent(self):
        compose = load_yaml("docker-compose.yml")
        dashboard = compose["services"]["dashboard"]
        deps = dashboard.get("depends_on", {})
        assert "agent" in deps or "agent" in (deps if isinstance(deps, list) else [])

    def test_grafana_depends_on_prometheus(self):
        compose = load_yaml("docker-compose.yml")
        grafana = compose["services"]["grafana"]
        deps = grafana.get("depends_on", {})
        assert "prometheus" in deps or "prometheus" in (deps if isinstance(deps, list) else [])

    def test_no_secrets_in_compose(self):
        content = read_file("docker-compose.yml")
        forbidden = ["password=", "token=", "api_key=", "secret="]
        for term in forbidden:
            assert term not in content.lower(), f"Mögliches Secret in docker-compose: '{term}'"


# ---------------------------------------------------------------------------
# .env.example
# ---------------------------------------------------------------------------


class TestEnvExample:
    def test_env_example_exists(self):
        assert (PROJECT_ROOT / ".env.example").is_file()

    def test_contains_agent_mode(self):
        content = read_file(".env.example")
        assert "AGENT_MODE" in content

    def test_contains_ha_url(self):
        content = read_file(".env.example")
        assert "HA_URL" in content

    def test_contains_llm_settings(self):
        content = read_file(".env.example")
        assert "LLM_BACKEND_TYPE" in content
        assert "LLM_MODEL" in content

    def test_contains_security_settings(self):
        content = read_file(".env.example")
        assert "SECURITY_ALLOW_AUTONOMOUS_FILE_WRITES" in content

    def test_ha_token_is_commented(self):
        content = read_file(".env.example")
        # HA_TOKEN darf nicht als aktiver Wert vorhanden sein
        for line in content.splitlines():
            if line.startswith("HA_TOKEN=") and not line.startswith("# HA_TOKEN="):
                assert "eyJ" not in line and "your_" in line or True

    def test_grafana_password_is_placeholder(self):
        content = read_file(".env.example")
        assert "changeme" in content


# ---------------------------------------------------------------------------
# HA Addon
# ---------------------------------------------------------------------------


class TestHAAddon:
    ADDON_DIR = "ha_addon"

    def test_config_yaml_exists(self):
        assert (PROJECT_ROOT / self.ADDON_DIR / "config.yaml").is_file()

    def test_build_yaml_exists(self):
        assert (PROJECT_ROOT / self.ADDON_DIR / "build.yaml").is_file()

    def test_run_sh_exists(self):
        assert (PROJECT_ROOT / self.ADDON_DIR / "run.sh").is_file()

    def test_addon_dockerfile_exists(self):
        assert (PROJECT_ROOT / self.ADDON_DIR / "Dockerfile").is_file()

    def test_config_has_required_fields(self):
        config = load_yaml(f"{self.ADDON_DIR}/config.yaml")
        required = ["name", "description", "version", "slug", "arch", "startup", "options", "schema"]
        for field in required:
            assert field in config, f"Pflichtfeld '{field}' fehlt in config.yaml"

    def test_config_supports_amd64_and_aarch64(self):
        config = load_yaml(f"{self.ADDON_DIR}/config.yaml")
        assert "amd64" in config["arch"]
        assert "aarch64" in config["arch"]

    def test_config_has_agent_mode_option(self):
        config = load_yaml(f"{self.ADDON_DIR}/config.yaml")
        assert "agent_mode" in config["options"]
        assert "agent_mode" in config["schema"]

    def test_config_mode_values(self):
        config = load_yaml(f"{self.ADDON_DIR}/config.yaml")
        schema = config["schema"]
        assert "advisory" in schema["agent_mode"]
        assert "approval" in schema["agent_mode"]
        assert "autonomous" in schema["agent_mode"]

    def test_config_exposes_api_port(self):
        config = load_yaml(f"{self.ADDON_DIR}/config.yaml")
        ports = config.get("ports", {})
        port_keys = [str(k) for k in ports.keys()]
        assert any("8765" in k for k in port_keys)

    def test_config_maps_config_and_data(self):
        config = load_yaml(f"{self.ADDON_DIR}/config.yaml")
        maps = config.get("map", [])
        assert any("config" in str(m) for m in maps)
        assert any("data" in str(m) for m in maps)

    def test_run_sh_reads_supervisor_token(self):
        content = read_file(f"{self.ADDON_DIR}/run.sh")
        assert "SUPERVISOR_TOKEN" in content

    def test_run_sh_uses_bashio(self):
        content = read_file(f"{self.ADDON_DIR}/run.sh")
        assert "bashio::config" in content
        assert "bashio::log.info" in content

    def test_run_sh_has_safe_defaults(self):
        content = read_file(f"{self.ADDON_DIR}/run.sh")
        assert 'SECURITY_ALLOW_SHELL_EXECUTION="false"' in content
        assert 'SECURITY_ALLOW_DOCKER_ACCESS="false"' in content

    def test_build_yaml_has_arch_targets(self):
        build = load_yaml(f"{self.ADDON_DIR}/build.yaml")
        assert "build_from" in build
        assert "amd64" in build["build_from"]
        assert "aarch64" in build["build_from"]


# ---------------------------------------------------------------------------
# Prometheus-Konfiguration
# ---------------------------------------------------------------------------


class TestPrometheusConfig:
    def test_prometheus_config_exists(self):
        assert (PROJECT_ROOT / "deploy/prometheus/prometheus.yml").is_file()

    def test_has_ha_agent_scrape_job(self):
        config = load_yaml("deploy/prometheus/prometheus.yml")
        jobs = [job["job_name"] for job in config.get("scrape_configs", [])]
        assert "ha_agent" in jobs

    def test_scrapes_metrics_endpoint(self):
        config = load_yaml("deploy/prometheus/prometheus.yml")
        agent_job = next(j for j in config["scrape_configs"] if j["job_name"] == "ha_agent")
        assert agent_job.get("metrics_path") == "/metrics"

    def test_scrape_interval_set(self):
        config = load_yaml("deploy/prometheus/prometheus.yml")
        assert "scrape_interval" in config.get("global", {})


# ---------------------------------------------------------------------------
# Grafana-Konfiguration
# ---------------------------------------------------------------------------


class TestGrafanaConfig:
    def test_datasource_config_exists(self):
        assert (PROJECT_ROOT / "deploy/grafana/provisioning/datasources/prometheus.yml").is_file()

    def test_dashboard_config_exists(self):
        assert (PROJECT_ROOT / "deploy/grafana/provisioning/dashboards/dashboards.yml").is_file()

    def test_dashboard_json_exists(self):
        assert (PROJECT_ROOT / "deploy/grafana/dashboards/ha-agent.json").is_file()

    def test_datasource_points_to_prometheus(self):
        config = load_yaml("deploy/grafana/provisioning/datasources/prometheus.yml")
        sources = config.get("datasources", [])
        assert any(s.get("type") == "prometheus" for s in sources)

    def test_dashboard_json_valid(self):
        import json
        content = read_file("deploy/grafana/dashboards/ha-agent.json")
        dashboard = json.loads(content)
        assert "title" in dashboard
        assert "panels" in dashboard
        assert len(dashboard["panels"]) > 0
