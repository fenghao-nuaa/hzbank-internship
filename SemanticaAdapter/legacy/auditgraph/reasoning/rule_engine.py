"""Small, deterministic forward rule engine with explicit explanations."""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class Condition:
    field: str
    operator: str
    value: Any


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    version: str
    conditions: list[Condition]
    conclusion: str
    source_ref: str | None = None
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.rule_id or not self.version or not self.conclusion:
            raise ValueError("rule_id, version, and conclusion are required")


@dataclass(slots=True)
class RuleMatch:
    rule_id: str
    version: str
    conclusion: str
    facts: dict[str, Any]
    steps: list[str]
    source_ref: str | None = None


@dataclass(slots=True)
class ReasoningResult:
    conclusions: list[str] = field(default_factory=list)
    matches: list[RuleMatch] = field(default_factory=list)


class RuleEngine:
    _OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
        "==": operator.eq,
        "!=": operator.ne,
        ">": operator.gt,
        ">=": operator.ge,
        "<": operator.lt,
        "<=": operator.le,
        "in": lambda actual, expected: actual in expected,
        "contains": lambda actual, expected: expected in actual,
    }

    def __init__(self, rules: list[Rule] | None = None) -> None:
        self.rules = sorted(rules or [], key=lambda rule: (-rule.priority, rule.rule_id))

    def add_rule(self, rule: Rule) -> None:
        self.rules.append(rule)
        self.rules.sort(key=lambda item: (-item.priority, item.rule_id))

    def evaluate(self, facts: dict[str, Any]) -> ReasoningResult:
        result = ReasoningResult()
        for rule in self.rules:
            matched_facts: dict[str, Any] = {}
            steps: list[str] = []
            matched = True
            for condition in rule.conditions:
                if condition.operator not in self._OPERATORS:
                    raise ValueError(f"unsupported operator: {condition.operator}")
                if condition.field not in facts:
                    matched = False
                    break
                actual = facts[condition.field]
                try:
                    condition_matched = self._OPERATORS[condition.operator](actual, condition.value)
                except TypeError:
                    condition_matched = False
                if not condition_matched:
                    matched = False
                    break
                matched_facts[condition.field] = actual
                steps.append(
                    f"{condition.field} {condition.operator} {condition.value!r} matched actual value {actual!r}"
                )
            if matched:
                result.conclusions.append(rule.conclusion)
                result.matches.append(
                    RuleMatch(
                        rule_id=rule.rule_id,
                        version=rule.version,
                        conclusion=rule.conclusion,
                        facts=matched_facts,
                        steps=steps,
                        source_ref=rule.source_ref,
                    )
                )
        return result
