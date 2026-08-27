"""Knowledge graph builder over the shared ContextGraph implementation."""

from auditgraph.context.context_graph import ContextGraph
from auditgraph.core.models import ExtractionResult


class KnowledgeGraph(ContextGraph):
    @classmethod
    def from_extraction(cls, extraction: ExtractionResult) -> "KnowledgeGraph":
        graph = cls()
        for entity in extraction.entities:
            graph.add_node(
                entity.entity_id,
                entity.entity_type,
                name=entity.name,
                aliases=sorted(entity.aliases),
                source_ids=sorted(entity.source_ids),
                confidence=entity.confidence,
                **entity.properties,
            )
        for relation in extraction.relations:
            if relation.subject_id not in graph.nodes:
                graph.add_node(relation.subject_id, "Entity")
            if relation.object_id not in graph.nodes:
                graph.add_node(relation.object_id, "Entity")
            graph.add_edge(
                relation.subject_id,
                relation.object_id,
                relation.predicate,
                relation_id=relation.relation_id,
                source_id=relation.source_id,
                confidence=relation.confidence,
                **relation.properties,
            )
        for triplet in extraction.triplets:
            if triplet.subject not in graph.nodes:
                graph.add_node(triplet.subject, "Entity")
            node = graph.nodes[triplet.subject]
            existing = node.properties.get(triplet.predicate)
            if existing is None:
                node.properties[triplet.predicate] = triplet.object
            elif existing != triplet.object:
                values = existing if isinstance(existing, list) else [existing]
                if triplet.object not in values:
                    values.append(triplet.object)
                node.properties[triplet.predicate] = values
            sources = node.properties.setdefault("fact_sources", {})
            sources.setdefault(triplet.predicate, []).append(triplet.source_id)
        return graph
