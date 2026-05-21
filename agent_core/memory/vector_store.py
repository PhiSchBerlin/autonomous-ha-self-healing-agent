"""
Abstraktion über ChromaDB für persistentes Gedächtnis des Agenten.

Speichert Findings, Reparaturen und HA-Wissen als Embeddings und
ermöglicht semantische Suche zur Kontextualisierung neuer Probleme.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

# Kollektion-Namen
COLLECTION_FINDINGS = "ha_findings"
COLLECTION_REPAIRS = "ha_repairs"
COLLECTION_KNOWLEDGE = "ha_knowledge"


@dataclass
class VectorEntry:
    """Ein gespeicherter Eintrag im Vector-Store."""

    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    distance: float = 0.0


class HAVectorStore:
    """
    ChromaDB-basierter Vector-Store für den HA Self-Healing Agent.

    Verwaltet drei Kollektionen:
    - findings: vergangene Fehler und Probleme
    - repairs: erfolgreiche Reparaturen mit ihren Ergebnissen
    - knowledge: allgemeines HA-Wissen (Best Practices, bekannte Bugs)

    Läuft vollständig lokal ohne externe API.
    """

    def __init__(self, persist_directory: str = "/data/chromadb") -> None:
        self.persist_directory = persist_directory
        self._client: Any = None
        self._collections: dict[str, Any] = {}

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError as exc:
            raise ImportError(
                "chromadb nicht installiert. Installation: pip install chromadb"
            ) from exc

        import os
        # ONNX-Modell-Cache auf persistentes /data-Volume legen, damit kein
        # 79-MB-Download bei jedem Addon-Neustart nötig ist.
        cache_dir = os.environ.get("CHROMA_CACHE_DIR", "/data/chroma_cache")
        os.makedirs(cache_dir, exist_ok=True)
        os.environ.setdefault("CHROMA_CACHE_DIR", cache_dir)

        self._client = chromadb.PersistentClient(
            path=self.persist_directory,
            settings=Settings(anonymized_telemetry=False),
        )
        logger.info("ChromaDB initialisiert in %s (cache=%s)", self.persist_directory, cache_dir)
        return self._client

    def _get_collection(self, name: str) -> Any:
        if name in self._collections:
            return self._collections[name]

        client = self._get_client()

        # Embedding-Funktion: sentence-transformers wenn vorhanden, sonst ChromaDB-Default
        embedding_function = self._get_embedding_function()
        kwargs: dict[str, Any] = {
            "name": name,
            "metadata": {"hnsw:space": "cosine"},
        }
        if embedding_function is not None:
            kwargs["embedding_function"] = embedding_function

        collection = client.get_or_create_collection(**kwargs)
        self._collections[name] = collection
        return collection

    def _get_embedding_function(self) -> Any:
        """
        Gibt die beste verfügbare Embedding-Funktion zurück.

        Priorität:
        1. sentence-transformers (lokal, kein Download wenn pip-Paket vorhanden)
        2. ChromaDB Default-EF mit persistentem Cache (CHROMA_CACHE_DIR=/data/chroma_cache)
        """
        try:
            from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
            return SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        except Exception:
            pass
        try:
            from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
            return ONNXMiniLM_L6_V2()
        except Exception:
            pass
        return None

    # -------------------------------------------------------------------------
    # Findings speichern und abrufen
    # -------------------------------------------------------------------------

    def store_finding(
        self,
        title: str,
        description: str,
        severity: str,
        category: str,
        source_agent: str,
        finding_id: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> str:
        """Speichert ein Finding im Vector-Store. Gibt die Entry-ID zurück."""
        collection = self._get_collection(COLLECTION_FINDINGS)
        entry_id = finding_id or str(uuid4())

        text = f"Title: {title}\nDescription: {description}\nSeverity: {severity}\nCategory: {category}"
        metadata = {
            "title": title,
            "severity": severity,
            "category": category,
            "source_agent": source_agent,
            "stored_at": datetime.now(UTC).isoformat(),
            **(extra_metadata or {}),
        }

        collection.upsert(
            ids=[entry_id],
            documents=[text],
            metadatas=[metadata],
        )
        logger.debug("Finding gespeichert: %s (%s)", title, entry_id)
        return entry_id

    def search_similar_findings(
        self,
        query: str,
        n_results: int = 5,
        severity_filter: str | None = None,
    ) -> list[VectorEntry]:
        """Sucht ähnliche vergangene Findings über semantische Ähnlichkeit."""
        collection = self._get_collection(COLLECTION_FINDINGS)

        try:
            where = {"severity": severity_filter} if severity_filter else None
            kwargs: dict[str, Any] = {
                "query_texts": [query],
                "n_results": min(n_results, max(1, collection.count())),
            }
            if where:
                kwargs["where"] = where

            results = collection.query(**kwargs)
            return self._parse_results(results)
        except Exception as exc:
            logger.warning("Finding-Suche fehlgeschlagen: %s", exc)
            return []

    # -------------------------------------------------------------------------
    # Reparaturen speichern und abrufen
    # -------------------------------------------------------------------------

    def store_repair(
        self,
        title: str,
        rationale: str,
        finding_title: str,
        success: bool,
        risk_level: str = "low",
        repair_id: str | None = None,
        file_paths: list[str] | None = None,
    ) -> str:
        """Speichert eine Reparatur (erfolgreich oder nicht) im Vector-Store."""
        collection = self._get_collection(COLLECTION_REPAIRS)
        entry_id = repair_id or str(uuid4())

        outcome = "erfolgreich" if success else "fehlgeschlagen"
        text = (
            f"Reparatur: {title}\n"
            f"Problem: {finding_title}\n"
            f"Begründung: {rationale}\n"
            f"Ergebnis: {outcome}"
        )
        metadata = {
            "title": title,
            "finding_title": finding_title,
            "success": str(success),
            "risk_level": risk_level,
            "affected_files": ",".join(file_paths or []),
            "stored_at": datetime.now(UTC).isoformat(),
        }

        collection.upsert(
            ids=[entry_id],
            documents=[text],
            metadatas=[metadata],
        )
        logger.debug("Reparatur gespeichert: %s (%s)", title, entry_id)
        return entry_id

    def search_similar_repairs(
        self,
        query: str,
        n_results: int = 5,
        only_successful: bool = True,
    ) -> list[VectorEntry]:
        """Sucht ähnliche vergangene Reparaturen — standardmäßig nur erfolgreiche."""
        collection = self._get_collection(COLLECTION_REPAIRS)

        try:
            where = {"success": "True"} if only_successful else None
            kwargs: dict[str, Any] = {
                "query_texts": [query],
                "n_results": min(n_results, max(1, collection.count())),
            }
            if where:
                kwargs["where"] = where

            results = collection.query(**kwargs)
            return self._parse_results(results)
        except Exception as exc:
            logger.warning("Repair-Suche fehlgeschlagen: %s", exc)
            return []

    # -------------------------------------------------------------------------
    # Allgemeines HA-Wissen
    # -------------------------------------------------------------------------

    def store_knowledge(
        self,
        topic: str,
        content: str,
        source: str = "agent",
        tags: list[str] | None = None,
        knowledge_id: str | None = None,
    ) -> str:
        """Speichert einen allgemeinen Wissenseintrag (Best Practices, bekannte Bugs)."""
        collection = self._get_collection(COLLECTION_KNOWLEDGE)
        entry_id = knowledge_id or str(uuid4())

        text = f"Thema: {topic}\n{content}"
        metadata = {
            "topic": topic,
            "source": source,
            "tags": ",".join(tags or []),
            "stored_at": datetime.now(UTC).isoformat(),
        }

        collection.upsert(
            ids=[entry_id],
            documents=[text],
            metadatas=[metadata],
        )
        return entry_id

    def search_knowledge(self, query: str, n_results: int = 3) -> list[VectorEntry]:
        """Sucht relevantes HA-Wissen für eine gegebene Frage/Problem-Beschreibung."""
        collection = self._get_collection(COLLECTION_KNOWLEDGE)

        try:
            count = collection.count()
            if count == 0:
                return []

            results = collection.query(
                query_texts=[query],
                n_results=min(n_results, count),
            )
            return self._parse_results(results)
        except Exception as exc:
            logger.warning("Wissens-Suche fehlgeschlagen: %s", exc)
            return []

    def get_context_for_finding(self, title: str, description: str) -> str:
        """
        Aggregiert relevanten Kontext für ein neues Finding aus allen Kollektionen.

        Gibt einen formatierten String zurück der direkt in LLM-Prompts
        eingefügt werden kann.
        """
        query = f"{title}: {description}"
        parts: list[str] = []

        similar_findings = self.search_similar_findings(query, n_results=3)
        if similar_findings:
            parts.append("## Ähnliche vergangene Probleme")
            for entry in similar_findings:
                parts.append(f"- {entry.text[:200]}")

        successful_repairs = self.search_similar_repairs(query, n_results=3)
        if successful_repairs:
            parts.append("\n## Erfolgreiche Reparaturen für ähnliche Probleme")
            for entry in successful_repairs:
                parts.append(f"- {entry.text[:200]}")

        knowledge = self.search_knowledge(query, n_results=2)
        if knowledge:
            parts.append("\n## Relevantes HA-Wissen")
            for entry in knowledge:
                parts.append(f"- {entry.text[:300]}")

        return "\n".join(parts) if parts else ""

    # -------------------------------------------------------------------------
    # Verwaltung
    # -------------------------------------------------------------------------

    def get_stats(self) -> dict[str, int]:
        """Gibt die Anzahl der Einträge pro Kollektion zurück."""
        stats: dict[str, int] = {}
        for name in (COLLECTION_FINDINGS, COLLECTION_REPAIRS, COLLECTION_KNOWLEDGE):
            try:
                collection = self._get_collection(name)
                stats[name] = collection.count()
            except Exception:
                stats[name] = 0
        stats["total_entries"] = sum(stats.values())
        return stats

    def delete_entry(self, collection_name: str, entry_id: str) -> bool:
        """Löscht einen einzelnen Eintrag aus einer Kollektion."""
        try:
            collection = self._get_collection(collection_name)
            collection.delete(ids=[entry_id])
            return True
        except Exception as exc:
            logger.warning("Löschen fehlgeschlagen: %s", exc)
            return False

    # -------------------------------------------------------------------------
    # Interne Hilfsmethoden
    # -------------------------------------------------------------------------

    @staticmethod
    def _parse_results(results: dict[str, Any]) -> list[VectorEntry]:
        """Konvertiert ChromaDB-Ergebnisse in VectorEntry-Objekte."""
        entries: list[VectorEntry] = []
        ids = results.get("ids", [[]])[0]
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for i, doc_id in enumerate(ids):
            entries.append(
                VectorEntry(
                    id=doc_id,
                    text=documents[i] if i < len(documents) else "",
                    metadata=metadatas[i] if i < len(metadatas) else {},
                    distance=distances[i] if i < len(distances) else 0.0,
                )
            )
        return entries
