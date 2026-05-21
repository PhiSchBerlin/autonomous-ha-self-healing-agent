"""API-Router für den Knowledge Agent (RAG/Vector-Store)."""

from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class KnowledgeSearchRequest(BaseModel):
    query: str
    n_results: int = 5


class KnowledgeStoreRequest(BaseModel):
    topic: str
    content: str
    source: str = "user"
    tags: list[str] = []


class FullAuditRequest(BaseModel):
    raw_logs: list[dict[str, Any]] | None = None
    ha_config_path: str | None = "/config"
    use_knowledge: bool = True


def _get_vector_store() -> Any:
    """Direkter Zugriff auf den Vector-Store ohne LLM-Initialisierung."""
    from config.settings import get_settings
    from agent_core.memory.vector_store import HAVectorStore

    settings = get_settings()
    return HAVectorStore(persist_directory=getattr(settings, "chromadb_path", "/data/chromadb"))


def _get_knowledge_agent() -> Any:
    from agent_core.orchestrator import AgentOrchestrator
    from config.settings import get_settings
    from llm_backends.base import LLMBackendConfig
    from llm_backends.factory import create_llm_backend
    from models.enums import AgentMode

    settings = get_settings()
    llm_config = LLMBackendConfig(
        backend_type=settings.llm.backend_type,
        model=settings.llm.model,
        base_url=settings.llm.base_url,
        api_key=settings.llm.api_key.get_secret_value() if settings.llm.api_key else None,
    )
    llm = create_llm_backend(llm_config)
    orchestrator = AgentOrchestrator(llm, mode=AgentMode(settings.agent.mode))
    return orchestrator.get_knowledge_agent()


@router.post("/search")
async def search_knowledge(request: KnowledgeSearchRequest) -> dict[str, Any]:
    """Semantische Suche über alle Wissensdatenbanken."""
    vs = _get_vector_store()
    findings = [{"id": e.id, "text": e.text, "metadata": e.metadata, "distance": e.distance} for e in vs.search_similar_findings(request.query, request.n_results)]
    repairs = [{"id": e.id, "text": e.text, "metadata": e.metadata, "distance": e.distance} for e in vs.search_similar_repairs(request.query, request.n_results)]
    knowledge = [{"id": e.id, "text": e.text, "metadata": e.metadata, "distance": e.distance} for e in vs.search_knowledge(request.query, request.n_results)]
    return {
        "query": request.query,
        "results": findings + repairs + knowledge,
        "findings": findings,
        "repairs": repairs,
        "knowledge": knowledge,
        "total_results": len(findings) + len(repairs) + len(knowledge),
    }


@router.post("/store", status_code=status.HTTP_201_CREATED)
async def store_knowledge(request: KnowledgeStoreRequest) -> dict[str, Any]:
    """Speichert einen neuen Wissenseintrag (kein LLM benötigt)."""
    vs = _get_vector_store()
    entry_id = vs.store_knowledge(
        topic=request.topic,
        content=request.content,
        source=request.source,
        tags=request.tags,
    )
    return {"id": entry_id, "topic": request.topic, "source": request.source}


@router.get("/stats")
async def get_knowledge_stats() -> dict[str, Any]:
    """Gibt Statistiken über alle Wissensdatenbanken zurück."""
    vs = _get_vector_store()
    return vs.get_stats()


@router.get("/list")
async def list_knowledge(collection: str = "ha_knowledge", limit: int = 100) -> dict[str, Any]:
    """Listet alle Einträge einer Kollektion auf."""
    from agent_core.memory.vector_store import COLLECTION_FINDINGS, COLLECTION_REPAIRS, COLLECTION_KNOWLEDGE
    allowed = {COLLECTION_FINDINGS, COLLECTION_REPAIRS, COLLECTION_KNOWLEDGE}
    if collection not in allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unbekannte Kollektion. Erlaubt: {sorted(allowed)}")

    vs = _get_vector_store()
    try:
        col = vs._get_collection(collection)
        count = col.count()
        if count == 0:
            return {"collection": collection, "total": 0, "entries": []}
        result = col.get(limit=min(limit, count), include=["documents", "metadatas"])
        entries = [
            {"id": rid, "text": doc, "metadata": meta}
            for rid, doc, meta in zip(
                result.get("ids", []),
                result.get("documents", []),
                result.get("metadatas", []),
            )
        ]
        return {"collection": collection, "total": count, "entries": entries}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("/update/{entry_id}", status_code=status.HTTP_200_OK)
