"""
Persistentes Gedächtnis des Agenten — JSON-basiert, ohne C-Erweiterungen.

Ersetzt ChromaDB durch einen einfachen JSON-Store mit Keyword-Suche,
der auf Alpine/aarch64 ohne onnxruntime/hnswlib funktioniert.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

COLLECTION_FINDINGS = "ha_findings"
COLLECTION_REPAIRS = "ha_repairs"
COLLECTION_KNOWLEDGE = "ha_knowledge"


@dataclass
class VectorEntry:
    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    distance: float = 0.0


class HAVectorStore:
    """
    JSON-basierter Store für den HA Self-Healing Agent.

    Speichert Findings, Reparaturen und HA-Wissen als JSON-Dateien
    und bietet Keyword-basierte Ähnlichkeitssuche.
    """

    def __init__(self, persist_directory: str = "/data/chromadb") -> None:
        self.persist_directory = Path(persist_directory)
        self.persist_directory.mkdir(parents=True, exist_ok=True)
        self._collections: dict[str, dict[str, Any]] = {}

    def _get_collection(self, name: str) -> dict[str, Any]:
        if name in self._collections:
            return self._collections[name]

        path = self.persist_directory / f"{name}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except Exception:
                data = {}
        else:
            data = {}

        self._collections[name] = data
        return data

    def _save_collection(self, name: str) -> None:
        path = self.persist_directory / f"{name}.json"
        path.write_text(json.dumps(self._collections[name], ensure_ascii=False, indent=2))

    def _keyword_score(self, query: str, text: str) -> float:
        """Einfacher Keyword-Score: Anteil der Query-Wörter die im Text vorkommen."""
        query_words = set(query.lower().split())
        text_lower = text.lower()
        if not query_words:
            return 0.0
        matches = sum(1 for w in query_words if w in text_lower)
        return matches / len(query_words)

    def _search(
        self,
        collection_name: str,
        query: str,
        n_results: int,
        where: dict[str, str] | None = None,
    ) -> list[VectorEntry]:
        collection = self._get_collection(collection_name)
        entries = []
        for entry_id, entry in collection.items():
            metadata = entry.get("metadata", {})
            if where:
                if not all(metadata.get(k) == v for k, v in where.items()):
                    continue
            score = self._keyword_score(query, entry.get("text", ""))
            entries.append(VectorEntry(
                id=entry_id,
                text=entry.get("text", ""),
                metadata=metadata,
                distance=1.0 - score,
            ))
        entries.sort(key=lambda e: e.distance)
        return entries[:n_results]

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
        collection = self._get_collection(COLLECTION_FINDINGS)
        entry_id = finding_id or str(uuid4())
        text = f"Title: {title}\nDescription: {description}\nSeverity: {severity}\nCategory: {category}"
        collection[entry_id] = {
            "text": text,
            "metadata": {
                "title": title,
                "severity": severity,
                "category": category,
                "source_agent": source_agent,
                "stored_at": datetime.now(UTC).isoformat(),
                **(extra_metadata or {}),
            },
        }
        self._save_collection(COLLECTION_FINDINGS)
        logger.debug("Finding gespeichert: %s (%s)", title, entry_id)
        return entry_id

    def search_similar_findings(
        self,
        query: str,
        n_results: int = 5,
        severity_filter: str | None = None,
    ) -> list[VectorEntry]:
        where = {"severity": severity_filter} if severity_filter else None
        try:
            return self._search(COLLECTION_FINDINGS, query, n_results, where)
        except Exception as exc:
            logger.warning("Finding-Suche fehlgeschlagen: %s", exc)
            return []

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
        collection = self._get_collection(COLLECTION_REPAIRS)
        entry_id = repair_id or str(uuid4())
        outcome = "erfolgreich" if success else "fehlgeschlagen"
        text = (
            f"Reparatur: {title}\n"
            f"Problem: {finding_title}\n"
            f"Begründung: {rationale}\n"
            f"Ergebnis: {outcome}"
        )
        collection[entry_id] = {
            "text": text,
            "metadata": {
                "title": title,
                "finding_title": finding_title,
                "success": str(success),
                "risk_level": risk_level,
                "affected_files": ",".join(file_paths or []),
                "stored_at": datetime.now(UTC).isoformat(),
            },
        }
        self._save_collection(COLLECTION_REPAIRS)
        logger.debug("Reparatur gespeichert: %s (%s)", title, entry_id)
        return entry_id

    def search_similar_repairs(
        self,
        query: str,
        n_results: int = 5,
        only_successful: bool = True,
    ) -> list[VectorEntry]:
        where = {"success": "True"} if only_successful else None
        try:
            return self._search(COLLECTION_REPAIRS, query, n_results, where)
        except Exception as exc:
            logger.warning("Repair-Suche fehlgeschlagen: %s", exc)
            return []

    def store_knowledge(
        self,
        topic: str,
        content: str,
        source: str = "agent",
        tags: list[str] | None = None,
        knowledge_id: str | None = None,
    ) -> str:
        collection = self._get_collection(COLLECTION_KNOWLEDGE)
        entry_id = knowledge_id or str(uuid4())
        text = f"Thema: {topic}\n{content}"
        collection[entry_id] = {
            "text": text,
            "metadata": {
                "topic": topic,
                "source": source,
                "tags": ",".join(tags or []),
                "stored_at": datetime.now(UTC).isoformat(),
            },
        }
        self._save_collection(COLLECTION_KNOWLEDGE)
        return entry_id

    def search_knowledge(self, query: str, n_results: int = 3) -> list[VectorEntry]:
        try:
            return self._search(COLLECTION_KNOWLEDGE, query, n_results)
        except Exception as exc:
            logger.warning("Wissens-Suche fehlgeschlagen: %s", exc)
            return []

    def get_context_for_finding(self, title: str, description: str) -> str:
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

    def get_stats(self) -> dict[str, int]:
        stats: dict[str, int] = {}
        for name in (COLLECTION_FINDINGS, COLLECTION_REPAIRS, COLLECTION_KNOWLEDGE):
            try:
                stats[name] = len(self._get_collection(name))
            except Exception:
                stats[name] = 0
        stats["total_entries"] = sum(stats.values())
        return stats

    def delete_entry(self, collection_name: str, entry_id: str) -> bool:
        try:
            collection = self._get_collection(collection_name)
            if entry_id in collection:
                del collection[entry_id]
                self._save_collection(collection_name)
            return True
        except Exception as exc:
            logger.warning("Löschen fehlgeschlagen: %s", exc)
            return False
