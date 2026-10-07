"""Custom function tool: hand a visitor query to a human NPWS officer.

The model only *decides* to escalate and fills in the arguments. This code runs
on our side (client-side function calling), so we control validation, storage
and where the ticket goes (queue file locally, webhook in a real deployment).
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import uuid
import urllib.request
from email.message import EmailMessage
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # read .env here so settings work however this module is imported

TOOL_NAME = "escalate_to_officer"

REASONS = [
    "safety_concern",           # non-life-threatening hazard, track damage, etc.
    "injured_or_lost_wildlife",
    "booking_or_refund_issue",
    "complaint",
    "permit_or_commercial_request",
    "not_covered_by_sources",   # agent couldn't ground an answer
    "visitor_requested_human",
]
URGENCY = ["low", "medium", "high"]

# Strict mode: every property required, no extra keys; optional = nullable.
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "enum": REASONS},
        "urgency": {"type": "string", "enum": URGENCY},
        "park_name": {"type": ["string", "null"],
                      "description": "Park or area the query is about, if known."},
        "summary": {"type": "string",
                    "description": "One or two sentences an officer can act on without reading the chat."},
        "contact": {"type": ["string", "null"],
                    "description": "Email or phone ONLY if the visitor volunteered it."},
    },
    "required": ["reason", "urgency", "park_name", "summary", "contact"],
    "additionalProperties": False,
}

TOOL_DESCRIPTION = (
    "Create a ticket for a human NSW National Parks officer. Use when the visitor asks "
    "for a person, reports a safety issue or injured wildlife, has a booking/refund problem "
    "or complaint, needs a permit decision, or when the official sources don't answer the "
    "question. Never use for life-threatening emergencies: tell the visitor to call 000."
)

QUEUE_PATH = Path(os.getenv("ESCALATION_QUEUE", "escalations.jsonl"))
WEBHOOK_URL = os.getenv("ESCALATION_WEBHOOK_URL")  # e.g. Logic App / Teams workflow

# Optional: email each ticket to an officer inbox via Gmail SMTP.
# Needs a Gmail *App Password* (Google account > Security > 2-Step Verification > App passwords),
# not your normal Gmail password.
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
OFFICER_EMAIL = os.getenv("OFFICER_EMAIL") or GMAIL_ADDRESS


class EscalationError(ValueError):
    pass


def _validate(args: dict) -> None:
    missing = [k for k in TOOL_SCHEMA["required"] if k not in args]
    if missing:
        raise EscalationError(f"missing fields: {missing}")
    if args["reason"] not in REASONS:
        raise EscalationError(f"invalid reason: {args['reason']}")
    if args["urgency"] not in URGENCY:
        raise EscalationError(f"invalid urgency: {args['urgency']}")
    if not str(args["summary"]).strip():
        raise EscalationError("summary is empty")


ETA = {"high": "within 1 business hour", "medium": "within 1 business day",
       "low": "within 3 business days"}


def new_ticket(args: dict) -> dict:
    """Validate arguments and build a ticket. Shared by the local tool and the Azure Function."""
    _validate(args)
    return {
        "ticket_id": f"NPWS-{uuid.uuid4().hex[:8].upper()}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        **{k: args[k] for k in TOOL_SCHEMA["required"]},  # drop any unexpected keys
    }


def escalate_to_officer(**args) -> dict:
    """Validate, persist and (optionally) forward the ticket. Returns what the model sees."""
    ticket = new_ticket(args)

    with QUEUE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ticket) + "\n")

    forwarded = False
    if WEBHOOK_URL:
        try:
            req = urllib.request.Request(
                WEBHOOK_URL, data=json.dumps(ticket).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            forwarded = True
        except Exception:
            forwarded = False  # ticket is still safe in the queue file

    return {"ticket_id": ticket["ticket_id"], "status": "queued", "forwarded": forwarded,
            "emailed": email_ticket(ticket), "expected_response": ETA[args["urgency"]]}


REASON_LABELS = {
    "safety_concern": "Safety concern",
    "injured_or_lost_wildlife": "Injured or lost wildlife",
    "booking_or_refund_issue": "Booking or refund",
    "complaint": "Complaint",
    "permit_or_commercial_request": "Permit request",
    "not_covered_by_sources": "Not covered by sources",
    "visitor_requested_human": "Visitor asked for a person",
}


def email_ticket(ticket: dict) -> bool:
    """Send the ticket to the officer inbox. Returns False (never raises) if not configured or it fails."""
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and OFFICER_EMAIL):
        logging.warning("Email not sent: set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env")
        return False
    reason = REASON_LABELS.get(ticket["reason"], ticket["reason"])
    msg = EmailMessage()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = OFFICER_EMAIL
    msg["Subject"] = (f"[{ticket['ticket_id']}] {ticket['urgency'].capitalize()} urgency: {reason}"
                      + (f", {ticket['park_name']}" if ticket["park_name"] else ""))
    if ticket["contact"]:
        msg["Reply-To"] = ticket["contact"] if "@" in ticket["contact"] else GMAIL_ADDRESS
    msg.set_content(
        f"Ticket: {ticket['ticket_id']}\n"
        f"Created: {ticket['created_at']}\n"
        f"Reason: {reason}\n"
        f"Urgency: {ticket['urgency']} (target reply {ETA[ticket['urgency']]})\n"
        f"Park: {ticket['park_name'] or 'not given'}\n"
        f"Visitor contact: {ticket['contact'] or 'not given'}\n\n"
        f"Summary:\n{ticket['summary']}\n\n"
        "Created by the Park Visitor Assistant (portfolio project, not affiliated with NSW National Parks)."
    )
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            smtp.send_message(msg)
        return True
    except smtplib.SMTPAuthenticationError:
        logging.warning("Email not sent: Gmail rejected the login. GMAIL_APP_PASSWORD must be a "
                        "16-character App Password, not your Gmail password.")
        return False
    except Exception as e:  # ticket is still in the queue; don't fail the conversation over email
        logging.warning("Email not sent: %s: %s", type(e).__name__, e)
        return False


def dispatch(name: str, arguments_json: str) -> str:
    """Route a model function_call to our code. Always returns a JSON string."""
    if name != TOOL_NAME:
        return json.dumps({"error": f"unknown tool {name}"})
    try:
        return json.dumps(escalate_to_officer(**json.loads(arguments_json)))
    except (EscalationError, json.JSONDecodeError, TypeError) as e:
        # Return the error to the model so it can correct itself instead of crashing.
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    import sys

    if "--test-email" in sys.argv:
        print(f"From: {GMAIL_ADDRESS or '(GMAIL_ADDRESS not set)'}")
        print(f"To:   {OFFICER_EMAIL or '(OFFICER_EMAIL not set)'}")
        print(f"App password set: {'yes' if GMAIL_APP_PASSWORD else 'no'}")
        test = {"ticket_id": "NPWS-TEST0000", "created_at": datetime.now(timezone.utc).isoformat(),
                "reason": "visitor_requested_human", "urgency": "low", "park_name": "Test park",
                "summary": "Test email from escalation.py --test-email.", "contact": None}
        print("Sent." if email_ticket(test) else "Not sent, see the warning above.")