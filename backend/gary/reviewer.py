"""The judgment half of a performance review.

A review is two things kept apart on purpose. The scorecard is arithmetic
over what the company recorded (services/performance.py), and it is the same
whoever reads it. This is the other half: one model reading those numbers and
saying what they mean.

The numbers are handed to the model; it is never asked to remember or
estimate them. It cannot see the database, cannot call a tool, and what it
writes changes nothing by itself — a review is a record, not an action. What
it produces is validated in Python before it is stored, like every other
model output here.

Three kinds of review, and the third is the point of the exercise: an
employee reviewing Gary. That one is written by the employee's own model,
speaking as them, and Gary has to answer it.
"""

import asyncio
import json
import urllib.error
import urllib.request

RESPONSES_URL = "https://api.openai.com/v1/responses"

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "strengths", "concerns", "recommendations", "evidence"],
    "properties": {
        "summary": {
            "type": "string",
            "description": "What this record shows, in three or four sentences.",
        },
        "strengths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What is going well. One per item, each tied to a number.",
        },
        "concerns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What is not. One per item, each tied to a number.",
        },
        "recommendations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What should change, addressed to whoever can change it.",
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The figures relied on, quoted from the scorecard.",
        },
    },
}

_SHARED = (
    "You are writing a performance review inside GaryCorp, a small company of "
    "AI employees run by Gary, the Chief of Staff, for {principal}.\n\n"
    "The scorecard you are given is the whole factual record: it was computed "
    "from the company's database, not by a model. Use those numbers and no "
    "others. Never invent a figure, an incident or a quotation. If the record "
    "is too thin to support a judgment, say so plainly instead of padding it.\n\n"
    "Be specific and be useful. Tie every strength and every concern to a "
    "number in the scorecard, and quote the figures you relied on in evidence. "
    "No flattery, no hedging, no praise for doing the job. A review that could "
    "have been written without reading the numbers is worthless.\n"
)

INSTRUCTIONS = {
    "employee": _SHARED + (
        "\nYou are Gary, reviewing {subject}, who works for you. Judge the work: "
        "was it delivered, was it right, did it cost more than it was worth, and "
        "is their stated confidence matched by their results? Confident failures "
        "and rejected reports matter more than volume. Address the "
        "recommendations to them, and to yourself where the fault is how you "
        "assigned the work."
    ),
    "principal": _SHARED + (
        "\nYou are Gary, reviewing {principal} — the person you work for. He "
        "asked for this, and a review that only flatters him wastes his time. "
        "The company is only as unblocked as he is: look at work he left "
        "overdue, blocks he booked and missed, approvals and questions he never "
        "answered, and how much of what you assigned him got done the same day. "
        "Be direct and be fair; he is one person, not a team."
    ),
    "manager": _SHARED + (
        "\nYou are {reviewer_name}, {reviewer_title} at GaryCorp, reviewing "
        "Gary — your manager. This is an upward review: you are being asked "
        "what it is like to work for him. Judge how he manages, not how you "
        "perform. Look at whether the work he sent you was clear and worth "
        "doing, whether your reports were used, how often he asked "
        "{principal} for things, and what the company's thinking cost per "
        "cycle. You may be critical; a polite review of your manager is a "
        "wasted one. He will read it and has to answer."
    ),
}


class ReviewerError(RuntimeError):
    pass


def instructions_for(kind: str, subject: str, principal: str, reviewer: dict | None) -> str:
    template = INSTRUCTIONS.get(kind)
    if template is None:
        raise ReviewerError(f"Unknown review kind: {kind}")
    reviewer = reviewer or {}
    return template.format(
        principal=principal,
        subject=subject,
        reviewer_name=reviewer.get("name", "an employee"),
        reviewer_title=reviewer.get("title", "a specialist"),
    )


class OpenAIReviewer:
    """One model call per review, with a strict schema and no tools."""

    def __init__(
        self,
        api_key: str,
        model: str,
        employee_model: str | None = None,
        principal: str = "Alex",
        timeout: float = 120,
        url: str = RESPONSES_URL,
    ):
        self.api_key = api_key
        self.model = model
        # An upward review is written by the employee, so it is written by
        # the model the employees run on, not by Gary's.
        self.employee_model = employee_model or model
        self.principal = principal
        self.timeout = timeout
        self.url = url

    async def write_review(
        self, kind: str, subject: str, scorecard: dict, reviewer: dict | None = None
    ) -> dict:
        return await asyncio.to_thread(
            self._write_sync, kind, subject, scorecard, reviewer
        )

    def _write_sync(
        self, kind: str, subject: str, scorecard: dict, reviewer: dict | None
    ) -> dict:
        model = self.employee_model if kind == "manager" else self.model
        body = {
            "model": model,
            "instructions": instructions_for(kind, subject, self.principal, reviewer),
            "input": json.dumps(
                {"subject": subject, "scorecard": scorecard}, ensure_ascii=False
            ),
            # A review is about the company's own people; it is not kept by OpenAI.
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "performance_review",
                    "strict": True,
                    "schema": REVIEW_SCHEMA,
                }
            },
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise ReviewerError(f"OpenAI returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ReviewerError(f"Could not reach OpenAI: {exc}") from exc

        from gary.planner import response_text

        try:
            review = json.loads(response_text(data))
        except json.JSONDecodeError as exc:
            raise ReviewerError("The model did not return a review") from exc
        review["_usage"] = data.get("usage") or {}
        review["_model"] = data.get("model") or model
        return review
