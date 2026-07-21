"""Graph repository backends.

Production can use Neo4j AuraDB as the graph/vector store while local demos keep
the existing JSON filesystem behavior.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import unicodedata
from typing import Any

import networkx as nx

from storage import file_store as fs


_ASCII_WORD_RE = re.compile(r"[a-z0-9]+")
_ASCII_TERM_RE = re.compile(r"^[a-z0-9]+$")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_ASCII_QUERY_STOPWORDS = {
    "a", "about", "an", "and", "are", "at", "be", "been", "being", "by",
    "can", "could", "did", "do", "does", "for", "from", "how", "in", "is",
    "it", "me", "of", "on", "or", "please", "should", "tell", "the", "this",
    "to", "was", "were", "what", "when", "where", "which", "who", "why", "with",
    "would",
}
_CJK_QUERY_NOISE_PHRASES = tuple(sorted({
    "是什么", "是多少", "在哪里", "什么时候", "为什么",
    "有哪些", "是哪个", "是哪些", "怎么样", "能不能",
    "是否", "什么", "为何", "怎么", "怎样", "如何", "多少", "哪个", "哪些",
    "哪里", "何时", "时候", "可以", "应该", "关于", "一下", "请问",
}, key=len, reverse=True))
_LEXICAL_CHUNK_TEXT_LIMIT = 1600
_LEXICAL_CHUNK_CONTEXT_BEFORE = 240


def _normalized_lexical_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).lower()


def _lexical_terms(value: Any) -> list[str]:
    """Return conservative English tokens and CJK bigrams for page retrieval."""

    normalized = _normalized_lexical_text(value)
    for phrase in _CJK_QUERY_NOISE_PHRASES:
        normalized = normalized.replace(phrase, " ")
    terms: list[str] = []
    terms.extend(
        word for word in _ASCII_WORD_RE.findall(normalized)
        if len(word) >= 2 and word not in _ASCII_QUERY_STOPWORDS
    )
    for run in _CJK_RUN_RE.findall(normalized):
        if len(run) == 1:
            continue
        if len(run) == 2:
            terms.append(run)
            continue
        terms.extend(
            run[index:index + 2]
            for index in range(len(run) - 1)
        )
    # Preserve order for deterministic SQL parameters and scoring.
    return list(dict.fromkeys(terms))[:32]


def _lexical_chunk_score(query: Any, text: Any) -> float:
    query_text = _normalized_lexical_text(query)
    chunk_text = _normalized_lexical_text(text)
    terms = _lexical_terms(query_text)
    if not terms or not chunk_text:
        return 0.0

    ascii_words = set(_ASCII_WORD_RE.findall(chunk_text))
    matched = [
        term for term in terms
        if (term in ascii_words if _ASCII_TERM_RE.fullmatch(term) else term in chunk_text)
    ]
    if not matched:
        return 0.0
    # Multi-term questions require corroboration. A single substantial token
    # remains useful for entity/product-name searches such as "Python".
    if len(terms) > 1 and len(matched) < 2:
        return 0.0
    if len(terms) == 1 and len(terms[0]) < 2:
        return 0.0

    coverage = len(matched) / len(terms)
    score = float(len(matched)) + coverage
    compact_query = re.sub(r"\s+", "", query_text)
    compact_chunk = re.sub(r"\s+", "", chunk_text)
    if compact_query and compact_query in compact_chunk:
        score += 2.0
    return round(score, 6)


def _node_id(value: Any) -> str:
    return str(value or "")


def _clean_props(data: dict) -> dict:
    return {k: v for k, v in data.items() if v is not None}


def _edge_id(edge: dict, doc_id: str) -> str:
    explicit = str(edge.get("id") or "").strip()
    if explicit:
        return explicit
    source = str(edge.get("source") or "")
    target = str(edge.get("target") or "")
    relation = str(edge.get("relation") or "RELATED_TO")
    page = str(edge.get("page") or 0)
    raw = "|".join((doc_id, min(source, target), max(source, target), relation, page))
    return "edge_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class FileGraphRepository:
    def profile(self) -> dict:
        return {"backend": "filesystem", **fs.storage_profile()}

    def health(self) -> dict:
        return {
            "status": "ok",
            "backend": "filesystem",
            "kg_nodes_exists": fs.kg_nodes_path().exists(),
            "kg_edges_exists": fs.kg_edges_path().exists(),
            **fs.storage_profile(),
        }

    def _load_nodes(self) -> list[dict]:
        return fs.load_kg_nodes()

    def _load_edges(self) -> list[dict]:
        return fs.load_kg_edges()

    def _load_graph(self) -> nx.Graph:
        nodes = self._load_nodes()
        edges = self._load_edges()
        graph = nx.Graph()
        for node in nodes:
            graph.add_node(node["id"], **node)
        for edge in edges:
            graph.add_edge(
                edge["source"],
                edge["target"],
                relation=edge.get("relation", "CO_OCCURS_IN"),
                doc_id=edge.get("doc_id", ""),
                page=edge.get("page", 0),
            )
        return graph

    def get_nodes(self, page: int = 1, page_size: int = 50,
                  node_type: str | None = None,
                  doc_id: str | None = None,
                  confidence: str | None = None) -> dict:
        nodes = [dict(item) for item in self._load_nodes()]
        graph = self._load_graph()
        degrees = dict(graph.degree())
        for node in nodes:
            node["degree"] = degrees.get(node["id"], 0)

        if node_type:
            nodes = [node for node in nodes if node.get("type", "").upper() == node_type.upper()]
        if doc_id:
            nodes = [node for node in nodes if node.get("source_doc") == doc_id]
        if confidence:
            nodes = [node for node in nodes if node.get("confidence") == confidence]

        total = len(nodes)
        start = (max(page, 1) - 1) * page_size
        return {"total": total, "page": page, "page_size": page_size, "items": nodes[start:start + page_size]}

    def get_edges(self, page: int = 1, page_size: int = 100,
                  doc_id: str | None = None,
                  relation: str | None = None) -> dict:
        edges = [dict(item) for item in self._load_edges()]
        if doc_id:
            edges = [edge for edge in edges if edge.get("doc_id") == doc_id]
        if relation:
            edges = [edge for edge in edges if edge.get("relation") == relation]
        total = len(edges)
        start = (max(page, 1) - 1) * page_size
        return {"total": total, "page": page, "page_size": page_size, "items": edges[start:start + page_size]}

    def get_node_detail(self, node_id: str) -> dict | None:
        nodes = [dict(item) for item in self._load_nodes()]
        node = next((item for item in nodes if item["id"] == node_id), None)
        if not node:
            return None
        graph = self._load_graph()
        if node_id not in graph:
            node["degree"] = 0
            node["degree_centrality"] = 0.0
            node["neighbor_count"] = 0
            return node
        degree = graph.degree(node_id)
        centrality = nx.degree_centrality(graph)
        node["degree"] = degree
        node["degree_centrality"] = round(centrality.get(node_id, 0.0), 4)
        node["neighbor_count"] = degree
        return node

    def get_neighbors(self, node_id: str, hops: int = 1) -> dict | None:
        nodes = [dict(item) for item in self._load_nodes()]
        node = next((item for item in nodes if item["id"] == node_id), None)
        if not node:
            return None
        graph = self._load_graph()
        if node_id not in graph:
            return {
                "center": {"id": node_id, "name": node["name"], "type": node["type"], "page": node.get("page", 0)},
                "hops": hops,
                "neighbors_by_hop": {},
                "total_neighbors": 0,
            }
        hops = max(1, min(hops, 3))
        reachable = nx.single_source_shortest_path_length(graph, node_id, cutoff=hops)
        by_hop: dict[str, list] = {}
        for reachable_id, distance in reachable.items():
            if distance == 0:
                continue
            data = graph.nodes[reachable_id]
            by_hop.setdefault(str(distance), []).append({
                "id": reachable_id,
                "name": data.get("name", ""),
                "type": data.get("type", ""),
                "page": data.get("page", 0),
            })
        return {
            "center": {"id": node_id, "name": node["name"], "type": node["type"], "page": node.get("page", 0)},
            "hops": hops,
            "neighbors_by_hop": by_hop,
            "total_neighbors": sum(len(items) for items in by_hop.values()),
        }

    def get_stats(self) -> dict:
        nodes = self._load_nodes()
        edges = self._load_edges()
        graph = self._load_graph()

        type_dist: dict[str, int] = {}
        for node in nodes:
            node_type = node.get("type", "UNKNOWN")
            type_dist[node_type] = type_dist.get(node_type, 0) + 1

        relation_types: dict[str, int] = {}
        for edge in edges:
            relation = edge.get("relation", "CO_OCCURS_IN")
            relation_types[relation] = relation_types.get(relation, 0) + 1

        top5: list[dict] = []
        if graph.number_of_nodes() > 0:
            centrality = nx.degree_centrality(graph)
            for node_id, score in sorted(centrality.items(), key=lambda item: item[1], reverse=True)[:5]:
                data = graph.nodes[node_id]
                top5.append({
                    "node_id": node_id,
                    "name": data.get("name", ""),
                    "type": data.get("type", ""),
                    "centrality": round(score, 4),
                })

        return {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "density": round(nx.density(graph), 4) if graph.number_of_nodes() > 1 else 0.0,
            "type_distribution": type_dist,
            "relation_types": relation_types,
            "top5_central_nodes": top5,
            "source_documents": list({node.get("source_doc", "") for node in nodes if node.get("source_doc")}),
        }

    def export_kg(self, doc_id: str | None = None) -> dict:
        from datetime import datetime, timezone

        nodes = [dict(item) for item in self._load_nodes()]
        edges = [dict(item) for item in self._load_edges()]
        graph = self._load_graph()
        degrees = dict(graph.degree())
        for node in nodes:
            node["degree"] = degrees.get(node["id"], 0)
        if doc_id:
            nodes = [node for node in nodes if node.get("source_doc") == doc_id]
            edges = [edge for edge in edges if edge.get("doc_id") == doc_id]
        return {
            "format": "json",
            "doc_id": doc_id,
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "nodes": nodes,
            "edges": edges,
        }

    def search_entities(self, q: str, entity_type: str | None = None, limit: int = 15) -> dict:
        nodes = [dict(item) for item in self._load_nodes()]
        graph = self._load_graph()
        degrees = dict(graph.degree())
        q_lower = q.lower()
        matches = [node for node in nodes if q_lower in node.get("name", "").lower()]
        if entity_type:
            matches = [node for node in matches if node.get("type", "").upper() == entity_type.upper()]
        for node in matches:
            node["degree"] = degrees.get(node["id"], 0)
        return {"query": q, "total": len(matches), "items": matches[:limit]}

    def search_path(self, from_id: str, to_id: str, max_hops: int = 3) -> dict | None:
        nodes = [dict(item) for item in self._load_nodes()]
        node_map = {node["id"]: node for node in nodes}
        if from_id not in node_map or to_id not in node_map:
            return None

        graph = self._load_graph()
        max_hops = max(1, min(max_hops, 5))
        try:
            raw_paths = list(nx.all_simple_paths(graph, from_id, to_id, cutoff=max_hops))
        except nx.NetworkXError:
            raw_paths = []

        paths = []
        for path_nodes in raw_paths:
            path_edges = []
            for index in range(len(path_nodes) - 1):
                source, target = path_nodes[index], path_nodes[index + 1]
                edge_data = graph.edges[source, target]
                path_edges.append({"source": source, "target": target, "relation": edge_data.get("relation", "CO_OCCURS_IN")})
            paths.append({
                "length": len(path_nodes) - 1,
                "nodes": [{"id": node_id, "name": node_map.get(node_id, {}).get("name", node_id), "type": node_map.get(node_id, {}).get("type", "")} for node_id in path_nodes],
                "edges": path_edges,
            })

        return {
            "from": {"id": from_id, "name": node_map[from_id].get("name", ""), "type": node_map[from_id].get("type", "")},
            "to": {"id": to_id, "name": node_map[to_id].get("name", ""), "type": node_map[to_id].get("type", "")},
            "max_hops": max_hops,
            "paths": paths,
            "total_paths": len(paths),
        }

    def search_graph(self, q: str, include_neighbors: bool = False) -> dict:
        nodes = [dict(item) for item in self._load_nodes()]
        edges = [dict(item) for item in self._load_edges()]
        graph = self._load_graph()
        degrees = dict(graph.degree())
        q_lower = q.lower()
        matched = [node for node in nodes if q_lower in node.get("name", "").lower()]
        matched_ids = {node["id"] for node in matched}
        for node in matched:
            node["degree"] = degrees.get(node["id"], 0)

        relevant_ids = set(matched_ids)
        if include_neighbors:
            for node_id in matched_ids:
                if node_id in graph:
                    relevant_ids.update(graph.neighbors(node_id))

        subgraph_edges = [
            edge for edge in edges
            if edge.get("source") in relevant_ids and edge.get("target") in relevant_ids
        ]
        return {"query": q, "matched_nodes": matched, "subgraph_edges": subgraph_edges}

    def hybrid_retrieve(self, q: str, embedding: list[float] | None = None,
                        limit: int = 8, include_neighbors: bool = True,
                        allowed_document_ids: set[str] | None = None) -> dict:
        graph_result = self.search_graph(q, include_neighbors=include_neighbors)
        nodes = list(graph_result.get("matched_nodes", []))
        edges = list(graph_result.get("subgraph_edges", []))
        if allowed_document_ids is not None:
            nodes = [
                node for node in nodes
                if str(node.get("source_doc") or node.get("doc_id") or "") in allowed_document_ids
            ]
            edges = [
                edge for edge in edges
                if str(edge.get("doc_id") or "") in allowed_document_ids
            ]
        return {
            "query": q,
            "nodes": nodes[:limit],
            "edges": edges[: max(limit * 4, 20)],
            "chunks": [],
        }

    def upsert_document_graph(self, document: dict, nodes: list[dict], edges: list[dict], chunks: list[dict] | None = None) -> None:
        fs.merge_kg(nodes, edges, document["doc_id"])

    def remove_document(self, doc_id: str) -> tuple[int, int]:
        return fs.remove_doc_from_kg(doc_id)


class PostgresGraphRepository(FileGraphRepository):
    """Durable graph storage backed by Neon/Postgres JSONB tables.

    Query behavior intentionally reuses the proven NetworkX read model from
    ``FileGraphRepository`` while the source of truth lives in Postgres.
    """

    def __init__(self):
        self.database_url = os.getenv("DATABASE_URL", os.getenv("POSTGRES_URL", "")).strip()
        self._cache_expires_at = 0.0
        self._cached_nodes: list[dict] = []
        self._cached_edges: list[dict] = []

    def profile(self) -> dict:
        return {
            "backend": "postgres",
            "url_configured": bool(self.database_url),
            "persistent": True,
            "persistence": "persistent",
        }

    def _connect(self):
        if not self.database_url:
            raise ValueError("DATABASE_URL is required when GRAPHRAG_GRAPH_BACKEND=postgres")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Install psycopg[binary]>=3.2.0 to use GRAPHRAG_GRAPH_BACKEND=postgres") from exc
        return psycopg.connect(self.database_url, connect_timeout=5, row_factory=dict_row)

    def _jsonb(self, data: dict | list):
        try:
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise RuntimeError("Install psycopg[binary]>=3.2.0 to use GRAPHRAG_GRAPH_BACKEND=postgres") from exc
        return Jsonb(data)

    def ensure_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS graph_documents (
              doc_id TEXT PRIMARY KEY,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS graph_nodes (
              node_id TEXT NOT NULL,
              source_doc TEXT NOT NULL,
              node_type TEXT NOT NULL DEFAULT '',
              name TEXT NOT NULL DEFAULT '',
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              PRIMARY KEY (node_id, source_doc)
            )
            """,
            "CREATE INDEX IF NOT EXISTS graph_nodes_doc_idx ON graph_nodes(source_doc)",
            "CREATE INDEX IF NOT EXISTS graph_nodes_type_idx ON graph_nodes(node_type)",
            "CREATE INDEX IF NOT EXISTS graph_nodes_name_idx ON graph_nodes(lower(name))",
            """
            CREATE TABLE IF NOT EXISTS graph_edges (
              edge_id TEXT PRIMARY KEY,
              source_id TEXT NOT NULL,
              target_id TEXT NOT NULL,
              doc_id TEXT NOT NULL,
              relation TEXT NOT NULL DEFAULT 'RELATED_TO',
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS graph_edges_doc_idx ON graph_edges(doc_id)",
            "CREATE INDEX IF NOT EXISTS graph_edges_source_target_idx ON graph_edges(source_id, target_id)",
            """
            CREATE TABLE IF NOT EXISTS graph_chunks (
              chunk_id TEXT PRIMARY KEY,
              doc_id TEXT NOT NULL,
              payload JSONB NOT NULL DEFAULT '{}'::jsonb,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            "CREATE INDEX IF NOT EXISTS graph_chunks_doc_idx ON graph_chunks(doc_id)",
        ]
        with self._connect() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)
            conn.commit()

    def health(self) -> dict:
        if not self.database_url:
            return {"status": "error", **self.profile()}
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            return {"status": "ok", **self.profile()}
        except Exception:
            return {"status": "error", **self.profile()}

    def _refresh_cache(self) -> None:
        if time.monotonic() < self._cache_expires_at:
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM graph_nodes ORDER BY source_doc, node_id")
                node_rows = cur.fetchall()
                cur.execute("SELECT payload FROM graph_edges ORDER BY doc_id, edge_id")
                edge_rows = cur.fetchall()
        self._cached_nodes = [dict(row["payload"]) for row in node_rows if row.get("payload")]
        self._cached_edges = [dict(row["payload"]) for row in edge_rows if row.get("payload")]
        self._cache_expires_at = time.monotonic() + 30.0

    def _load_nodes(self) -> list[dict]:
        self._refresh_cache()
        return [dict(item) for item in self._cached_nodes]

    def _load_edges(self) -> list[dict]:
        self._refresh_cache()
        return [dict(item) for item in self._cached_edges]

    def _lexical_chunks(
        self,
        q: str,
        *,
        limit: int,
        allowed_document_ids: set[str] | None,
    ) -> list[dict]:
        """Retrieve a small, tenant-scoped candidate set and rank it in Python.

        The current Postgres schema stores page text in JSONB but has no vector
        column.  We deliberately avoid loading the full chunk table: SQL first
        applies the authorized document scope and a lexical prefilter, then the
        bounded rows are ranked using English tokens and CJK bigrams.
        """

        terms = _lexical_terms(q)
        if not terms or limit <= 0 or allowed_document_ids == set():
            return []

        candidate_limit = min(max(limit * 32, 64), 256)
        required_matches = 1 if len(terms) == 1 else 2
        ascii_flags = [bool(_ASCII_TERM_RE.fullmatch(term)) for term in terms]
        scoped_where = ""
        params: list[Any] = []
        if allowed_document_ids is not None:
            scoped_where = "WHERE doc_id = ANY(%s)"
            params.append(sorted(allowed_document_ids))
        params.extend([
            terms,
            ascii_flags,
            required_matches,
            _LEXICAL_CHUNK_CONTEXT_BEFORE,
            _LEXICAL_CHUNK_TEXT_LIMIT,
            candidate_limit,
        ])

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    WITH scoped AS (
                      SELECT
                        chunk_id,
                        doc_id,
                        payload,
                        updated_at,
                        coalesce(payload->>'text', payload->>'content', '') AS full_text,
                        lower(coalesce(payload->>'text', payload->>'content', '')) AS lexical_text
                      FROM graph_chunks
                      {scoped_where}
                    ), matched AS (
                      SELECT
                        scoped.*,
                        lexical.match_position,
                        lexical.match_count
                      FROM scoped
                      CROSS JOIN LATERAL (
                        SELECT
                          min(nullif(position(wanted.term IN scoped.lexical_text), 0)) AS match_position,
                          count(*) AS match_count
                        FROM unnest(%s::text[], %s::boolean[]) AS wanted(term, is_ascii)
                        WHERE CASE
                          WHEN wanted.is_ascii THEN scoped.lexical_text ~
                            ('(^|[^a-z0-9])' || wanted.term || '([^a-z0-9]|$)')
                          ELSE position(wanted.term IN scoped.lexical_text) > 0
                        END
                      ) AS lexical
                      WHERE lexical.match_count >= %s
                    )
                    SELECT
                      scoped.chunk_id,
                      scoped.doc_id,
                      scoped.payload - 'text' - 'content' AS payload,
                      substring(
                        scoped.full_text
                        FROM greatest(1, scoped.match_position - %s)
                        FOR %s
                      ) AS text
                    FROM matched AS scoped
                    ORDER BY scoped.updated_at DESC, scoped.chunk_id
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()

        scored: list[dict] = []
        for row in rows:
            doc_id = str(row.get("doc_id") or "")
            # Defense in depth for test doubles and future query changes.
            if allowed_document_ids is not None and doc_id not in allowed_document_ids:
                continue
            payload = dict(row.get("payload") or {})
            payload["chunk_id"] = str(row.get("chunk_id") or payload.get("chunk_id") or "")
            # The relational columns are authoritative for scope and identity;
            # never trust duplicated JSONB fields over the filtered row.
            payload["doc_id"] = doc_id
            # SQL projects only a bounded window. Slice once more here as
            # defense in depth for test doubles and future query changes.
            text = str(row.get("text") or "")[:_LEXICAL_CHUNK_TEXT_LIMIT]
            payload["text"] = text
            score = _lexical_chunk_score(q, text)
            if score <= 0:
                continue
            payload["score"] = score
            scored.append(payload)
        scored.sort(key=lambda item: (-float(item.get("score") or 0), str(item.get("chunk_id") or "")))
        return scored[:limit]

    def hybrid_retrieve(self, q: str, embedding: list[float] | None = None,
                        limit: int = 8, include_neighbors: bool = True,
                        allowed_document_ids: set[str] | None = None) -> dict:
        graph_result = self.search_graph(q, include_neighbors=include_neighbors)
        nodes = list(graph_result.get("matched_nodes", []))
        edges = list(graph_result.get("subgraph_edges", []))
        if allowed_document_ids is not None:
            nodes = [
                node for node in nodes
                if str(node.get("source_doc") or node.get("doc_id") or "") in allowed_document_ids
            ]
            edges = [
                edge for edge in edges
                if str(edge.get("doc_id") or "") in allowed_document_ids
            ]
        return {
            "query": q,
            "nodes": nodes[:limit],
            "edges": edges[: max(limit * 4, 20)],
            "chunks": self._lexical_chunks(
                q,
                limit=limit,
                allowed_document_ids=allowed_document_ids,
            ),
        }

    def upsert_document_graph(self, document: dict, nodes: list[dict], edges: list[dict], chunks: list[dict] | None = None) -> None:
        self.ensure_schema()
        doc_id = str(document["doc_id"])
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM graph_chunks WHERE doc_id = %s", (doc_id,))
                cur.execute("DELETE FROM graph_edges WHERE doc_id = %s", (doc_id,))
                cur.execute("DELETE FROM graph_nodes WHERE source_doc = %s", (doc_id,))
                cur.execute(
                    """
                    INSERT INTO graph_documents (doc_id, payload, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (doc_id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
                    """,
                    (doc_id, self._jsonb({**document, "doc_id": doc_id})),
                )
                node_rows = []
                for node in nodes:
                    node_id = _node_id(node.get("id") or node.get("entity_id"))
                    payload = {**node, "id": node_id, "source_doc": node.get("source_doc") or doc_id}
                    node_rows.append({
                        "node_id": node_id,
                        "source_doc": payload["source_doc"],
                        "node_type": payload.get("type", ""),
                        "name": payload.get("name", ""),
                        "payload": payload,
                    })
                if node_rows:
                    cur.execute(
                        """
                        WITH rows AS (
                          SELECT * FROM jsonb_to_recordset(%s)
                          AS item(node_id TEXT, source_doc TEXT, node_type TEXT, name TEXT, payload JSONB)
                        )
                        INSERT INTO graph_nodes (node_id, source_doc, node_type, name, payload, updated_at)
                        SELECT node_id, source_doc, node_type, name, payload, now() FROM rows
                        ON CONFLICT (node_id, source_doc) DO UPDATE SET
                          node_type = EXCLUDED.node_type,
                          name = EXCLUDED.name,
                          payload = EXCLUDED.payload,
                          updated_at = now()
                        """,
                        (self._jsonb(node_rows),),
                    )
                edge_rows = []
                for edge in edges:
                    payload = {**edge, "doc_id": edge.get("doc_id") or doc_id}
                    payload["id"] = _edge_id(payload, payload["doc_id"])
                    edge_rows.append({
                        "edge_id": payload["id"],
                        "source_id": payload["source"],
                        "target_id": payload["target"],
                        "doc_id": payload["doc_id"],
                        "relation": payload.get("relation", "RELATED_TO"),
                        "payload": payload,
                    })
                if edge_rows:
                    cur.execute(
                        """
                        WITH rows AS (
                          SELECT * FROM jsonb_to_recordset(%s)
                          AS item(edge_id TEXT, source_id TEXT, target_id TEXT, doc_id TEXT, relation TEXT, payload JSONB)
                        )
                        INSERT INTO graph_edges (edge_id, source_id, target_id, doc_id, relation, payload, updated_at)
                        SELECT edge_id, source_id, target_id, doc_id, relation, payload, now() FROM rows
                        ON CONFLICT (edge_id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
                        """,
                        (self._jsonb(edge_rows),),
                    )
                chunk_rows = []
                for chunk in chunks or []:
                    chunk_id = _node_id(chunk.get("chunk_id") or f"{doc_id}:page:{chunk.get('page', 0)}")
                    payload = {**chunk, "chunk_id": chunk_id, "doc_id": chunk.get("doc_id") or doc_id}
                    chunk_rows.append({"chunk_id": chunk_id, "doc_id": payload["doc_id"], "payload": payload})
                if chunk_rows:
                    cur.execute(
                        """
                        WITH rows AS (
                          SELECT * FROM jsonb_to_recordset(%s)
                          AS item(chunk_id TEXT, doc_id TEXT, payload JSONB)
                        )
                        INSERT INTO graph_chunks (chunk_id, doc_id, payload, updated_at)
                        SELECT chunk_id, doc_id, payload, now() FROM rows
                        ON CONFLICT (chunk_id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
                        """,
                        (self._jsonb(chunk_rows),),
                    )
            conn.commit()
        self._cache_expires_at = 0.0

    def remove_document(self, doc_id: str) -> tuple[int, int]:
        self.ensure_schema()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS count FROM graph_nodes WHERE source_doc = %s", (doc_id,))
                removed_nodes = int(cur.fetchone()["count"])
                cur.execute("SELECT count(*) AS count FROM graph_edges WHERE doc_id = %s", (doc_id,))
                removed_edges = int(cur.fetchone()["count"])
                cur.execute("DELETE FROM graph_chunks WHERE doc_id = %s", (doc_id,))
                cur.execute("DELETE FROM graph_edges WHERE doc_id = %s", (doc_id,))
                cur.execute("DELETE FROM graph_nodes WHERE source_doc = %s", (doc_id,))
                cur.execute("DELETE FROM graph_documents WHERE doc_id = %s", (doc_id,))
            conn.commit()
        self._cache_expires_at = 0.0
        return removed_nodes, removed_edges


