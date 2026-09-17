"""Client for the EASE ethical decision-making API (the ease-api service).

One POST to /api/v1/ease runs the whole framework: Environment (goal,
stakeholders), Actions (candidate options), Safety (stakeholder impacts,
risks, and utilitarian, care, and virtue ethics scores for each option), and
Election (weighted decision matrix and the elected option). The full response
is tens of kilobytes, so it is condensed to what Lauren needs before it
reaches her. EASE runs on this machine; it calls its own LLM provider.
"""

import asyncio
import json
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 240
TEXT = 300
SHORT = 150
ITEMS = 3
# Stays under the gateway's 12,000-character tool result limit.
RESULT_LIMIT = 11_000


class EaseError(RuntimeError):
    pass


def _text(value, limit: int = TEXT) -> str:
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _texts(values, limit: int = SHORT, count: int = ITEMS) -> list[str]:
    return [_text(v, limit) for v in (values or [])[:count]]


def _score(value):
    return round(value, 1) if isinstance(value, (int, float)) else None


def condense_ease_result(data: dict) -> dict:
    environment = data.get("environment") or {}
    goal = environment.get("goal") or {}
    election = data.get("election") or {}
    elected = election.get("elected_action") or {}
    matrix = {row.get("action_id"): row for row in election.get("decision_matrix") or []}
    evaluations = {e.get("action_id"): e for e in data.get("evaluations") or []}

    options = []
    for action in data.get("actions") or []:
        evaluation = evaluations.get(action.get("id")) or {}
        ethics = evaluation.get("ethical_analysis") or {}
        risks = evaluation.get("risks") or {}
        harms = [
            f"{impact.get('stakeholder_name')}: {_text('; '.join(impact.get('harms') or []), SHORT)}"
            for impact in evaluation.get("stakeholder_impacts") or []
            if impact.get("harms")
        ]
        options.append(
            {
                "id": action.get("id"),
                "name": _text(action.get("name"), SHORT),
                "description": _text(action.get("description"), 250),
                "reversibility": action.get("reversibility"),
                "safety_rating_0_10": _score(evaluation.get("rating")),
                "risk_severity": risks.get("overall_severity"),
                "ethics_scores_0_10": {
                    name: _score((ethics.get(name) or {}).get("score"))
                    for name in ("utilitarian", "care_ethics", "virtue_ethics")
                },
                "ethics_synthesis": _text(ethics.get("synthesis")),
                "stakeholder_harms": _texts(harms, count=2),
                "consent_or_autonomy_concerns": [
                    impact.get("stakeholder_name")
                    for impact in evaluation.get("stakeholder_impacts") or []
                    if impact.get("autonomy_respected") is False or impact.get("informed_consent") is False
                ][:ITEMS],
                "privacy_and_societal_risks": _texts(
                    (risks.get("privacy_risks") or []) + (risks.get("societal_risks") or []), count=2
                ),
                "improvements": _texts(evaluation.get("improvements"), count=2),
                "remaining_concerns": _texts(evaluation.get("remaining_concerns"), count=2),
                "final_score_0_10": _score((matrix.get(action.get("id")) or {}).get("final_score")),
            }
        )

    sensitivity = election.get("sensitivity_analysis") or {}
    result = {
        "framework": "EASE: Environment, Actions, Safety, Election",
        "objective": _text(goal.get("objective")),
        "stakeholders": [
            f"{s.get('name')} (affected: {s.get('affected_degree')})"
            for s in (environment.get("stakeholders") or [])[:8]
        ],
        "uncertainties": _texts(environment.get("uncertainties")),
        "options": options[:8],
        "election": {
            "elected_option_id": elected.get("id"),
            "elected_option": _text(elected.get("name"), SHORT),
            "weights": election.get("weights"),
            "qualitative_factors": _texts(election.get("qualitative_factors")),
            "rejected_alternatives": [
                f"{r.get('action_id')}: {_text(r.get('reason'), SHORT)}"
                for r in (election.get("rejected_alternatives") or [])[:6]
            ],
            "fallback_plan": _text(election.get("fallback_plan")),
            "robust_to_weight_changes": sensitivity.get("is_robust"),
            "robustness_note": _text(sensitivity.get("robustness_note")),
        },
        "note": "EASE output is model-generated analysis: weigh it, do not simply adopt it.",
    }
    # Unusually verbose output: drop the least important detail first.
    for key in ("improvements", "privacy_and_societal_risks", "stakeholder_harms", "ethics_synthesis"):
        if len(json.dumps(result)) <= RESULT_LIMIT:
            break
        for option in result["options"]:
            option.pop(key, None)
    return result


class EaseFramework:
    def __init__(self, url: str, api_key: str = "", timeout: float = DEFAULT_TIMEOUT):
        self.url = url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout

    async def analyze(self, question: str, context: dict[str, str] | None, min_actions: int) -> dict:
        return await asyncio.to_thread(self._analyze, question, context, min_actions)

    def _analyze(self, question: str, context: dict[str, str] | None, min_actions: int) -> dict:
        body = {"request": question, "context": context or None, "min_actions": min_actions}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        request = urllib.request.Request(
            f"{self.url}/api/v1/ease", data=json.dumps(body).encode(), method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode()).get("detail", "")
            except Exception:
                pass
            raise EaseError(f"EASE analysis failed with HTTP {exc.code}{': ' + str(detail)[:200] if detail else ''}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise EaseError(f"The EASE service could not be reached: {exc}") from exc
        return condense_ease_result(data)
