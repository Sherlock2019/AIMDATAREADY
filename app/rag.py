"""Permission-aware document search (RAG retrieval) with Chroma, embedded, no server.

    pipeline/documents/*.md     SIMULATED policies and supplier agreements (front matter: doc_id, version, groups)
    pipeline/documents/acl.yaml which roles are in which document group (simulated DMS / SharePoint groups)

The permission filter runs INSIDE the vector query (Chroma `where`): a chunk is only a candidate when
  1. its (doc_id, version) is approved and valid today in the SAP Ariba document register (staged CSV), and
  2. one of the document's groups contains the requesting role.
Unauthorized passages are never returned, so neither the agent nor the user can see them.

Embeddings: Chroma's default local model (all-MiniLM-L6-v2, ONNX, downloaded once, no API cost). If it cannot be
loaded (offline machine), a deterministic hashing embedding is used and named in the index version.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml
from chromadb.api.types import EmbeddingFunction

DIM = 384


class HashingEmbedding(EmbeddingFunction):
    """Deterministic bag-of-words hashing embedding (offline fallback and tests). Lexical, not semantic."""

    def __init__(self) -> None:
        pass

    def __call__(self, input):  # noqa: A002 (Chroma's parameter name)
        out = []
        for text in input:
            vec = [0.0] * DIM
            for tok in re.findall(r"[a-z0-9]+", text.lower()):
                vec[int(hashlib.md5(tok.encode()).hexdigest(), 16) % DIM] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    @staticmethod
    def name() -> str:
        return "tdr-hashing-384"

    def get_config(self) -> dict[str, Any]:
        return {}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> HashingEmbedding:
        return HashingEmbedding()


def parse(path: Path) -> tuple[dict, list[tuple[str, str]]]:
    """Front matter + one chunk per '## ' section (intro text before the first heading is skipped)."""
    text = path.read_text()
    _, front, body = text.split("---", 2)
    meta = yaml.safe_load(front)
    sections = []
    for block in re.split(r"(?m)^## ", body)[1:]:
        heading, _, content = block.partition("\n")
        sections.append((heading.strip(), " ".join(content.split())))
    return meta, sections


class DocumentIndex:
    def __init__(self, folder: Path, store: Path, lineage=None, embedding=None):
        self.folder, self.store, self.lineage = Path(folder), Path(store), lineage
        self.embedding = embedding  # None -> Chroma default local model, with hashing fallback
        self._col, self._version, self._ef_name = None, None, None
        self._lock = threading.RLock()  # re-entrant: certified-data methods call collection() under the lock
        self._client = self._ef_obj = None
        acl = yaml.safe_load((self.folder / "acl.yaml").read_text()) if (self.folder / "acl.yaml").exists() else {}
        self.groups = {g: set(members) for g, members in (acl.get("groups") or {}).items()}

    def files(self) -> list[Path]:
        return sorted(self.folder.glob("*.md"))

    def groups_for(self, role_key: str) -> list[str]:
        return sorted(g for g, members in self.groups.items() if role_key in members)

    def _ef(self):
        if self.embedding is not None:
            return self.embedding
        if os.environ.get("TDR_RAG_EMBEDDING") == "hashing":  # tests / offline machines
            return HashingEmbedding()
        try:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

            ef = DefaultEmbeddingFunction()
            ef(["warm-up"])  # downloads / loads the local ONNX model once
            return ef
        except Exception:  # offline or model unavailable: deterministic fallback, named in the index version
            return HashingEmbedding()

    def warm_up(self) -> None:
        """Build the index in a background thread (the first model load takes ~20 s)."""
        threading.Thread(target=self.collection, name="rag-warm-up", daemon=True).start()

    def collection(self):
        with self._lock:
            return self._collection()

    def _collection(self):
        if self._col is not None:
            return self._col
        import chromadb
        from chromadb.config import Settings

        ef = self._ef()
        self._ef_name = ef.name() if callable(getattr(ef, "name", None)) else type(ef).__name__
        if self._ef_name == "default":
            self._ef_name = "all-MiniLM-L6-v2 local ONNX"
        digest = hashlib.sha256(self._ef_name.encode())
        for path in self.files():
            digest.update(path.read_bytes())
        self._version = digest.hexdigest()[:12]
        client = chromadb.PersistentClient(path=str(self.store), settings=Settings(anonymized_telemetry=False))
        self._client, self._ef_obj = client, ef
        name = f"documents_{self._version}"
        existing = {c.name if hasattr(c, "name") else c for c in client.list_collections()}
        if name in existing:
            self._col = client.get_collection(name, embedding_function=ef)
            return self._col
        for old in existing:  # one live document index version (the certified-data collection is kept)
            if old.startswith("documents_"):
                client.delete_collection(old)
        col = client.create_collection(name, embedding_function=ef, metadata={"index_version": self._version})
        ids, docs, metas = [], [], []
        for path in self.files():
            meta, sections = parse(path)
            for i, (heading, content) in enumerate(sections):
                ids.append(f"{meta['doc_id']}@{meta['version']}#{i}")
                docs.append(f"{meta['title']} - {heading}: {content}")
                m = {"doc_key": f"{meta['doc_id']}@{meta['version']}", "doc_id": meta["doc_id"],
                     "version": str(meta["version"]), "title": meta["title"], "section": heading}  # fmt: skip
                m.update({f"g_{g}": True for g in meta.get("groups") or []})
                metas.append(m)
        if ids:
            col.add(ids=ids, documents=docs, metadatas=metas)
        if self.lineage is not None:
            from app.lineage import InputDataset, OutputDataset

            self.lineage.run("rag_index_build",
                             [InputDataset(namespace="file://pipeline/documents", name=p.name) for p in self.files()],
                             [OutputDataset(namespace="chroma://rag", name="documents")])  # fmt: skip
        self._col = col
        return col

    def version(self) -> str:
        self.collection()
        return f"{self._version} ({self._ef_name}, one chunk per section)"

    # ------------------------------------------------------------------ certified (published) data sets
    def _certified(self):
        self.collection()  # shares the client and the embedding model
        name = "certified_" + hashlib.sha256(self._ef_name.encode()).hexdigest()[:8]
        return self._client.get_or_create_collection(name, embedding_function=self._ef_obj)

    def index_rows(self, asset: str, version: int, label: str, rows: list[dict], restricted: set[str]) -> int:
        """Ingest one published version: one chunk per row. Restricted columns (e.g. supplier prices) are NEVER
        indexed, so no search can leak them. The previous version of the asset is replaced."""
        with self._lock:
            col = self._certified()
            col.delete(where={"asset": asset})
            if not rows:
                return 0
            ids, docs, metas = [], [], []
            for i, row in enumerate(rows):
                fields = "; ".join(f"{k} {v}" for k, v in row.items() if k not in restricted and v not in ("", None))
                ids.append(f"{asset}@v{version}#{i}")
                docs.append(f"{label} (certified v{version}): {fields}")
                metas.append({"asset": asset, "version": version, "label": label,
                              "wh": row.get("warehouse_id") or "ALL",
                              "product": row.get("product_id") or "ALL"})  # fmt: skip
            col.add(ids=ids, documents=docs, metadatas=metas)
            return len(ids)

    def search_rows(self, query: str, assets: list[str], warehouses: list[str] | None = None, k: int = 5) -> list[dict]:
        """Search only the given (task-authorized, published) assets; row ACL on warehouse when the role has one."""
        if not assets:
            return []
        clauses: list[dict] = [{"asset": {"$in": assets}}]
        if warehouses:
            clauses.append({"wh": {"$in": [*warehouses, "ALL"]}})
        where = clauses[0] if len(clauses) == 1 else {"$and": clauses}
        with self._lock:
            col = self._certified()
            n = len(col.get(where=where, include=[])["ids"])
            if not n:
                return []
            res = col.query(query_texts=[query], n_results=min(k, n), where=where,
                            include=["documents", "metadatas", "distances"])  # fmt: skip
        found = zip(res["documents"][0], res["metadatas"][0], res["distances"][0], strict=True)
        return [{"asset": m["asset"], "label": m["label"], "version": m["version"], "text": d.split(": ", 1)[-1],
                 "distance": round(float(x), 3)} for d, m, x in found]  # fmt: skip

    def clear_certified(self) -> None:
        if self._col is None and not self.store.exists():
            return
        with self._lock:
            col = self._certified()
            ids = col.get(include=[])["ids"]
            if ids:
                col.delete(ids=ids)

    def search(self, query: str, role_key: str, allowed: dict[str, str], k: int = 3) -> dict[str, Any]:
        """allowed = {doc_id: version} approved and valid today (from the SAP Ariba register)."""
        groups = self.groups_for(role_key)
        base = {"groups": groups, "passages": [], "searchable": []}
        if not groups or not allowed:
            return base
        group_clause = [{f"g_{g}": True} for g in groups]
        where = {"$and": [{"doc_key": {"$in": [f"{d}@{v}" for d, v in allowed.items()]}},
                          group_clause[0] if len(group_clause) == 1 else {"$or": group_clause}]}  # fmt: skip
        col = self.collection()
        visible = col.get(where=where, include=["metadatas"])["metadatas"]
        base["searchable"] = sorted({f"{m['doc_id']} v{m['version']}" for m in visible})
        if not visible:
            return base
        res = col.query(query_texts=[query], n_results=min(k, len(visible)), where=where,
                        include=["documents", "metadatas", "distances"])  # fmt: skip
        for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0], strict=True):
            base["passages"].append({"doc_id": meta["doc_id"], "version": meta["version"], "title": meta["title"],
                                     "section": meta["section"], "text": doc.split(": ", 1)[-1],
                                     "distance": round(float(dist), 3)})  # fmt: skip
        return base
