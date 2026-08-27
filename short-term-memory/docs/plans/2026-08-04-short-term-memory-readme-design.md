# Short-Term Memory README Design

## Goal

Replace the inherited DREAM README with a branch-specific guide that explains
and audits the PLAN.md short-term-memory path: Redis session storage, journals
durability, Headroom compression, official Proxy/CCR recall, and the company
Agent call boundary.

## Audience

- Reviewers checking whether the branch satisfies PLAN.md sections 5.1–5.4.
- Company Agent developers integrating DREAM before and after an LLM turn.
- Developers running Redis, Headroom, and focused acceptance tests locally.

## Information architecture

The root README will contain:

1. Branch purpose and exclusions.
2. A PLAN-to-code capability matrix with `implemented`, `partial`, and
   `not included` states.
3. Write, read, compression, and official CCR request flows.
4. Redis key/TTL and summary-envelope semantics.
5. The Python integration boundary exposed by `ConversationHandler`.
6. Environment setup, service startup, and verification commands.
7. Known limitations, including the unverified official transparent CCR
   continuation path.

Detailed implementation notes remain in `docs/short-term-memory.md`; the root
README links to that file instead of duplicating every test detail.

## Accuracy rules

- Do not claim DREAM generates the final user answer.
- Do not claim `/v1/compress` alone proves CCR retrieval.
- State that DREAM supplies a Proxy URL and anonymous stable scope while the
  official Headroom Proxy owns routing, relevance detection, cache retrieval,
  tool injection, and continuation.
- Mark transparent CCR continuation through a real provider as partially
  implemented until its opt-in acceptance test passes.
- Distinguish Redis short-term-context recovery from Headroom CCR recovery.
- Do not document historical-session UI, Memory Retrieval Skill, Persona,
  Decision Card, or Wiki persistence as part of this branch.

## Validation

- Scan the rewritten README to ensure inherited old-architecture sections are
  gone.
- Run the focused deterministic short-term-memory tests.
- Run Ruff on the implementation and focused tests.
- Report external Redis and Headroom tests as opt-in; do not fabricate results.
