"""Persistentes Gedächtnis und Vector-Store für den HA Self-Healing Agent."""

from .vector_store import HAVectorStore, VectorEntry

__all__ = ["HAVectorStore", "VectorEntry"]
