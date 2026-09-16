"""Business logic. Services validate state, enforce rules (dependency cycles,
policy, approvals), keep related writes in one transaction, and write the
audit log. They contain no SQL and no LLM logic."""
