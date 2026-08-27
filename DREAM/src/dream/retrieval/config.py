"""Configuration and deterministic domain inference for memory retrieval."""

from dataclasses import dataclass
from typing import Mapping


DOMAIN_ALIASES = {
    "crypto": "crypto_investment",
    "cryptocurrency": "crypto_investment",
    "crypto_investment": "crypto_investment",
    "finance": "finance",
    "banking": "finance",
    "coding": "coding",
    "code": "coding",
    "writing": "writing",
    "research": "research",
}

DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "crypto_investment": (
        "btc",
        "eth",
        "bitcoin",
        "ethereum",
        "crypto",
        "usdt",
        "合约",
        "现货",
        "杠杆",
        "爆仓",
        "加密货币",
        "虚拟货币",
        "逐仓",
        "全仓",
    ),
    "finance": (
        "bank",
        "payment",
        "supplier",
        "account",
        "invoice",
        "transfer",
        "银行",
        "付款",
        "供应商",
        "账户",
        "转账",
        "汇率",
        "对账",
        "工资",
        "制裁",
    ),
    "coding": (
        "python",
        "code",
        "coding",
        "debug",
        "exception",
        "traceback",
        "代码",
        "编程",
        "报错",
        "异常",
        "调试",
    ),
    "writing": (
        "writing",
        "report",
        "reply",
        "complaint",
        "document",
        "写作",
        "报告",
        "回复",
        "投诉",
        "文稿",
    ),
    "research": (
        "research",
        "paper",
        "literature",
        "citation",
        "研究",
        "论文",
        "文献",
        "引用",
    ),
}


@dataclass(frozen=True)
class RetrievalConfig:
    default_limit: int = 5
    max_limit: int = 10
    context_token_budget: int = 1_200
    duplicate_similarity: float = 0.8

    def __post_init__(self) -> None:
        if self.default_limit < 1:
            raise ValueError("default_limit must be positive")
        if self.max_limit < self.default_limit:
            raise ValueError("max_limit must be at least default_limit")
        if self.context_token_budget < 1:
            raise ValueError("context_token_budget must be positive")
        if not 0 <= self.duplicate_similarity <= 1:
            raise ValueError("duplicate_similarity must be between zero and one")


def normalize_domain(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if not normalized:
        return None
    return DOMAIN_ALIASES.get(normalized, normalized)


def infer_domain(
    text: str,
    task_context: Mapping[str, object] | None = None,
) -> str | None:
    if task_context:
        explicit = task_context.get("domain")
        if isinstance(explicit, str) and explicit.strip():
            return normalize_domain(explicit)
    normalized = text.casefold()
    scores = {
        domain: sum(keyword in normalized for keyword in keywords)
        for domain, keywords in DOMAIN_KEYWORDS.items()
    }
    best_domain, best_score = max(scores.items(), key=lambda item: item[1])
    return best_domain if best_score else None
