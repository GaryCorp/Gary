"""The deployment described without secrets, for Dave's security reviews."""

import os

from app.config import (
    AGENT_LIMITS,
    AGENT_WEB_SEARCH_MODEL,
    EASE_API_KEY,
    EASE_API_URL,
    GARY_EMPLOYEE_MODEL,
    GITHUB_SYNC_INTERVAL_MINUTES,
    JOPLIN_NOTEBOOK,
    OPENAI_REALTIME_MODEL,
    PERPLEXITY_API_KEY,
    PERPLEXITY_MODEL,
    PLANNING_MODEL,
    PLANNING_SCHEDULE,
    SCOPES,
    SPENDING_LIMITS,
)
from gary.finance.purchases import format_cents


def system_configuration_summary(
    card_vault_configured: bool, engineering_configured: bool
) -> dict:
    """Non-secret facts about this deployment, for security reviews."""
    return {
        "services": {
            "backend": "FastAPI on 127.0.0.1:8000 (loopback only); holds the OpenAI key, Google OAuth tokens (encrypted at rest), and the Joplin token",
            "voice": "local microphone and wake word; holds only the voice bridge token",
            "joplin-proxy": "host network, listens only on the Docker network gateway, forwards to Joplin's local API",
            "ease-api": "EASE ethical decision-making API for Lauren, on the assistant network and host 127.0.0.1:8002; "
                        "holds its own LLM key; stateless (no database)",
            "ease-worker and ease-redis": "EASE background job runner and its queue; not used by Gary",
        },
        "web_pages": ["/", "/login", "/events", "/approvals (CSRF-protected approve/reject)", "/team",
                      "/finance (CSRF-protected: add, freeze, or remove Catherine's card)",
                      "/engineering/status (read-only privacy and health report)", "/health"],
        "web_authentication": "none; relies on loopback-only binding",
        "google_scopes": SCOPES,
        "joplin_access": (
            f"Gary: notes and notebooks inside the {JOPLIN_NOTEBOOK} notebook only; "
            "Susan, Dave, Linda, Catherine, Lauren: create, list, and read notes directly in "
            "their own top-level notebook only; no editing or deleting"
        ),
        "openai_usage": {
            "voice": OPENAI_REALTIME_MODEL,
            "planning": PLANNING_MODEL,
            "specialists": GARY_EMPLOYEE_MODEL,
            "web_search": AGENT_WEB_SEARCH_MODEL,
        },
        "perplexity_usage": (
            f"Susan's perplexity_search tool, model {PERPLEXITY_MODEL}: one HTTPS request "
            "carrying only her research question; read-only, no company data, key in the "
            "environment only"
            if PERPLEXITY_API_KEY
            else "not configured; perplexity_search reports itself unavailable"
        ),
        "operations_database": "SQLite data/gary.db, owner-only, append-only audit log, daily backups",
        "approval_policy": "code-defined green/yellow/red; voice approval needs spoken confirmation",
        "planning_schedule": {name: time.strftime("%H:%M") for name, time in PLANNING_SCHEDULE.items()},
        "agent_limits": AGENT_LIMITS.__dict__,
        "finance": {
            "card_storage": "card number Fernet-encrypted in data/card_vault.enc with CARD_ENCRYPTION_KEY; "
                            "SQLite holds only brand, last four digits, expiry, and status",
            "vault_key_configured": card_vault_configured,
            "purchases": "Catherine can request card_purchase actions (yellow, approvable only on the "
                         "/approvals web page); no payment channel is connected, so nothing is charged",
            "per_purchase_limit": format_cents(SPENDING_LIMITS.per_purchase_cents),
            "monthly_limit": format_cents(SPENDING_LIMITS.monthly_cents),
        },
        "engineering_github": {
            "configured": engineering_configured,
            "repository": f"{os.getenv('GITHUB_OWNER', '')}/{os.getenv('GITHUB_REPOSITORY', '')}".strip("/"),
            "project_number": os.getenv("GITHUB_PROJECT_NUMBER", ""),
            "engineer": os.getenv("GITHUB_ENGINEER_USERNAME", ""),
            "requirement": "repository and Project must both be private; every write is refused otherwise",
            "credential": "environment only (GITHUB_TOKEN), never stored in SQLite, Joplin, issues, or prompts",
            "permissions": "repository Metadata read, Issues write, organization Projects write; "
                            "no Contents, Actions, Administration, Workflows, or Secrets access",
            "sync_interval_minutes": GITHUB_SYNC_INTERVAL_MINUTES,
        },
        "crewai": "telemetry and tracing disabled; memory, planning, and code execution off",
        "ease": {
            "configured": bool(EASE_API_URL),
            "url": EASE_API_URL or None,
            "api_key_configured": bool(EASE_API_KEY),
            "use": "Lauren's run_ease_analysis sends the decision question and context she chooses "
                    "to EASE, which sends them to its LLM provider; at most one analysis per assignment",
        },
    }
