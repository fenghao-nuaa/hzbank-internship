"""Canonical user-persona candidates derived from provider knowledge proposals."""

from dataclasses import dataclass
from enum import StrEnum
import re

from dream.governance.knowledge import CandidateKnowledge
from dream.memory.items import AtomicMemoryItem, memory_id_for, parse_memory_items


_WORD = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "be",
        "for",
        "in",
        "is",
        "of",
        "on",
        "or",
        "preference",
        "preferences",
        "prefer",
        "prefers",
        "require",
        "requires",
        "that",
        "the",
        "this",
        "to",
        "user",
        "when",
        "with",
    }
)
_PERSONA_ID = re.compile(r"^<!-- dream-persona-id:\s*([^>]+) -->$")
_PERSONA_DOMAIN = re.compile(r"^<!-- dream-persona-domain:\s*([^>]+) -->$")
_PERSONA_CONFIDENCE = re.compile(
    r"^<!-- dream-persona-confidence:\s*([0-9.]+) -->$"
)
_DOMAIN_TERMS: dict[str, frozenset[str]] = {
    "crypto_investment": frozenset(
        {
            "bitcoin",
            "blockchain",
            "btc",
            "c2c",
            "contract",
            "crypto",
            "cryptocurrency",
            "doge",
            "eth",
            "exchange",
            "leverage",
            "margin",
            "spot",
            "usdt",
            "wallet",
            "以太坊",
            "保证金",
            "全仓",
            "加密",
            "合约",
            "币圈",
            "杠杆",
            "比特币",
            "爆仓",
            "现货",
            "虚拟货币",
            "逐仓",
        }
    ),
    "bank_operation": frozenset(
        {
            "account",
            "bank",
            "beneficiary",
            "invoice",
            "payment",
            "payroll",
            "reconciliation",
            "supplier",
            "transfer",
            "付款",
            "供应商",
            "受益人",
            "对账",
            "工资",
            "汇率",
            "账户",
            "转账",
            "银行",
        }
    ),
    "communication": frozenset(
        {
            "answer",
            "concise",
            "format",
            "response",
            "structured",
            "先给结论",
            "简洁",
            "结构化",
            "回复",
            "表达",
        }
    ),
    "workflow": frozenset(
        {
            "action plan",
            "deadline",
            "dependency",
            "evidence",
            "owner",
            "workflow",
            "依赖",
            "完成证据",
            "期限",
            "流程",
            "负责人",
        }
    ),
    "security": frozenset(
        {
            "credential",
            "password",
            "phishing",
            "scam",
            "security",
            "verification code",
            "安全",
            "密码",
            "诈骗",
            "钓鱼",
            "验证码",
        }
    ),
}


class PersonaCanonicalizationRequired(ValueError):
    """A persona proposal cannot be converted safely before writeback."""


class PersonaMergeType(StrEnum):
    DUPLICATE = "duplicate"
    EXTENSION = "extension"
    UPDATE = "update"
    MERGE = "merge"
    NEW = "new"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class PersonaCandidate:
    """Provider-independent, atomic user-persona update proposal."""

    id: str
    target_memory_id: str
    statement: str
    new_information: str
    evidence: tuple[str, ...]
    source_event_ids: tuple[str, ...]
    confidence: float
    merge_type: PersonaMergeType
    domain: str = "general"

    @classmethod
    def from_knowledge(cls, candidate: CandidateKnowledge) -> "PersonaCandidate":
        attributes = candidate.attributes
        return cls(
            id=candidate.knowledge_id,
            target_memory_id=str(attributes.get("target_memory_id", "")).strip(),
            statement=str(attributes.get("statement", candidate.content)).strip(),
            new_information=str(attributes.get("new_information", "")).strip(),
            evidence=_strings(attributes.get("evidence")),
            source_event_ids=candidate.source_event_ids,
            confidence=candidate.confidence,
            merge_type=_merge_type(attributes.get("merge_type")),
            domain=persona_domain_for(candidate.content, attributes),
        )


@dataclass(frozen=True)
class PersonaAtom:
    """A domain-aware persona item stored inside one USER.md atom."""

    persona_id: str
    domain: str
    statement: str
    confidence: float


def parse_persona_atom(content: str) -> PersonaAtom:
    """Parse persona metadata while remaining compatible with legacy atoms."""

    persona_id = ""
    domain = ""
    confidence = 1.0
    statement_lines: list[str] = []
    for line in content.strip().splitlines():
        if match := _PERSONA_ID.fullmatch(line.strip()):
            persona_id = match.group(1).strip()
            continue
        if match := _PERSONA_DOMAIN.fullmatch(line.strip()):
            domain = _domain_name(match.group(1))
            continue
        if match := _PERSONA_CONFIDENCE.fullmatch(line.strip()):
            confidence = min(1.0, max(0.0, float(match.group(1))))
            continue
        statement_lines.append(line)
    statement = "\n".join(statement_lines).strip()
    return PersonaAtom(
        persona_id=persona_id or memory_id_for(statement),
        domain=domain or persona_domain_for(statement),
        statement=statement,
        confidence=confidence,
    )


