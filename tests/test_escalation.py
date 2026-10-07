import json

import pytest

import escalation

VALID = {"reason": "injured_or_lost_wildlife", "urgency": "medium", "park_name": "Royal National Park",
         "summary": "Visitor found an injured wallaby near the Wattamolla car park.", "contact": None}


@pytest.fixture(autouse=True)
def tmp_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(escalation, "QUEUE_PATH", tmp_path / "q.jsonl")
    monkeypatch.setattr(escalation, "WEBHOOK_URL", None)
    monkeypatch.setattr(escalation, "GMAIL_ADDRESS", None)
    monkeypatch.setattr(escalation, "GMAIL_APP_PASSWORD", None)


def test_schema_is_strict_mode_compatible():
    s = escalation.TOOL_SCHEMA
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])


def test_valid_call_creates_ticket():
    out = escalation.escalate_to_officer(**VALID)
    assert out["ticket_id"].startswith("NPWS-")
    assert out["expected_response"] == "within 1 business day"
    saved = json.loads(escalation.QUEUE_PATH.read_text().strip())
    assert saved["park_name"] == "Royal National Park"


@pytest.mark.parametrize("bad", [{"reason": "chat"}, {"urgency": "urgent"}, {"summary": "  "}])
def test_invalid_args_return_error_to_model(bad):
    out = json.loads(escalation.dispatch(escalation.TOOL_NAME, json.dumps({**VALID, **bad})))
    assert "error" in out
    assert not escalation.QUEUE_PATH.exists()


def test_unknown_tool_and_bad_json():
    assert "error" in json.loads(escalation.dispatch("delete_park", "{}"))
    assert "error" in json.loads(escalation.dispatch(escalation.TOOL_NAME, "{not json"))


def test_webhook_failure_still_queues(monkeypatch):
    monkeypatch.setattr(escalation, "WEBHOOK_URL", "http://127.0.0.1:9/nowhere")
    out = escalation.escalate_to_officer(**VALID)
    assert out["forwarded"] is False
    assert escalation.QUEUE_PATH.exists()


class FakeSMTP:
    sent = []
    fail = False

    def __init__(self, host, port, timeout):
        assert (host, port) == ("smtp.gmail.com", 465)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        if FakeSMTP.fail:
            raise OSError("auth failed")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def gmail(monkeypatch):
    FakeSMTP.sent, FakeSMTP.fail = [], False
    monkeypatch.setattr(escalation, "GMAIL_ADDRESS", "officer.demo@gmail.com")
    monkeypatch.setattr(escalation, "GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setattr(escalation, "OFFICER_EMAIL", "officer.demo@gmail.com")
    monkeypatch.setattr(escalation.smtplib, "SMTP_SSL", FakeSMTP)


def test_ticket_is_emailed(gmail):
    out = escalation.escalate_to_officer(**VALID)
    assert out["emailed"] is True
    msg = FakeSMTP.sent[0]
    assert out["ticket_id"] in msg["Subject"] and "Royal National Park" in msg["Subject"]
    assert "injured wallaby" in msg.get_content()


def test_email_failure_still_queues(gmail):
    FakeSMTP.fail = True
    out = escalation.escalate_to_officer(**VALID)
    assert out["emailed"] is False
    assert escalation.QUEUE_PATH.exists()


def test_no_email_when_not_configured():
    assert escalation.escalate_to_officer(**VALID)["emailed"] is False