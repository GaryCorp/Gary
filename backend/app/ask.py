"""Talk to Gary without a microphone.

    docker compose exec backend python -m app.ask "What should I work on?"
    docker compose exec backend python -m app.ask --show-tools "Create an engineering ticket for X"

Gary's own instructions and his company tools, driven over the Responses API
instead of the voice bridge. This is how to make Gary *do* something in a
script, a test, or a demo, and how to watch which tools he chooses.

Deliberately narrower than voice: only the GaryCorp tools are offered
(projects, tasks, follow-ups, planning, approvals, the specialist team,
engineering tickets). Google Calendar, Gmail and Joplin are not, so a text
conversation cannot send email, change the calendar, or write notes. Approvals
still apply to everything that has a policy: Gary proposes, Alex approves.
"""

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request

RESPONSES_URL = "https://api.openai.com/v1/responses"
MAX_TURNS = 8
RESULT_PREVIEW = 400


class AskError(RuntimeError):
    pass


def _post(url: str, key: str, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise AskError(f"OpenAI returned HTTP {exc.code}: {exc.read().decode()[:300]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AskError(f"Could not reach OpenAI: {exc}") from exc


def _text(payload: dict) -> str:
    parts = []
    for item in payload.get("output") or []:
        if item.get("type") != "message":
            continue
        for chunk in item.get("content") or []:
            if chunk.get("type") == "output_text":
                parts.append(chunk.get("text", ""))
    return "\n".join(parts).strip()


def _calls(payload: dict) -> list[dict]:
    return [item for item in payload.get("output") or [] if item.get("type") == "function_call"]


async def ask(prompt: str, *, show_tools: bool = True, model: str | None = None) -> str:
    from app import main as app_main
    from gary.finance.pricing import usage_from_openai
    from gary.tools import call_tool

    key = app_main.OPENAI_API_KEY
    model = model or app_main.PLANNING_MODEL
    session: dict = {"event_ids": set(), "emails": {}, "replied": set(),
                     "new_emails": set(), "note_ids": set(), "approval_ids": set()}
    context = app_main.GaryToolContext(app_main.gary_ops, session, app_main.GARY_INTEGRATIONS)

    conversation: list[dict] = [{"role": "user", "content": prompt}]
    instructions = app_main.build_instructions() + (
        "\n\nThis is a written exchange, not speech: no microphone is attached. "
        "Use your tools to actually do what is asked, then answer in plain text. "
        "Only report what the tools returned."
    )

    for turn in range(1, MAX_TURNS + 1):
        payload = await asyncio.to_thread(
            _post,
            RESPONSES_URL,
            key,
            {
                "model": model,
                "instructions": instructions,
                "input": conversation,
                "tools": app_main.GARY_TOOL_SCHEMAS,
                "tool_choice": "auto",
                "store": False,
            },
            180.0,
        )
        app_main.usage_ledger.record(
            "other", payload.get("model") or model,
            usage_from_openai(payload.get("usage")),
            detail="text conversation with Gary",
        )

        calls = _calls(payload)
        if not calls:
            return _text(payload) or "(Gary said nothing)"

        for call in calls:
            name = call.get("name", "")
            arguments = json.loads(call.get("arguments") or "{}")
            result = await call_tool(name, arguments, context)
            if show_tools:
                summary = json.dumps(result, default=str)
                mark = "ok " if result.get("success", True) else "FAIL"
                print(f"  [{mark}] {name}({json.dumps(arguments, default=str)[:160]})")
                print(f"         -> {summary[:RESULT_PREVIEW]}")
            conversation.append(call)
            conversation.append(
                {
                    "type": "function_call_output",
                    "call_id": call.get("call_id"),
                    "output": json.dumps(result, default=str),
                }
            )

    return "(Gary used his tools but did not finish within the turn limit)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ask")
    parser.add_argument("prompt", help="what to say to Gary")
    parser.add_argument("--model", help="override the model (default: PLANNING_MODEL)")
    parser.add_argument("--quiet", action="store_true", help="hide the tool calls")
    args = parser.parse_args(argv)

    try:
        answer = asyncio.run(ask(args.prompt, show_tools=not args.quiet, model=args.model))
    except AskError as exc:
        print(f"error: {exc}")
        return 1
    print(f"\nGary: {answer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
