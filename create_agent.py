"""Index official sources into a vector store and create/version the agent.

Run: python create_agent.py
Re-running creates a new agent *version* (good for A/B-ing prompt changes).
"""
import os
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import FileSearchTool, FunctionTool, PromptAgentDefinition
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from escalation import TOOL_DESCRIPTION, TOOL_NAME, TOOL_SCHEMA

load_dotenv()
ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
MODEL = os.environ["FOUNDRY_MODEL_NAME"]
AGENT_NAME = os.getenv("FOUNDRY_AGENT_NAME", "park-visitor-assistant")
KNOWLEDGE_DIR = Path("knowledge")

with (
    DefaultAzureCredential() as cred,
    AIProjectClient(endpoint=ENDPOINT, credential=cred) as project,
    project.get_openai_client() as oai,
):
    # 1. Knowledge grounding: upload official docs into a vector store
    paths = sorted(p for p in KNOWLEDGE_DIR.iterdir()
                   if p.suffix in {".pdf", ".md", ".txt", ".docx"} and not p.name.startswith("_"))
    if not paths:
        raise SystemExit("Put official source documents in ./knowledge first.")

    vs = oai.vector_stores.create(name="nsw-npws-official-sources")
    streams = [p.open("rb") for p in paths]
    try:
        batch = oai.vector_stores.file_batches.upload_and_poll(vector_store_id=vs.id, files=streams)
    finally:
        for s in streams:
            s.close()
    print(f"Vector store {vs.id}: {batch.file_counts.completed}/{len(paths)} files indexed")
    if batch.file_counts.failed:
        print("WARNING: some files failed to index; check formats.")

    # 2. Tools: hosted file search + our custom escalation function
    tools = [
        FileSearchTool(vector_store_ids=[vs.id]),
        FunctionTool(name=TOOL_NAME, description=TOOL_DESCRIPTION,
                     parameters=TOOL_SCHEMA, strict=True),
    ]

    # 3. Agent definition (versioned)
    agent = project.agents.create_version(
        agent_name=AGENT_NAME,
        definition=PromptAgentDefinition(
            model=MODEL,
            instructions=Path("instructions.md").read_text(encoding="utf-8"),
            tools=tools,
        ),
        description="Answers NSW park visitor queries from official sources; escalates to officers.",
    )
    print(f"Agent {agent.name} version {agent.version} created")
