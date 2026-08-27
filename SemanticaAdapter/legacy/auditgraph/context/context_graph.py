"""A compact graph-native context store modeled after Semantica's ContextGraph."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str) -> datetime:
    timestamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    return timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp


@dataclass(slots=True)
class GraphNode:
    node_id: str
    node_type: str
    properties: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utc_now)
    invalidated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "type": self.node_type,
            "properties": dict(self.properties),
            "created_at": self.created_at.isoformat(),
            "invalidated_at": self.invalidated_at.isoformat() if self.invalidated_at else None,
        }


@dataclass(slots=True)
class GraphEdge:
    source: str
    target: str
    edge_type: str
    properties: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utc_now)
    invalidated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "type": self.edge_type,
            "properties": dict(self.properties),
            "created_at": self.created_at.isoformat(),
            "invalidated_at": self.invalidated_at.isoformat() if self.invalidated_at else None,
        }


class ContextGraph:
    CAUSAL_TYPES = {"CAUSED", "INFLUENCED", "PRECEDENT_FOR"}

    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []

    def add_node(self, node_id: str, node_type: str, **properties: Any) -> str:
        if not node_id or not node_type:
            raise ValueError("node_id and node_type are required")
        existing = self.nodes.get(node_id)
        if existing:
            if existing.node_type != node_type:
                raise ValueError(f"node {node_id!r} already exists with type {existing.node_type!r}")
            existing.properties.update(properties)
        else:
            self.nodes[node_id] = GraphNode(node_id, node_type, dict(properties))
        return node_id

    def add_edge(self, source: str, target: str, edge_type: str, **properties: Any) -> GraphEdge:
        if source not in self.nodes or target not in self.nodes:
            raise KeyError("both edge endpoints must exist")
        if not edge_type:
            raise ValueError("edge_type is required")
        edge = GraphEdge(source, target, edge_type, dict(properties))
        self.edges.append(edge)
        return edge

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        node = self.nodes.get(node_id)
        return node.to_dict() if node and node.invalidated_at is None else None

    def get_neighbors(self, node_id: str, hops: int = 1) -> list[dict[str, Any]]:
        if node_id not in self.nodes:
            raise KeyError(f"unknown node: {node_id}")
        visited = {node_id}
        queue: deque[tuple[str, int]] = deque([(node_id, 0)])
        results: list[dict[str, Any]] = []
        while queue:
            current, depth = queue.popleft()
            if depth >= hops:
                continue
            for edge in self.edges:
                if edge.invalidated_at is not None:
                    continue
                neighbor = edge.target if edge.source == current else edge.source if edge.target == current else None
                if neighbor is None or neighbor in visited:
                    continue
                visited.add(neighbor)
                node = self.nodes[neighbor]
                results.append(
                    {
                        **node.to_dict(),
                        "relationship": edge.edge_type,
                        "direction": "outgoing" if edge.source == current else "incoming",
                        "hop": depth + 1,
                    }
                )
                queue.append((neighbor, depth + 1))
        return results

    def add_causal_relationship(
        self, source: str, target: str, relationship_type: str, **properties: Any
    ) -> GraphEdge:
        if relationship_type not in self.CAUSAL_TYPES:
            raise ValueError(f"relationship_type must be one of {sorted(self.CAUSAL_TYPES)}")
        return self.add_edge(source, target, relationship_type, **properties)

    def trace_decision_chain(self, decision_id: str, max_steps: int = 5) -> list[dict[str, Any]]:
        if decision_id not in self.nodes:
            raise KeyError(f"unknown decision: {decision_id}")
        causal_edges = [edge for edge in self.edges if edge.edge_type in self.CAUSAL_TYPES]
        queue: deque[tuple[str, int]] = deque([(decision_id, 0)])
        visited = {decision_id}
        chain: list[dict[str, Any]] = []
        while queue:
            current, depth = queue.popleft()
            if depth >= max_steps:
                continue
            for edge in causal_edges:
                if edge.target != current or edge.source in visited:
                    continue
                visited.add(edge.source)
                chain.append({**edge.to_dict(), "hop": depth + 1})
                queue.append((edge.source, depth + 1))
        return chain

    def state_at(self, timestamp: datetime | str) -> dict[str, list[dict[str, Any]]]:
        at = _as_utc(timestamp)
        nodes = [
            node.to_dict()
            for node in self.nodes.values()
            if node.created_at <= at and (node.invalidated_at is None or node.invalidated_at > at)
        ]
        node_ids = {node["id"] for node in nodes}
        edges = [
            edge.to_dict()
            for edge in self.edges
            if edge.source in node_ids
            and edge.target in node_ids
            and edge.created_at <= at
            and (edge.invalidated_at is None or edge.invalidated_at > at)
        ]
        return {"nodes": nodes, "edges": edges}

    def to_kg_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "entities": [node.to_dict() for node in self.nodes.values() if node.invalidated_at is None],
            "relationships": [edge.to_dict() for edge in self.edges if edge.invalidated_at is None],
        }

