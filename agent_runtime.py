"""Run conversation turns against the agent and execute function calls locally.

Run interactively: python agent_runtime.py
Commands: /tickets (show officer queue), /new (new conversation), /quit
"""
import os
from dataclasses import dataclass, field
from typing import Callable

from dotenv import load_dotenv

import escalation

load_dotenv()
AGENT_NAME = os.getenv("FOUNDRY_AGENT_NAME", "park-visitor-assistant")
AGENT_REF = {"agent_reference": {"name": AGENT_NAME, "type": "agent_reference"}}
MAX_TOOL_ROUNDS = 3  # guard against tool-call loops


@dataclass
class TurnResult:
    text: str
    response_id: str
    tool_calls: list = field(default_factory=list)   # [(name, arguments_json)]
    citations: list = field(default_factory=list)    # filenames cited by file search


def run_turn(oai, user_text: str, previous_response_id: str | None = None,
             dispatcher: Callable[[str, str], str] = escalation.dispatch) -> TurnResult:
    # Only send previous_response_id when we have one: the service rejects an explicit null.
    first = {"previous_response_id": previous_response_id} if previous_response_id else {}
    response = oai.responses.create(input=user_text, extra_body=AGENT_REF, **first)
    tool_calls = []

    for _ in range(MAX_TOOL_ROUNDS):
        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            break
        outputs = []
        for call in calls:
            tool_calls.append((call.name, call.arguments))
            outputs.append({"type": "function_call_output", "call_id": call.call_id,
                            "output": dispatcher(call.name, call.arguments)})
        response = oai.responses.create(
            input=outputs, previous_response_id=response.id, extra_body=AGENT_REF)

    citations = []
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                for ann in getattr(part, "annotations", None) or []:
                    if ann.type == "file_citation":
                        citations.append(getattr(ann, "filename", None) or ann.file_id)

    return TurnResult(response.output_text, response.id, tool_calls, sorted(set(citations)))


def _c(text, code):  # ANSI colour; plain text if output isn't a terminal
    import sys
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def show_tickets():
    import json
    path = escalation.QUEUE_PATH
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []
    if not rows:
        print(_c("  Officer queue is empty.", "2"))
    for t in rows:
        print(f"  {_c(t['ticket_id'], '33')}  {t['urgency']:<6} {t['reason']:<28} "
              f"{t['park_name'] or '-'}\n      {t['summary']}")


if __name__ == "__main__":
    import json

    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    with (
        DefaultAzureCredential() as cred,
        AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=cred) as project,
        project.get_openai_client() as oai,
    ):
        prev = None
        print(f"{AGENT_NAME}  |  /tickets  /new  /quit")
        while True:
            try:
                text = input(_c("\nYou: ", "1")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text == "/quit":
                break
            if text == "/new":
                prev = None
                print(_c("  New conversation.", "2"))
                continue
            if text == "/tickets":
                show_tickets()
                continue

            created = []

            def dispatcher(name, args):
                out = escalation.dispatch(name, args)
                created.append(json.loads(out))
                return out

            turn = run_turn(oai, text, prev, dispatcher)
            prev = turn.response_id
            print(f"\n{_c('Agent:', '1;32')} {turn.text}")
            if turn.citations:
                print(_c(f"  sources: {', '.join(turn.citations)}", "2"))
            for name, args in turn.tool_calls:
                print(_c(f"  tool call: {name} {args}", "36"))
            for result in created:
                if "ticket_id" in result:
                    sent = ", emailed to officer" if result.get("emailed") else ""
                    print(_c(f"  ticket created: {result['ticket_id']} "
                             f"(reply {result['expected_response']}{sent})", "33"))
                else:
                    print(_c(f"  tool error returned to agent: {result}", "31"))