class Neo4jGraphRepository:
    def __init__(self):
        self.uri = os.getenv("NEO4J_URI", "").strip()
        self.username = os.getenv("NEO4J_USERNAME", os.getenv("NEO4J_USER", "neo4j")).strip()
        self.password = os.getenv("NEO4J_PASSWORD", "").strip()
        self.database = os.getenv("NEO4J_DATABASE", "neo4j").strip() or "neo4j"
        self.vector_dimensions = int(os.getenv("NEO4J_VECTOR_DIMENSIONS", "1024"))
        if not self.uri or not self.password:
            raise ValueError("NEO4J_URI and NEO4J_PASSWORD are required when GRAPHRAG_GRAPH_BACKEND=neo4j")
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise RuntimeError("Install neo4j>=5.23.0 to use GRAPHRAG_GRAPH_BACKEND=neo4j") from exc
        self.driver = GraphDatabase.driver(self.uri, auth=(self.username, self.password))

    def profile(self) -> dict:
        return {
            "backend": "neo4j",
            "uri_configured": bool(self.uri),
            "database": self.database,
            "vector_dimensions": self.vector_dimensions,
        }

    def health(self) -> dict:
        try:
            self.driver.verify_connectivity()
            return {"status": "ok", **self.profile()}
        except Exception as exc:
            return {"status": "error", **self.profile(), "error": str(exc)}

    def _execute(self, query: str, parameters: dict | None = None) -> list:
        records, _summary, _keys = self.driver.execute_query(
            query,
            parameters or {},
            database_=self.database,
        )
        return list(records)

    def _record_data(self, record: Any) -> dict:
        if hasattr(record, "data"):
            return record.data()
        if isinstance(record, dict):
            return record
        return dict(record)

    def _record_value(self, record: Any, key: str, default: Any = None) -> Any:
        data = self._record_data(record)
        return data.get(key, default)

    def _plain_node(self, value: Any) -> dict:
        if isinstance(value, dict):
            node = dict(value)
        else:
            node = dict(value)
        node["id"] = _node_id(node.get("id") or node.get("entity_id"))
        node["source_doc"] = node.get("source_doc") or node.get("doc_id") or ""
        node["confidence"] = node.get("confidence") or "match_exact"
        node["page"] = int(node.get("page") or 0)
        node["degree"] = int(node.get("degree") or 0)
        return node

    def ensure_schema(self) -> None:
        for query in [
            "CREATE CONSTRAINT graph_document_id IF NOT EXISTS FOR (d:Document) REQUIRE d.doc_id IS UNIQUE",
            "CREATE CONSTRAINT graph_entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
            "CREATE CONSTRAINT graph_chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
            "CREATE INDEX graph_entity_doc IF NOT EXISTS FOR (e:Entity) ON (e.source_doc)",
            "CREATE INDEX graph_entity_type IF NOT EXISTS FOR (e:Entity) ON (e.type)",
            "CREATE INDEX graph_document_owner IF NOT EXISTS FOR (d:Document) ON (d.owner_id)",
            "CREATE FULLTEXT INDEX graph_entity_name_fulltext IF NOT EXISTS FOR (e:Entity) ON EACH [e.name]",
            (
                "CREATE VECTOR INDEX graph_chunk_embedding_vector IF NOT EXISTS "
                "FOR (c:Chunk) ON (c.embedding) "
                f"OPTIONS {{indexConfig: {{`vector.dimensions`: {self.vector_dimensions}, `vector.similarity_function`: 'cosine'}}}}"
            ),
            (
                "CREATE VECTOR INDEX graph_entity_embedding_vector IF NOT EXISTS "
                "FOR (e:Entity) ON (e.embedding) "
                f"OPTIONS {{indexConfig: {{`vector.dimensions`: {self.vector_dimensions}, `vector.similarity_function`: 'cosine'}}}}"
            ),
        ]:
            self._execute(query)

    def get_nodes(self, page: int = 1, page_size: int = 50,
                  node_type: str | None = None,
                  doc_id: str | None = None,
                  confidence: str | None = None) -> dict:
        filters = []
        params: dict[str, Any] = {"skip": (max(page, 1) - 1) * page_size, "limit": page_size}
        if node_type:
            filters.append("toUpper(e.type) = toUpper($node_type)")
            params["node_type"] = node_type
        if doc_id:
            filters.append("e.source_doc = $doc_id")
            params["doc_id"] = doc_id
        if confidence:
            filters.append("e.confidence = $confidence")
            params["confidence"] = confidence
        where = "WHERE " + " AND ".join(filters) if filters else ""

        count_records = self._execute(f"MATCH (e:Entity) {where} RETURN count(e) AS total", params)
        total = int(self._record_value(count_records[0], "total", 0)) if count_records else 0
        records = self._execute(
            f"""
            MATCH (e:Entity)
            {where}
            OPTIONAL MATCH (e)--(n:Entity)
            WITH e, count(DISTINCT n) AS degree
            ORDER BY coalesce(e.name, "")
            SKIP $skip LIMIT $limit
            RETURN e {{.*, id: e.id, source_doc: coalesce(e.source_doc, ""), degree: degree}} AS node
            """,
            params,
        )
        return {"total": total, "page": page, "page_size": page_size, "items": [self._plain_node(self._record_value(record, "node", {})) for record in records]}

    def get_edges(self, page: int = 1, page_size: int = 100,
                  doc_id: str | None = None,
                  relation: str | None = None) -> dict:
        filters = []
        params: dict[str, Any] = {"skip": (max(page, 1) - 1) * page_size, "limit": page_size}
        if doc_id:
            filters.append("r.doc_id = $doc_id")
            params["doc_id"] = doc_id
        if relation:
            filters.append("r.relation = $relation")
            params["relation"] = relation
        where = "WHERE " + " AND ".join(filters) if filters else ""
        count_records = self._execute(f"MATCH (:Entity)-[r:RELATED_TO]->(:Entity) {where} RETURN count(r) AS total", params)
        total = int(self._record_value(count_records[0], "total", 0)) if count_records else 0
        records = self._execute(
            f"""
            MATCH (s:Entity)-[r:RELATED_TO]->(t:Entity)
            {where}
            RETURN {{
              id: coalesce(r.id, elementId(r)),
              source: s.id,
              target: t.id,
              relation: coalesce(r.relation, "RELATED_TO"),
              doc_id: coalesce(r.doc_id, ""),
              page: coalesce(r.page, 0)
            }} AS edge
            SKIP $skip LIMIT $limit
            """,
            params,
        )
        return {"total": total, "page": page, "page_size": page_size, "items": [self._record_value(record, "edge", {}) for record in records]}

    def get_node_detail(self, node_id: str) -> dict | None:
        records = self._execute(
            """
            MATCH (e:Entity {id: $node_id})
            OPTIONAL MATCH (e)--(n:Entity)
            WITH e, count(DISTINCT n) AS degree
            MATCH (all:Entity)
            WITH e, degree, count(all) AS total_nodes
            RETURN e {.*, id: e.id, source_doc: coalesce(e.source_doc, ""), degree: degree,
              neighbor_count: degree,
              degree_centrality: CASE WHEN total_nodes > 1 THEN toFloat(degree) / toFloat(total_nodes - 1) ELSE 0.0 END
            } AS node
            """,
            {"node_id": node_id},
        )
        if not records:
            return None
        return self._plain_node(self._record_value(records[0], "node", {}))

    def get_neighbors(self, node_id: str, hops: int = 1) -> dict | None:
        hops = max(1, min(hops, 3))
        center = self.get_node_detail(node_id)
        if not center:
            return None
        records = self._execute(
            f"""
            MATCH path=(center:Entity {{id: $node_id}})-[:RELATED_TO*1..{hops}]-(neighbor:Entity)
            WITH neighbor, min(length(path)) AS distance
            RETURN neighbor {{.*, id: neighbor.id, source_doc: coalesce(neighbor.source_doc, "")}} AS node, distance
            ORDER BY distance, node.name
            """,
            {"node_id": node_id},
        )
        by_hop: dict[str, list] = {}
        for record in records:
            distance = str(self._record_value(record, "distance", 1))
            node = self._plain_node(self._record_value(record, "node", {}))
            by_hop.setdefault(distance, []).append({
                "id": node["id"],
                "name": node.get("name", ""),
                "type": node.get("type", ""),
                "page": node.get("page", 0),
            })
        return {
            "center": {"id": center["id"], "name": center.get("name", ""), "type": center.get("type", ""), "page": center.get("page", 0)},
            "hops": hops,
            "neighbors_by_hop": by_hop,
            "total_neighbors": sum(len(items) for items in by_hop.values()),
        }

    def get_stats(self) -> dict:
        totals = self._execute(
            """
            MATCH (e:Entity)
            WITH count(e) AS total_nodes
            OPTIONAL MATCH ()-[r:RELATED_TO]->()
            RETURN total_nodes, count(r) AS total_edges
            """
        )
        total_nodes = int(self._record_value(totals[0], "total_nodes", 0)) if totals else 0
        total_edges = int(self._record_value(totals[0], "total_edges", 0)) if totals else 0
        type_records = self._execute("MATCH (e:Entity) RETURN coalesce(e.type, 'UNKNOWN') AS type, count(e) AS count")
        relation_records = self._execute("MATCH ()-[r:RELATED_TO]->() RETURN coalesce(r.relation, 'RELATED_TO') AS relation, count(r) AS count")
        top_records = self._execute(
            """
            MATCH (e:Entity)
            OPTIONAL MATCH (e)--(n:Entity)
            WITH e, count(DISTINCT n) AS degree
            ORDER BY degree DESC LIMIT 5
            RETURN e.id AS node_id, e.name AS name, e.type AS type, degree
            """
        )
        doc_records = self._execute("MATCH (e:Entity) WHERE e.source_doc IS NOT NULL RETURN DISTINCT e.source_doc AS doc_id")
        denominator = max(total_nodes - 1, 1)
        return {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "density": 0.0,
            "type_distribution": {self._record_value(record, "type", "UNKNOWN"): int(self._record_value(record, "count", 0)) for record in type_records},
            "relation_types": {self._record_value(record, "relation", "RELATED_TO"): int(self._record_value(record, "count", 0)) for record in relation_records},
            "top5_central_nodes": [
                {
                    "node_id": self._record_value(record, "node_id", ""),
                    "name": self._record_value(record, "name", ""),
                    "type": self._record_value(record, "type", ""),
                    "centrality": round(int(self._record_value(record, "degree", 0)) / denominator, 4),
                }
                for record in top_records
            ],
            "source_documents": [self._record_value(record, "doc_id", "") for record in doc_records],
        }

    def export_kg(self, doc_id: str | None = None) -> dict:
        from datetime import datetime, timezone

        nodes = self.get_nodes(1, 100_000, doc_id=doc_id)["items"]
        edges = self.get_edges(1, 500_000, doc_id=doc_id)["items"]
        return {
            "format": "json",
            "doc_id": doc_id,
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "nodes": nodes,
            "edges": edges,
        }

    def search_entities(self, q: str, entity_type: str | None = None, limit: int = 15) -> dict:
        filters = ["toLower(e.name) CONTAINS toLower($q)"]
        params: dict[str, Any] = {"q": q, "limit": limit}
        if entity_type:
            filters.append("toUpper(e.type) = toUpper($entity_type)")
            params["entity_type"] = entity_type
        records = self._execute(
            f"""
            MATCH (e:Entity)
            WHERE {" AND ".join(filters)}
            OPTIONAL MATCH (e)--(n:Entity)
            WITH e, count(DISTINCT n) AS degree
            RETURN e {{.*, id: e.id, source_doc: coalesce(e.source_doc, ""), degree: degree}} AS node
            ORDER BY degree DESC, e.name
            LIMIT $limit
            """,
            params,
        )
        items = [self._plain_node(self._record_value(record, "node", {})) for record in records]
        return {"query": q, "total": len(items), "items": items}

    def search_path(self, from_id: str, to_id: str, max_hops: int = 3) -> dict | None:
        max_hops = max(1, min(max_hops, 5))
        from_node = self.get_node_detail(from_id)
        to_node = self.get_node_detail(to_id)
        if not from_node or not to_node:
            return None
        records = self._execute(
            f"""
            MATCH p = shortestPath((from:Entity {{id: $from_id}})-[:RELATED_TO*..{max_hops}]-(to:Entity {{id: $to_id}}))
            RETURN [node IN nodes(p) | node {{.*, id: node.id}}] AS nodes,
                   [rel IN relationships(p) | {{
                     source: startNode(rel).id,
                     target: endNode(rel).id,
                     relation: coalesce(rel.relation, "RELATED_TO")
                   }}] AS edges
            LIMIT 5
            """,
            {"from_id": from_id, "to_id": to_id},
        )
        paths = []
        for record in records:
            path_nodes = [self._plain_node(node) for node in self._record_value(record, "nodes", [])]
            path_edges = self._record_value(record, "edges", [])
            paths.append({
                "length": max(len(path_nodes) - 1, 0),
                "nodes": [{"id": node["id"], "name": node.get("name", ""), "type": node.get("type", "")} for node in path_nodes],
                "edges": path_edges,
            })
        return {
            "from": {"id": from_id, "name": from_node.get("name", ""), "type": from_node.get("type", "")},
            "to": {"id": to_id, "name": to_node.get("name", ""), "type": to_node.get("type", "")},
            "max_hops": max_hops,
            "paths": paths,
            "total_paths": len(paths),
        }

    def search_graph(self, q: str, include_neighbors: bool = False) -> dict:
        matched = self.search_entities(q, None, 50)["items"]
        if not include_neighbors:
            return {"query": q, "matched_nodes": matched, "subgraph_edges": []}
        matched_ids = [node["id"] for node in matched]
        records = self._execute(
            """
            MATCH (s:Entity)-[r:RELATED_TO]-(t:Entity)
            WHERE s.id IN $matched_ids OR t.id IN $matched_ids
            RETURN DISTINCT {
              id: coalesce(r.id, elementId(r)),
              source: s.id,
              target: t.id,
              relation: coalesce(r.relation, "RELATED_TO"),
              doc_id: coalesce(r.doc_id, ""),
              page: coalesce(r.page, 0)
            } AS edge
            LIMIT 500
            """,
            {"matched_ids": matched_ids},
        )
        return {"query": q, "matched_nodes": matched, "subgraph_edges": [self._record_value(record, "edge", {}) for record in records]}

    def hybrid_retrieve(self, q: str, embedding: list[float] | None = None,
                        limit: int = 8, include_neighbors: bool = True,
                        allowed_document_ids: set[str] | None = None) -> dict:
        allowed_ids = sorted(allowed_document_ids) if allowed_document_ids is not None else None
        chunks: list[dict] = []
        if embedding:
            chunk_records = self._execute(
                """
                CALL db.index.vector.queryNodes('graph_chunk_embedding_vector', $limit, $embedding)
                YIELD node, score
                WHERE $allowed_document_ids IS NULL OR coalesce(node.doc_id, '') IN $allowed_document_ids
                RETURN node {
                  .*,
                  chunk_id: node.chunk_id,
                  doc_id: coalesce(node.doc_id, ""),
                  page: coalesce(node.page, 0),
                  score: score
                } AS chunk
                """,
                {"embedding": embedding, "limit": limit, "allowed_document_ids": allowed_ids},
            )
            chunks = [self._record_value(record, "chunk", {}) for record in chunk_records]

        node_records = self._execute(
            """
            CALL db.index.fulltext.queryNodes('graph_entity_name_fulltext', $q)
            YIELD node, score
            WHERE $allowed_document_ids IS NULL OR coalesce(node.source_doc, '') IN $allowed_document_ids
            RETURN node {
              .*,
              id: node.id,
              source_doc: coalesce(node.source_doc, ""),
              score: score
            } AS node
            LIMIT $limit
            """,
            {"q": q, "limit": limit, "allowed_document_ids": allowed_ids},
        )
        nodes = [self._plain_node(self._record_value(record, "node", {})) for record in node_records]
        matched_ids = [node["id"] for node in nodes]

        edges: list[dict] = []
        if include_neighbors and matched_ids:
            edge_records = self._execute(
                """
                MATCH (s:Entity)-[r:RELATED_TO]-(t:Entity)
                WHERE (s.id IN $matched_ids OR t.id IN $matched_ids)
                  AND ($allowed_document_ids IS NULL OR coalesce(r.doc_id, '') IN $allowed_document_ids)
                RETURN DISTINCT {
                  id: coalesce(r.id, elementId(r)),
                  source: s.id,
                  target: t.id,
                  relation: coalesce(r.relation, "RELATED_TO"),
                  doc_id: coalesce(r.doc_id, ""),
                  page: coalesce(r.page, 0)
                } AS edge
                LIMIT 500
                """,
                {"matched_ids": matched_ids, "allowed_document_ids": allowed_ids},
            )
            edges = [self._record_value(record, "edge", {}) for record in edge_records]

        return {"query": q, "nodes": nodes, "edges": edges, "chunks": chunks}

    def upsert_document_graph(self, document: dict, nodes: list[dict], edges: list[dict], chunks: list[dict] | None = None) -> None:
        self.ensure_schema()
        document_props = _clean_props({
            "doc_id": document["doc_id"],
            "owner_id": document.get("owner_id", "default"),
            "filename": document.get("filename", ""),
            "status": document.get("status", "indexed"),
        })
        self._execute(
            "MERGE (d:Document {doc_id: $doc_id}) SET d += $properties, d.updated_at = datetime()",
            {"doc_id": document["doc_id"], "properties": document_props},
        )

        for node in nodes:
            node_id = _node_id(node.get("id") or node.get("entity_id"))
            source_doc = node.get("source_doc") or document["doc_id"]
            properties = _clean_props({**node, "id": node_id, "entity_id": node_id, "source_doc": source_doc})
            self._execute(
                """
                MERGE (e:Entity {id: $id})
                SET e += $properties, e.updated_at = datetime()
                WITH e
                MATCH (d:Document {doc_id: $source_doc})
                MERGE (d)-[:MENTIONS_ENTITY]->(e)
                """,
                {"id": node_id, "source_doc": source_doc, "properties": properties},
            )

        for chunk in chunks or []:
            chunk_id = _node_id(chunk.get("chunk_id") or f"{document['doc_id']}:page:{chunk.get('page', 0)}")
            doc_id = chunk.get("doc_id") or document["doc_id"]
            entity_ids = list(chunk.get("entity_ids") or [])
            properties = _clean_props({**chunk, "chunk_id": chunk_id, "doc_id": doc_id})
            properties.pop("entity_ids", None)
            self._execute(
                """
                MERGE (c:Chunk {chunk_id: $chunk_id})
                SET c += $properties, c.updated_at = datetime()
                WITH c
                MATCH (d:Document {doc_id: $doc_id})
                MERGE (d)-[:HAS_CHUNK]->(c)
                """,
                {"chunk_id": chunk_id, "doc_id": doc_id, "properties": properties},
            )
            for entity_id in entity_ids:
                self._execute(
                    """
                    MATCH (c:Chunk {chunk_id: $chunk_id})
                    MATCH (e:Entity {id: $entity_id})
                    MERGE (c)-[:MENTIONS]->(e)
                    """,
                    {"chunk_id": chunk_id, "entity_id": entity_id},
                )

        for edge in edges:
            relation = edge.get("relation", "RELATED_TO")
            doc_id = edge.get("doc_id") or document["doc_id"]
            properties = _clean_props({**edge, "relation": relation, "doc_id": doc_id})
            self._execute(
                """
                MATCH (s:Entity {id: $source})
                MATCH (t:Entity {id: $target})
                MERGE (s)-[r:RELATED_TO]->(t)
                SET r += $properties, r.updated_at = datetime()
                """,
                {
                    "source": edge["source"],
                    "target": edge["target"],
                    "relation": relation,
                    "doc_id": doc_id,
                    "properties": properties,
                },
            )

    def remove_document(self, doc_id: str) -> tuple[int, int]:
        count_records = self._execute(
            """
            MATCH (d:Document {doc_id: $doc_id})
            OPTIONAL MATCH (d)-[:MENTIONS_ENTITY]->(e:Entity)
            RETURN count(DISTINCT e) AS entity_count
            """,
            {"doc_id": doc_id},
        )
        removed_nodes = int(self._record_value(count_records[0], "entity_count", 0)) if count_records else 0
        self._execute(
            """
            MATCH (d:Document {doc_id: $doc_id})
            OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Chunk)
            DETACH DELETE c
            """,
            {"doc_id": doc_id},
        )
        self._execute("MATCH (d:Document {doc_id: $doc_id}) DETACH DELETE d", {"doc_id": doc_id})
        self._execute(
            """
            MATCH (e:Entity {source_doc: $doc_id})
            WHERE NOT (e)<-[:MENTIONS_ENTITY]-(:Document)
            DETACH DELETE e
            """,
            {"doc_id": doc_id},
        )
        return removed_nodes, 0


