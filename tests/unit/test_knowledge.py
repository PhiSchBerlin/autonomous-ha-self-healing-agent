"""Unit-Tests für Knowledge Agent und Vector-Store (mit Mock-ChromaDB)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from agent_core.memory.vector_store import HAVectorStore, VectorEntry


# ---------------------------------------------------------------------------
# Fixtures: Mock-ChromaDB
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_collection() -> MagicMock:
    """Mock für eine ChromaDB-Collection."""
    collection = MagicMock()
    collection.count.return_value = 0
    collection.query.return_value = {
        "ids": [[]],
        "documents": [[]],
        "metadatas": [[]],
        "distances": [[]],
    }
    return collection


@pytest.fixture
def mock_chroma_client(mock_collection: MagicMock) -> MagicMock:
    """Mock für den ChromaDB-Client."""
    client = MagicMock()
    client.get_or_create_collection.return_value = mock_collection
    return client


@pytest.fixture
def vector_store(mock_chroma_client: MagicMock) -> HAVectorStore:
    """HAVectorStore mit Mock-ChromaDB-Client."""
    store = HAVectorStore(persist_directory="/tmp/test_chromadb")
    store._client = mock_chroma_client
    return store


# ---------------------------------------------------------------------------
# VectorEntry
# ---------------------------------------------------------------------------

class TestVectorEntry:
    def test_default_distance_is_zero(self):
        entry = VectorEntry(id="test-id", text="some text")
        assert entry.distance == 0.0

    def test_metadata_defaults_to_empty(self):
        entry = VectorEntry(id="test-id", text="some text")
        assert entry.metadata == {}

    def test_with_all_fields(self):
        entry = VectorEntry(
            id="abc",
            text="MQTT error",
            metadata={"severity": "high"},
            distance=0.15,
        )
        assert entry.id == "abc"
        assert entry.distance == 0.15
        assert entry.metadata["severity"] == "high"


# ---------------------------------------------------------------------------
# HAVectorStore — Grundfunktionen
# ---------------------------------------------------------------------------

class TestHAVectorStoreInit:
    def test_persist_directory_stored(self):
        store = HAVectorStore(persist_directory="/custom/path")
        assert store.persist_directory == "/custom/path"

    def test_client_initially_none(self):
        store = HAVectorStore()
        assert store._client is None

    def test_get_client_raises_without_chromadb(self):
        store = HAVectorStore()
        with patch.dict("sys.modules", {"chromadb": None}):
            with pytest.raises(ImportError, match="chromadb"):
                store._get_client()


class TestParseResults:
    def test_empty_results(self):
        results = {
            "ids": [[]],
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
        }
        entries = HAVectorStore._parse_results(results)
        assert entries == []

    def test_single_result(self):
        results = {
            "ids": [["id-1"]],
            "documents": [["MQTT error: broker unreachable"]],
            "metadatas": [[{"severity": "high"}]],
            "distances": [[0.12]],
        }
        entries = HAVectorStore._parse_results(results)
        assert len(entries) == 1
        assert entries[0].id == "id-1"
        assert entries[0].text == "MQTT error: broker unreachable"
        assert entries[0].metadata["severity"] == "high"
        assert entries[0].distance == 0.12

    def test_multiple_results(self):
        results = {
            "ids": [["id-1", "id-2", "id-3"]],
            "documents": [["Doc A", "Doc B", "Doc C"]],
            "metadatas": [[{}, {}, {}]],
            "distances": [[0.1, 0.2, 0.3]],
        }
        entries = HAVectorStore._parse_results(results)
        assert len(entries) == 3
        assert entries[2].distance == 0.3

    def test_missing_distances_default_to_zero(self):
        results = {
            "ids": [["id-1"]],
            "documents": [["text"]],
            "metadatas": [[{}]],
            "distances": [[]],
        }
        entries = HAVectorStore._parse_results(results)
        assert entries[0].distance == 0.0


# ---------------------------------------------------------------------------
# HAVectorStore — store_finding
# ---------------------------------------------------------------------------

class TestStoreFinding:
    def test_returns_entry_id(self, vector_store: HAVectorStore, mock_collection: MagicMock):
        entry_id = vector_store.store_finding(
            title="MQTT-Fehler",
            description="Broker nicht erreichbar",
            severity="high",
            category="security",
            source_agent="security_agent",
        )
        assert isinstance(entry_id, str)
        assert len(entry_id) > 0

    def test_uses_provided_finding_id(self, vector_store: HAVectorStore, mock_collection: MagicMock):
        custom_id = str(uuid4())
        returned_id = vector_store.store_finding(
            title="Test",
            description="Test desc",
            severity="low",
            category="log_error",
            source_agent="log_agent",
            finding_id=custom_id,
        )
        assert returned_id == custom_id

    def test_calls_upsert(self, vector_store: HAVectorStore, mock_collection: MagicMock):
        vector_store.store_finding(
            title="Test Finding",
            description="Some description",
            severity="medium",
            category="config_error",
            source_agent="config_agent",
        )
        mock_collection.upsert.assert_called_once()

    def test_metadata_includes_severity(self, vector_store: HAVectorStore, mock_collection: MagicMock):
        vector_store.store_finding(
            title="Test",
            description="Desc",
            severity="critical",
            category="security",
            source_agent="security_agent",
        )
        call_kwargs = mock_collection.upsert.call_args.kwargs
        metadata = call_kwargs["metadatas"][0]
        assert metadata["severity"] == "critical"

    def test_extra_metadata_merged(self, vector_store: HAVectorStore, mock_collection: MagicMock):
        vector_store.store_finding(
            title="Test",
            description="Desc",
            severity="high",
            category="security",
            source_agent="agent",
            extra_metadata={"run_id": "test-run-123"},
        )
        call_kwargs = mock_collection.upsert.call_args.kwargs
        metadata = call_kwargs["metadatas"][0]
        assert metadata["run_id"] == "test-run-123"


# ---------------------------------------------------------------------------
# HAVectorStore — store_repair
# ---------------------------------------------------------------------------

class TestStoreRepair:
    def test_returns_entry_id(self, vector_store: HAVectorStore, mock_collection: MagicMock):
        entry_id = vector_store.store_repair(
            title="Fix MQTT credentials",
            rationale="Credentials were hardcoded",
            finding_title="MQTT hardcoded password",
            success=True,
        )
        assert isinstance(entry_id, str)

    def test_success_stored_as_string(self, vector_store: HAVectorStore, mock_collection: MagicMock):
        vector_store.store_repair(
            title="Fix",
            rationale="Reason",
            finding_title="Problem",
            success=True,
        )
        call_kwargs = mock_collection.upsert.call_args.kwargs
        metadata = call_kwargs["metadatas"][0]
        assert metadata["success"] == "True"

    def test_failed_repair_stored(self, vector_store: HAVectorStore, mock_collection: MagicMock):
        vector_store.store_repair(
            title="Failed fix",
            rationale="Reason",
            finding_title="Problem",
            success=False,
        )
        call_kwargs = mock_collection.upsert.call_args.kwargs
        metadata = call_kwargs["metadatas"][0]
        assert metadata["success"] == "False"

    def test_file_paths_joined(self, vector_store: HAVectorStore, mock_collection: MagicMock):
        vector_store.store_repair(
            title="Fix",
            rationale="R",
            finding_title="P",
            success=True,
            file_paths=["/config/configuration.yaml", "/config/automations.yaml"],
        )
        call_kwargs = mock_collection.upsert.call_args.kwargs
        metadata = call_kwargs["metadatas"][0]
        assert "/config/configuration.yaml" in metadata["affected_files"]


# ---------------------------------------------------------------------------
# HAVectorStore — store_knowledge
# ---------------------------------------------------------------------------

class TestStoreKnowledge:
    def test_returns_entry_id(self, vector_store: HAVectorStore, mock_collection: MagicMock):
        entry_id = vector_store.store_knowledge(
            topic="MQTT Best Practice",
            content="Always use !secret for passwords",
        )
        assert isinstance(entry_id, str)

    def test_tags_joined(self, vector_store: HAVectorStore, mock_collection: MagicMock):
        vector_store.store_knowledge(
            topic="Test",
            content="Content",
            tags=["mqtt", "security"],
        )
        call_kwargs = mock_collection.upsert.call_args.kwargs
        metadata = call_kwargs["metadatas"][0]
        assert "mqtt" in metadata["tags"]
        assert "security" in metadata["tags"]

    def test_source_stored(self, vector_store: HAVectorStore, mock_collection: MagicMock):
        vector_store.store_knowledge(
            topic="Test",
            content="Content",
            source="user",
        )
        call_kwargs = mock_collection.upsert.call_args.kwargs
        metadata = call_kwargs["metadatas"][0]
        assert metadata["source"] == "user"


# ---------------------------------------------------------------------------
# HAVectorStore — Suchen
# ---------------------------------------------------------------------------

class TestSearch:
    def test_search_returns_empty_when_collection_empty(
        self, vector_store: HAVectorStore, mock_collection: MagicMock
    ):
        mock_collection.count.return_value = 0
        results = vector_store.search_similar_findings("MQTT error")
        assert results == []

    def test_search_finds_with_results(
        self, vector_store: HAVectorStore, mock_collection: MagicMock
    ):
        mock_collection.count.return_value = 3
        mock_collection.query.return_value = {
            "ids": [["id-1"]],
            "documents": [["MQTT broker error"]],
            "metadatas": [[{"severity": "high"}]],
            "distances": [[0.1]],
        }
        results = vector_store.search_similar_findings("MQTT problem", n_results=5)
        assert len(results) == 1
        assert results[0].text == "MQTT broker error"

    def test_search_repairs_with_success_filter(
        self, vector_store: HAVectorStore, mock_collection: MagicMock
    ):
        mock_collection.count.return_value = 2
        mock_collection.query.return_value = {
            "ids": [["repair-1"]],
            "documents": [["Fix MQTT"]],
            "metadatas": [[{"success": "True"}]],
            "distances": [[0.05]],
        }
        results = vector_store.search_similar_repairs("mqtt fix", only_successful=True)
        call_kwargs = mock_collection.query.call_args.kwargs
        assert call_kwargs.get("where") == {"success": "True"}

    def test_search_knowledge_query_sent(
        self, vector_store: HAVectorStore, mock_collection: MagicMock
    ):
        mock_collection.count.return_value = 1
        mock_collection.query.return_value = {
            "ids": [["k-1"]],
            "documents": [["MQTT best practice"]],
            "metadatas": [[{}]],
            "distances": [[0.2]],
        }
        vector_store.search_knowledge("mqtt security", n_results=3)
        call_kwargs = mock_collection.query.call_args.kwargs
        assert "mqtt security" in call_kwargs["query_texts"]


# ---------------------------------------------------------------------------
# HAVectorStore — get_context_for_finding
# ---------------------------------------------------------------------------

class TestGetContextForFinding:
    def test_returns_empty_string_when_no_data(
        self, vector_store: HAVectorStore, mock_collection: MagicMock
    ):
        mock_collection.count.return_value = 0
        context = vector_store.get_context_for_finding("Test", "Description")
        assert isinstance(context, str)

    def test_returns_formatted_context_with_data(
        self, vector_store: HAVectorStore, mock_collection: MagicMock
    ):
        mock_collection.count.return_value = 1
        mock_collection.query.return_value = {
            "ids": [["id-1"]],
            "documents": [["MQTT broker connection error"]],
            "metadatas": [[{"severity": "high"}]],
            "distances": [[0.1]],
        }
        context = vector_store.get_context_for_finding("MQTT Error", "Cannot connect to broker")
        assert len(context) > 0


# ---------------------------------------------------------------------------
# HAVectorStore — get_stats
# ---------------------------------------------------------------------------

class TestGetStats:
    def test_returns_stats_dict(
        self, vector_store: HAVectorStore, mock_collection: MagicMock
    ):
        mock_collection.count.return_value = 5
        stats = vector_store.get_stats()
        assert "total_entries" in stats
        assert stats["total_entries"] >= 0

    def test_total_is_sum_of_collections(
        self, vector_store: HAVectorStore, mock_collection: MagicMock
    ):
        mock_collection.count.return_value = 3
        stats = vector_store.get_stats()
        # 3 Kollektionen × 3 = 9
        assert stats["total_entries"] == 9


# ---------------------------------------------------------------------------
# HAVectorStore — delete_entry
# ---------------------------------------------------------------------------

class TestDeleteEntry:
    def test_delete_calls_collection_delete(
        self, vector_store: HAVectorStore, mock_collection: MagicMock
    ):
        result = vector_store.delete_entry("ha_findings", "some-id")
        assert result is True
        mock_collection.delete.assert_called_once_with(ids=["some-id"])

    def test_delete_returns_false_on_exception(
        self, vector_store: HAVectorStore, mock_collection: MagicMock
    ):
        mock_collection.delete.side_effect = Exception("Collection error")
        result = vector_store.delete_entry("ha_findings", "some-id")
        assert result is False
