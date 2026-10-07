"""Add the escalate_to_officer function tool to an agent you built in the Foundry portal.

Reads the agent's latest version, keeps its model, instructions and File Search tool
exactly as they are, appends the escalation tool (and an escalation paragraph in the
instructions), and saves it as a NEW version. Your portal version stays untouched.

Run: python add_escalation_tool.py
"""
import os

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import FunctionTool, PromptAgentDefinition
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from escalation import TOOL_DESCRIPTION, TOOL_NAME, TOOL_SCHEMA

load_dotenv()
AGENT_NAME = os.getenv("FOUNDRY_AGENT_NAME", "park-visitor-assistant")

ESCALATION_RULES = """

Escalation
- Life-threatening emergency: tell the visitor to call 000 immediately. Do not create a ticket instead.
- Call escalate_to_officer when the visitor asks for a person, reports a non-urgent hazard or injured
  wildlife, has a booking/refund problem or complaint, needs a permit decision, or when the official
  sources don't answer the question.
- Ask for the park name if unknown. Only include contact details the visitor volunteered.
- After the tool returns, give the visitor the ticket ID and expected response time."""

with (
    DefaultAzureCredential() as cred,
    AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=cred) as project,
):
    current = project.agents.get(agent_name=AGENT_NAME).versions.latest
    d = current.definition
    print(f"Current: {AGENT_NAME} v{current.version}, model={d.model}, tools={[t.type for t in d.tools or []]}")

    tools = [t for t in (d.tools or []) if getattr(t, "name", None) != TOOL_NAME]  # idempotent
    tools.append(FunctionTool(name=TOOL_NAME, description=TOOL_DESCRIPTION,
                              parameters=TOOL_SCHEMA, strict=True))

    instructions = d.instructions or ""
    if TOOL_NAME not in instructions:
        instructions += ESCALATION_RULES

    new = project.agents.create_version(
        agent_name=AGENT_NAME,
        definition=PromptAgentDefinition(model=d.model, instructions=instructions, tools=tools),
        description=(current.description or "") + " (+ escalation tool)",
    )
    print(f"Created {AGENT_NAME} v{new.version}. Refresh the portal to see it.")