def render_persona_atom(candidate: PersonaCandidate, *, statement: str = "") -> str:
    """Render stable metadata without changing MemoryManager's atomic protocol."""

    value = statement.strip() or candidate.statement.strip()
    persona_id = candidate.id.strip() or memory_id_for(value)
    return (
        f"<!-- dream-persona-id: {persona_id} -->\n"
        f"<!-- dream-persona-domain: {candidate.domain} -->\n"
        f"<!-- dream-persona-confidence: {candidate.confidence:.3f} -->\n"
        f"{value}"
    )


def persona_domain_for(
    content: str,
    attributes: dict[str, object] | None = None,
) -> str:
    """Return a deterministic broad domain used only for merge isolation."""

    explicit = _text((attributes or {}).get("domain"))
    if explicit:
        return _domain_name(explicit)
    corpus = " ".join(
        (
            content,
            *_strings((attributes or {}).get("new_information")),
            *_strings((attributes or {}).get("signals")),
            *_strings((attributes or {}).get("steps")),
            *_strings((attributes or {}).get("constraints")),
        )
    ).casefold()
    ranked = [
        (sum(term in corpus for term in terms), domain)
        for domain, terms in _DOMAIN_TERMS.items()
    ]
    score, domain = max(ranked, default=(0, "general"))
    return domain if score else "general"


