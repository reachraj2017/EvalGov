"""
HTTP API for Opt-Demo.

Wraps run_agent() behind a simple FastAPI endpoint so the Eval UI
(or any external caller) can send prompts programmatically.

Start:
    uvicorn api:app --host 0.0.0.0 --port 8080 --reload

Endpoints:
    GET  /health            → liveness probe
    POST /chat              → run agent, return response + trace_id
"""

import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from runner import run_agent

app = FastAPI(
    title="Opt-Demo API",
    description="HTTP endpoint for the opt-demo agent system",
    version="1.0.0",
)


class ChatRequest(BaseModel):
    message: str
    user_id: str = "default"
    run_id: str | None = None           # group this call under a specific eval run
    source: str = "benchmark"           # "benchmark" | "production" | "exploratory"
    conversation_id: str | None = None  # groups turns for multi-turn eval


class ChatResponse(BaseModel):
    response: str
    trace_id: str
    run_id: str | None = None
    source: str = "benchmark"


@app.get("/health")
def health():
    return {"status": "ok", "service": "opt-demo-api"}


@app.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest):
    """
    Send a message to the multi-agent system and return the response.

    Optionally pass run_id to tag all spans from this call with a specific
    eval run, enabling the Eval UI to group and score this execution.
    """
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message cannot be empty")
    try:
        response, trace_id = run_agent(
            user_input=body.message,
            user_id=body.user_id,
            run_id=body.run_id,
            source=body.source,
            conversation_id=body.conversation_id,
        )
        return ChatResponse(response=response, trace_id=trace_id, run_id=body.run_id, source=body.source)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
