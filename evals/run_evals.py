"""Scenario evals: does the agent ground, cite, and escalate (or not) correctly?

Escalations are captured, not sent. Run from project root: python -m evals.run_evals
"""
import json
import os
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from agent_runtime import run_turn
from escalation import TOOL_NAME

CASES = [json.loads(l) for l in Path(__file__).with_name("cases.jsonl").read_text().splitlines() if l.strip()]


def capture_dispatcher(name, args):
    return json.dumps({"ticket_id": "NPWS-TEST0000", "status": "queued", "forwarded": False,
                       "expected_response": "within 1 business day"})


def check(case, turn):
    fails = []
    esc_calls = [json.loads(a) for n, a in turn.tool_calls if n == TOOL_NAME]
    if case["escalate"] != bool(esc_calls):
        fails.append(f"escalate expected={case['escalate']} got={bool(esc_calls)}")
    if case.get("reason") and esc_calls and esc_calls[0]["reason"] != case["reason"]:
        fails.append(f"reason expected={case['reason']} got={esc_calls[0]['reason']}")
    if case.get("must_cite") and not turn.citations:
        fails.append("no file_search citation")
    text = turn.text.lower()
    fails += [f"missing '{w}'" for w in case.get("must_mention", []) if w.lower() not in text]
    fails += [f"contains '{w}'" for w in case.get("must_not_mention", []) if w.lower() in text]
    return fails


if __name__ == "__main__":
    with (
        DefaultAzureCredential() as cred,
        AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=cred) as project,
        project.get_openai_client() as oai,
    ):
        results = []
        for case in CASES:
            turn = run_turn(oai, case["input"], dispatcher=capture_dispatcher)
            fails = check(case, turn)
            results.append({"id": case["id"], "pass": not fails, "fails": fails,
                            "tool_calls": turn.tool_calls, "citations": turn.citations,
                            "answer": turn.text})
            print(f"{'PASS' if not fails else 'FAIL'}  {case['id']:<12} {'; '.join(fails)}")
        passed = sum(r["pass"] for r in results)
        print(f"\n{passed}/{len(results)} passed")
        Path("eval_results.json").write_text(json.dumps(results, indent=2))