class PersonaCanonicalizer:
    """Convert a broad knowledge candidate into one complete persona statement."""

    def __init__(self, existing_memory: str) -> None:
        self._items = list(parse_memory_items(existing_memory))

    def canonicalize(self, candidate: CandidateKnowledge) -> PersonaCandidate:
        attributes = candidate.attributes
        declared = _merge_type(attributes.get("merge_type"), allow_empty=True)
        domain = persona_domain_for(candidate.content, attributes)
        target = (
            None
            if declared is PersonaMergeType.NEW
            else self._target(candidate, domain=domain)
        )
        base_statement = _text(attributes.get("statement")) or candidate.content.strip()
        if not base_statement:
            raise PersonaCanonicalizationRequired(
                "persona statement must be non-empty"
            )

        new_parts = self._new_parts(attributes, base_statement)
        new_information = "\n".join(new_parts)
        evidence = _unique_strings(
            (*_strings(attributes.get("evidence")), *_strings(attributes.get("signals")))
        )
        if declared is PersonaMergeType.CONFLICT:
            merge_type = PersonaMergeType.CONFLICT
        elif declared is PersonaMergeType.NEW:
            merge_type = PersonaMergeType.NEW
        elif declared in {
            PersonaMergeType.UPDATE,
            PersonaMergeType.MERGE,
            PersonaMergeType.EXTENSION,
        }:
            if target is None:
                raise PersonaCanonicalizationRequired(
                    f"{declared.value} persona must identify a same-domain target"
                )
            merge_type = declared
        elif target is None:
            merge_type = PersonaMergeType.NEW
        elif _normalized(base_statement) == _normalized(target.content) and not new_parts:
            merge_type = PersonaMergeType.DUPLICATE
        else:
            merge_type = PersonaMergeType.EXTENSION

        statement = self._statement(
            base_statement,
            target=target,
            new_parts=new_parts,
            merge_type=merge_type,
        )
        result = PersonaCandidate(
            id=candidate.knowledge_id,
            target_memory_id=target.memory_id if target is not None else "",
            statement=statement,
            new_information=new_information,
            evidence=evidence,
            source_event_ids=candidate.source_event_ids,
            confidence=candidate.confidence,
            merge_type=merge_type,
            domain=domain,
        )
        self._advance(result)
        return result

    def _target(
        self,
        candidate: CandidateKnowledge,
        *,
        domain: str,
    ) -> AtomicMemoryItem | None:
        explicit = _text(candidate.attributes.get("target_memory_id"))
        if explicit:
            target = self._by_id(explicit)
            if target is None:
                raise PersonaCanonicalizationRequired(
                    "target_memory_id does not identify an existing atomic memory"
                )
            self._require_compatible_domain(target, domain)
            return target
        by_candidate_id = self._by_id(candidate.knowledge_id)
        if by_candidate_id is not None:
            self._require_compatible_domain(by_candidate_id, domain)
            return by_candidate_id
        exact = next(
            (
                item
                for item in self._items
                if _normalized(item.content) == _normalized(candidate.content)
            ),
            None,
        )
        if exact is not None:
            self._require_compatible_domain(exact, domain)
            return exact
        return self._related(candidate.content, domain=domain)

    @staticmethod
    def _require_compatible_domain(item: AtomicMemoryItem, domain: str) -> None:
        existing_domain = parse_persona_atom(item.content).domain
        if "general" not in {existing_domain, domain} and existing_domain != domain:
            raise PersonaCanonicalizationRequired(
                "persona target belongs to a different domain"
            )

    def _by_id(self, memory_id: str) -> AtomicMemoryItem | None:
        return next(
            (item for item in self._items if item.memory_id == memory_id),
            None,
        )

    def _related(self, statement: str, *, domain: str) -> AtomicMemoryItem | None:
        candidate_tokens = _tokens(statement)
        ranked: list[tuple[int, float, int, AtomicMemoryItem]] = []
        for index, item in enumerate(self._items):
            atom = parse_persona_atom(item.content)
            if atom.domain != domain:
                continue
            item_tokens = _tokens(atom.statement)
            overlap = len(candidate_tokens & item_tokens)
            denominator = min(len(candidate_tokens), len(item_tokens))
            score = overlap / denominator if denominator else 0.0
            ranked.append((overlap, score, -index, item))
        if not ranked:
            return None
        overlap, score, _, item = max(ranked, key=lambda value: value[:3])
        return item if overlap >= 2 and score >= 0.08 else None

    @staticmethod
    def _new_parts(
        attributes: dict[str, object],
        base_statement: str,
    ) -> tuple[str, ...]:
        values = (
            *_strings(attributes.get("new_information")),
            *_strings(attributes.get("signals")),
            *_strings(attributes.get("steps")),
            *_strings(attributes.get("constraints")),
        )
        result: list[str] = []
        known = _normalized(base_statement)
        for value in _unique_strings(values):
            normalized = _normalized(value)
            if normalized and normalized not in known:
                result.append(value)
        return tuple(result)

    @staticmethod
    def _statement(
        base_statement: str,
        *,
        target: AtomicMemoryItem | None,
        new_parts: tuple[str, ...],
        merge_type: PersonaMergeType,
    ) -> str:
        if merge_type is PersonaMergeType.DUPLICATE:
            return target.content if target is not None else base_statement
        sections: list[str] = []
        if target is not None:
            target_statement = parse_persona_atom(target.content).statement
            normalized_target = _normalized(target_statement)
            normalized_base = _normalized(base_statement)
            if normalized_target in normalized_base:
                sections.append(base_statement.strip())
            elif normalized_base in normalized_target:
                sections.append(target_statement.strip())
            else:
                sections.extend((target_statement.strip(), base_statement.strip()))
        else:
            sections.append(base_statement.strip())
        if new_parts:
            sections.append(
                "Additional durable requirements:\n"
                + "\n".join(f"- {value}" for value in new_parts)
            )
        return "\n".join(dict.fromkeys(section for section in sections if section))

    def _advance(self, candidate: PersonaCandidate) -> None:
        if candidate.merge_type is PersonaMergeType.DUPLICATE:
            return
        if candidate.target_memory_id:
            for index, item in enumerate(self._items):
                if item.memory_id == candidate.target_memory_id:
                    rendered = render_persona_atom(candidate)
                    self._items[index] = AtomicMemoryItem(
                        memory_id=memory_id_for(rendered),
                        content=rendered,
                        raw_entry=rendered,
                    )
                    return
        rendered = render_persona_atom(candidate)
        self._items.append(
            AtomicMemoryItem(
                memory_id=memory_id_for(rendered),
                content=rendered,
                raw_entry=rendered,
            )
        )


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    if isinstance(value, (list, tuple)):
        return tuple(
            item.strip() for item in value if isinstance(item, str) and item.strip()
        )
    return ()


def _unique_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalized(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(value.strip())
    return tuple(result)


def _normalized(content: str) -> str:
    return " ".join(content.split()).casefold()


def _domain_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")
    return normalized or "general"


def _merge_type(
    value: object,
    *,
    allow_empty: bool = False,
) -> PersonaMergeType | None:
    raw = _text(value).casefold()
    if not raw:
        return None if allow_empty else PersonaMergeType.NEW
    if raw == PersonaMergeType.EXTENSION.value:
        return PersonaMergeType.UPDATE
    try:
        return PersonaMergeType(raw)
    except ValueError as exc:
        raise PersonaCanonicalizationRequired("unknown persona merge_type") from exc


def _tokens(content: str) -> set[str]:
    tokens: set[str] = set()
    for raw in _WORD.findall(content.casefold()):
        if raw.isascii():
            if len(raw) > 1 and raw not in _STOP_WORDS:
                tokens.add(raw)
            continue
        if len(raw) == 1:
            tokens.add(raw)
        else:
            tokens.update(raw[index : index + 2] for index in range(len(raw) - 1))
    return tokens
