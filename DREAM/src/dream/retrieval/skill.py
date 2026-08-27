"""Public Memory Retrieval Skill entry point for external agents."""

from pathlib import Path
from typing import Mapping

from dream.retrieval.config import RetrievalConfig, infer_domain
from dream.retrieval.context_builder import ContextBuilder
from dream.retrieval.loader import MemoryLoader
from dream.retrieval.models import (
    MemoryRetrievalRequest,
    MemoryRetrievalResponse,
    RetrievedMemory,
    RetrievalQuery,
)
from dream.retrieval.retriever import MemoryRetriever


class MemoryRetrievalSkill:
    """Retrieve a bounded, user-isolated context from durable DREAM artifacts."""

    def __init__(
        self,
        *,
        home: Path,
        tenant_id: str,
        agent_id: str,
        config: RetrievalConfig | None = None,
    ) -> None:
        self.home = Path(home)
        self.tenant_id = tenant_id
        self.agent_id = agent_id
        self.config = config or RetrievalConfig()

    def retrieve(
        self,
        user_id: str,
        query: str,
        task_context: Mapping[str, object] | None = None,
        limit: int = 5,
    ) -> MemoryRetrievalResponse:
        request = MemoryRetrievalRequest(
            user_id=user_id,
            query=query,
            task_context=task_context or {},
            limit=limit,
        )
        effective_limit = min(request.limit, self.config.max_limit)
        domain = infer_domain(request.query, request.task_context)
        retrieval_query = RetrievalQuery(
            text=request.query,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            user_id=request.user_id,
            limit=effective_limit,
            domain=domain,
        )
        loader = MemoryLoader(
            home=self.home,
            tenant_id=self.tenant_id,
            agent_id=self.agent_id,
            user_id=request.user_id,
        )
        result = MemoryRetriever(loader).retrieve(retrieval_query)
        context = ContextBuilder(
            token_budget=self.config.context_token_budget,
            duplicate_similarity=self.config.duplicate_similarity,
        ).build(result)
        included = set(context.included_memory_ids)
        memories = tuple(
            RetrievedMemory.from_ranked(match)
            for match in result.matches
            if match.record.memory_id in included
        )
        return MemoryRetrievalResponse(
            query=request.query,
            memories=memories,
            context=context.markdown,
            domain=domain,
        )