async def update_knowledge(entry_id: str, request: KnowledgeStoreRequest) -> dict[str, Any]:
    """Überschreibt einen bestehenden Wissenseintrag (upsert per ID)."""
    vs = _get_vector_store()
    try:
        col = vs._get_collection("ha_knowledge")
        from datetime import UTC, datetime
        text = f"Thema: {request.topic}\n{request.content}"
        metadata = {
            "topic": request.topic,
            "source": request.source,
            "tags": ",".join(request.tags),
            "stored_at": datetime.now(UTC).isoformat(),
        }
        col.upsert(ids=[entry_id], documents=[text], metadatas=[metadata])
        return {"id": entry_id, "topic": request.topic}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/delete/{collection}/{entry_id}", status_code=status.HTTP_200_OK)
async def delete_knowledge(collection: str, entry_id: str) -> dict[str, Any]:
    """Löscht einen Eintrag aus einer Kollektion."""
    vs = _get_vector_store()
    ok = vs.delete_entry(collection, entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Eintrag nicht gefunden oder Löschen fehlgeschlagen")
    return {"deleted": entry_id, "collection": collection}


@router.get("/export")
async def export_knowledge() -> StreamingResponse:
    """Exportiert alle Wissenseinträge als JSON-Datei."""
    import json
    from agent_core.memory.vector_store import COLLECTION_FINDINGS, COLLECTION_REPAIRS, COLLECTION_KNOWLEDGE

    vs = _get_vector_store()
    export: dict[str, Any] = {}
    for coll_name in (COLLECTION_FINDINGS, COLLECTION_REPAIRS, COLLECTION_KNOWLEDGE):
        try:
            col = vs._get_collection(coll_name)
            count = col.count()
            if count > 0:
                result = col.get(limit=count, include=["documents", "metadatas"])
                export[coll_name] = [
                    {"id": rid, "text": doc, "metadata": meta}
                    for rid, doc, meta in zip(
                        result.get("ids", []),
                        result.get("documents", []),
                        result.get("metadatas", []),
                    )
                ]
            else:
                export[coll_name] = []
        except Exception:
            export[coll_name] = []

    content = json.dumps(export, ensure_ascii=False, indent=2)
    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=knowledge_export.json"},
    )


class KnowledgeImportRequest(BaseModel):
    entries: list[dict[str, Any]]
    collection: str = "ha_knowledge"
    overwrite: bool = False


@router.post("/import", status_code=status.HTTP_200_OK)
async def import_knowledge(request: KnowledgeImportRequest) -> dict[str, Any]:
    """Importiert Wissenseinträge aus einem vorherigen Export."""
    from agent_core.memory.vector_store import COLLECTION_FINDINGS, COLLECTION_REPAIRS, COLLECTION_KNOWLEDGE
    allowed = {COLLECTION_FINDINGS, COLLECTION_REPAIRS, COLLECTION_KNOWLEDGE}
    if request.collection not in allowed:
        raise HTTPException(status_code=400, detail=f"Unbekannte Kollektion. Erlaubt: {sorted(allowed)}")

    vs = _get_vector_store()
    col = vs._get_collection(request.collection)
    imported = 0
    skipped = 0

    for entry in request.entries:
        entry_id = entry.get("id")
        text = entry.get("text", "")
        metadata = entry.get("metadata", {})
        if not entry_id or not text:
            skipped += 1
            continue
        try:
            if not request.overwrite:
                existing = col.get(ids=[entry_id])
                if existing.get("ids"):
                    skipped += 1
                    continue
            col.upsert(ids=[entry_id], documents=[text], metadatas=[metadata])
            imported += 1
        except Exception:
            skipped += 1

    return {"imported": imported, "skipped": skipped, "collection": request.collection}


@router.post("/full-audit", status_code=status.HTTP_202_ACCEPTED)
async def run_full_audit(request: FullAuditRequest) -> dict[str, Any]:
    """
    Führt einen vollständigen Audit durch: Log-Analyse + Config-Analyse + Security-Scan + Knowledge.

    Alle Agenten laufen koordiniert, Findings werden im Knowledge-Store persistiert.
    """
    from agent_core.orchestrator import AgentOrchestrator
    from config.settings import get_settings
    from llm_backends.base import LLMBackendConfig
    from llm_backends.factory import create_llm_backend
    from models.enums import AgentMode

    settings = get_settings()
    llm_config = LLMBackendConfig(
        backend_type=settings.llm.backend_type,
        model=settings.llm.model,
        base_url=settings.llm.base_url,
        api_key=settings.llm.api_key.get_secret_value() if settings.llm.api_key else None,
    )
    llm = create_llm_backend(llm_config)
    orchestrator = AgentOrchestrator(llm, mode=AgentMode(settings.agent.mode))

    results = await orchestrator.run_full_audit(
        raw_logs=request.raw_logs,
        ha_config_path=request.ha_config_path,
        use_knowledge=request.use_knowledge,
    )

    summary: dict[str, Any] = {"agents_run": list(results.keys())}
    for agent_name, result in results.items():
        summary[agent_name] = {
            "run_id": str(result.run_id),
            "success": result.success,
            "duration_seconds": result.duration_seconds,
            "errors": result.errors,
        }

    return summary