_CACHE_KEY: tuple[str, ...] | None = None
_CACHE_REPO: FileGraphRepository | PostgresGraphRepository | Neo4jGraphRepository | None = None


def get_graph_repository() -> FileGraphRepository | PostgresGraphRepository | Neo4jGraphRepository:
    global _CACHE_KEY, _CACHE_REPO
    backend = os.getenv("GRAPHRAG_GRAPH_BACKEND", os.getenv("GRAPHRAG_STORAGE_BACKEND", "filesystem")).strip().lower()
    if backend in {"json", "file", "local"}:
        backend = "filesystem"
    key = (
        backend,
        os.getenv("NEO4J_URI", ""),
        os.getenv("NEO4J_USERNAME", os.getenv("NEO4J_USER", "neo4j")),
        os.getenv("NEO4J_PASSWORD", ""),
        os.getenv("NEO4J_DATABASE", "neo4j"),
        os.getenv("NEO4J_VECTOR_DIMENSIONS", "1024"),
        os.getenv("DATABASE_URL", os.getenv("POSTGRES_URL", "")),
    )
    if _CACHE_REPO is not None and _CACHE_KEY == key:
        return _CACHE_REPO
    _CACHE_KEY = key
    if backend == "neo4j":
        _CACHE_REPO = Neo4jGraphRepository()
    elif backend in {"postgres", "postgresql", "neon"}:
        _CACHE_REPO = PostgresGraphRepository()
    else:
        _CACHE_REPO = FileGraphRepository()
    return _CACHE_REPO


def reset_graph_repository_cache() -> None:
    global _CACHE_KEY, _CACHE_REPO
    if _CACHE_REPO and hasattr(_CACHE_REPO, "driver"):
        try:
            _CACHE_REPO.driver.close()
        except Exception:
            pass
    _CACHE_KEY = None
    _CACHE_REPO = None
