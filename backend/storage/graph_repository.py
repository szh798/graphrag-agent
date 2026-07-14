"""Graph repository backends.

Production can use Neo4j AuraDB as the graph/vector store while local demos keep
the existing JSON filesystem behavior.
"""
from __future__ import annotations

import os
from typing import Any

import networkx as nx

from storage import file_store as fs


def _node_id(value: Any) -> str:
    return str(value or "")


def _clean_props(data: dict) -> dict:
    return {k: v for k, v in data.items() if v is not None}


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

    def _load_graph(self) -> nx.Graph:
        nodes = fs.load_kg_nodes()
        edges = fs.load_kg_edges()
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
        nodes = [dict(item) for item in fs.load_kg_nodes()]
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
        edges = [dict(item) for item in fs.load_kg_edges()]
        if doc_id:
            edges = [edge for edge in edges if edge.get("doc_id") == doc_id]
        if relation:
            edges = [edge for edge in edges if edge.get("relation") == relation]
        total = len(edges)
        start = (max(page, 1) - 1) * page_size
        return {"total": total, "page": page, "page_size": page_size, "items": edges[start:start + page_size]}

    def get_node_detail(self, node_id: str) -> dict | None:
        nodes = [dict(item) for item in fs.load_kg_nodes()]
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
        nodes = [dict(item) for item in fs.load_kg_nodes()]
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
        nodes = fs.load_kg_nodes()
        edges = fs.load_kg_edges()
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

        nodes = [dict(item) for item in fs.load_kg_nodes()]
        edges = [dict(item) for item in fs.load_kg_edges()]
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
        nodes = [dict(item) for item in fs.load_kg_nodes()]
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
        nodes = [dict(item) for item in fs.load_kg_nodes()]
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
        nodes = [dict(item) for item in fs.load_kg_nodes()]
        edges = [dict(item) for item in fs.load_kg_edges()]
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
                        limit: int = 8, include_neighbors: bool = True) -> dict:
        graph_result = self.search_graph(q, include_neighbors=include_neighbors)
        return {
            "query": q,
            "nodes": graph_result.get("matched_nodes", [])[:limit],
            "edges": graph_result.get("subgraph_edges", [])[: max(limit * 4, 20)],
            "chunks": [],
        }

    def upsert_document_graph(self, document: dict, nodes: list[dict], edges: list[dict], chunks: list[dict] | None = None) -> None:
        fs.merge_kg(nodes, edges, document["doc_id"])

    def remove_document(self, doc_id: str) -> tuple[int, int]:
        return fs.remove_doc_from_kg(doc_id)


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
                        limit: int = 8, include_neighbors: bool = True) -> dict:
        chunks: list[dict] = []
        if embedding:
            chunk_records = self._execute(
                """
                CALL db.index.vector.queryNodes('graph_chunk_embedding_vector', $limit, $embedding)
                YIELD node, score
                RETURN node {
                  .*,
                  chunk_id: node.chunk_id,
                  doc_id: coalesce(node.doc_id, ""),
                  page: coalesce(node.page, 0),
                  score: score
                } AS chunk
                """,
                {"embedding": embedding, "limit": limit},
            )
            chunks = [self._record_value(record, "chunk", {}) for record in chunk_records]

        node_records = self._execute(
            """
            CALL db.index.fulltext.queryNodes('graph_entity_name_fulltext', $q)
            YIELD node, score
            RETURN node {
              .*,
              id: node.id,
              source_doc: coalesce(node.source_doc, ""),
              score: score
            } AS node
            LIMIT $limit
            """,
            {"q": q, "limit": limit},
        )
        nodes = [self._plain_node(self._record_value(record, "node", {})) for record in node_records]
        matched_ids = [node["id"] for node in nodes]

        edges: list[dict] = []
        if include_neighbors and matched_ids:
            edge_records = self._execute(
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


_CACHE_KEY: tuple[str, str, str, str, str, str] | None = None
_CACHE_REPO: FileGraphRepository | Neo4jGraphRepository | None = None


def get_graph_repository() -> FileGraphRepository | Neo4jGraphRepository:
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
    )
    if _CACHE_REPO is not None and _CACHE_KEY == key:
        return _CACHE_REPO
    _CACHE_KEY = key
    _CACHE_REPO = Neo4jGraphRepository() if backend == "neo4j" else FileGraphRepository()
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
