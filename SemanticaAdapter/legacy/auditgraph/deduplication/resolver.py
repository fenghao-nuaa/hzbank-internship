"""Deterministic canonical-name entity resolution."""

import copy
import re
import unicodedata

from auditgraph.core.models import Entity


def _canonical(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", "", normalized)


class EntityResolver:
    def resolve(self, entities: list[Entity]) -> list[Entity]:
        resolved, _ = self.resolve_with_mapping(entities)
        return resolved

    def resolve_with_mapping(self, entities: list[Entity]) -> tuple[list[Entity], dict[str, str]]:
        resolved: dict[tuple[str, str], Entity] = {}
        alias_index: dict[tuple[str, str], tuple[str, str]] = {}
        id_mapping: dict[str, str] = {}

        for entity in entities:
            entity_type = entity.entity_type.casefold()
            candidates = {_canonical(entity.name), *(_canonical(alias) for alias in entity.aliases)}
            key = next(
                (alias_index[(entity_type, candidate)] for candidate in candidates if (entity_type, candidate) in alias_index),
                (entity_type, _canonical(entity.name)),
            )
            if key not in resolved:
                resolved[key] = copy.deepcopy(entity)
            else:
                target = resolved[key]
                target.aliases.update(entity.aliases)
                target.aliases.add(entity.name)
                target.source_ids.update(entity.source_ids)
                target.confidence = max(target.confidence, entity.confidence)
                for name, value in entity.properties.items():
                    target.properties.setdefault(name, value)
            id_mapping[entity.entity_id] = resolved[key].entity_id
            for candidate in candidates:
                alias_index[(entity_type, candidate)] = key
        return list(resolved.values()), id_mapping
