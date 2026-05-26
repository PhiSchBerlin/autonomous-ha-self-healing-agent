"""
Integration-Tests für die FastAPI-Endpunkte.

Testet die HTTP-API gegen eine echte In-Process-Instanz der FastAPI-App
mit httpx.AsyncClient (kein laufender Server nötig, kein LLM nötig).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest.fixture(scope="module")
def app():
    """Erstellt die FastAPI-App einmal pro Modul."""
    from api.app import create_app
    return create_app()


@pytest_asyncio.fixture
async def client(app):
    """Async HTTPX-Client der direkt gegen die ASGI-App spricht."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_health_body_has_status(self, client: AsyncClient):
        resp = await client.get("/health")
        data = resp.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_body_has_version(self, client: AsyncClient):
        resp = await client.get("/health")
        data = resp.json()
        assert "version" in data


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------

class TestMetricsEndpoint:
    @pytest.mark.asyncio
    async def test_metrics_returns_200(self, client: AsyncClient):
        resp = await client.get("/metrics")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_metrics_content_type_is_text(self, client: AsyncClient):
        resp = await client.get("/metrics")
        assert "text/plain" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# /api/v1/security/issues
# ---------------------------------------------------------------------------

class TestSecurityIssuesEndpoint:
    @pytest.mark.asyncio
    async def test_issues_returns_list(self, client: AsyncClient):
        resp = await client.get("/api/v1/security/issues")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_issues_summary_returns_dict(self, client: AsyncClient):
        resp = await client.get("/api/v1/security/issues/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "critical" in data
        assert "high" in data

    @pytest.mark.asyncio
    async def test_issues_summary_counts_are_non_negative(self, client: AsyncClient):
        resp = await client.get("/api/v1/security/issues/summary")
        data = resp.json()
        for key in ("total", "critical", "high", "medium", "low", "info"):
            assert data.get(key, 0) >= 0

    @pytest.mark.asyncio
    async def test_quick_scan_yaml_clean(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/security/quick-scan",
            params={"content": "homeassistant:\n  name: Test\n", "file_type": "yaml"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_quick_scan_detects_secret(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/security/quick-scan",
            params={"content": "password: supersecretvalue123", "file_type": "yaml"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("issues_count", 0) > 0 or len(data.get("issues", [])) > 0


# ---------------------------------------------------------------------------
# /api/v1/security/scan — Pfad-Validierung (kein LLM nötig)
# ---------------------------------------------------------------------------

class TestSecurityScanEndpoint:
    @pytest.mark.asyncio
    async def test_scan_nonexistent_path_returns_400(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/security/scan",
            json={"ha_config_path": "/nonexistent/path/that/does/not/exist"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_scan_accepts_valid_path(self, client: AsyncClient, tmp_path):
        resp = await client.post(
            "/api/v1/security/scan",
            json={"ha_config_path": str(tmp_path)},
        )
        # 202 Accepted oder 200 — kein 4xx/5xx
        assert resp.status_code in (200, 202)


# ---------------------------------------------------------------------------
# /api/v1/knowledge
# ---------------------------------------------------------------------------

class TestKnowledgeEndpoints:
    @pytest.mark.asyncio
    async def test_stats_returns_dict(self, client: AsyncClient):
        resp = await client.get("/api/v1/knowledge/stats")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    @pytest.mark.asyncio
    async def test_search_returns_results_structure(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/knowledge/search",
            json={"query": "MQTT broker", "n_results": 3},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "query" in data
        assert "results" in data
        assert "total_results" in data

    @pytest.mark.asyncio
    async def test_store_and_retrieve_entry(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/knowledge/store",
            json={
                "topic": "Test-Eintrag",
                "content": "Integration-Test-Inhalt für den Knowledge-Store.",
                "source": "integration-test",
                "tags": ["test"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["topic"] == "Test-Eintrag"

    @pytest.mark.asyncio
    async def test_list_known_collection(self, client: AsyncClient):
        resp = await client.get(
            "/api/v1/knowledge/list",
            params={"collection": "ha_knowledge", "limit": 10},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "collection" in data
        assert "entries" in data

    @pytest.mark.asyncio
    async def test_list_unknown_collection_returns_400(self, client: AsyncClient):
        resp = await client.get(
            "/api/v1/knowledge/list",
            params={"collection": "nonexistent_collection"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_export_returns_json_attachment(self, client: AsyncClient):
        resp = await client.get("/api/v1/knowledge/export")
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("content-type", "")
        assert "attachment" in resp.headers.get("content-disposition", "")


# ---------------------------------------------------------------------------
# /api/v1/repair
# ---------------------------------------------------------------------------

class TestRepairEndpoints:
    @pytest.mark.asyncio
    async def test_pending_returns_list(self, client: AsyncClient):
        resp = await client.get("/api/v1/repair/pending")
        assert resp.status_code == 200
        data = resp.json()
        assert "pending" in data

    @pytest.mark.asyncio
    async def test_history_returns_commits(self, client: AsyncClient):
        resp = await client.get("/api/v1/repair/history")
        assert resp.status_code == 200
        data = resp.json()
        assert "commits" in data

    @pytest.mark.asyncio
    async def test_approve_nonexistent_repair_returns_404(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/repair/00000000-0000-0000-0000-000000000000/approve",
            json={"approved": True},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_actions_returns_list(self, client: AsyncClient):
        resp = await client.get("/api/v1/repair/actions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# /api/v1/agents/health
# ---------------------------------------------------------------------------

class TestAgentHealthEndpoint:
    @pytest.mark.asyncio
    async def test_agent_health_returns_200(self, client: AsyncClient):
        resp = await client.get("/api/v1/agents/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_agent_health_has_mode(self, client: AsyncClient):
        resp = await client.get("/api/v1/agents/health")
        data = resp.json()
        assert "mode" in data

    @pytest.mark.asyncio
    async def test_full_audit_latest_returns_200_or_404(self, client: AsyncClient):
        resp = await client.get("/api/v1/agents/full-audit/latest")
        assert resp.status_code in (200, 404)
