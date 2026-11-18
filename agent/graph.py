"""A provider-free LangGraph workflow smoke test.

This is orchestration scaffolding, not an LLM-powered assistant yet.
It deliberately runs without API keys or external network services.
"""
from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class AgentState(TypedDict):
    request: str
    intent: str
    response: str


def classify_request(state: AgentState) -> dict[str, str]:
    """Assign a basic intent using transparent keyword rules."""
    request = state["request"].lower()

    if any(word in request for word in ("search", "research", "source", "latest")):
        intent = "research"
    elif any(word in request for word in ("code", "debug", "python", "program")):
        intent = "technical"
    else:
        intent = "general"

    return {"intent": intent}


def prepare_response(state: AgentState) -> dict[str, str]:
    """Return an honest workflow status; no model inference is performed."""
    intent = state["intent"]
    return {
        "response": (
            f"LangGraph workflow executed successfully. "
            f"Detected intent: {intent}. "
            "No LLM or external service was called."
        )
    }


def build_agent():
    """Build and compile the initial two-node workflow."""
    workflow = StateGraph(AgentState)
    workflow.add_node("classify", classify_request)
    workflow.add_node("prepare_response", prepare_response)
    workflow.add_edge(START, "classify")
    workflow.add_edge("classify", "prepare_response")
    workflow.add_edge("prepare_response", END)
    return workflow.compile()


def run_agent(request: str) -> AgentState:
    """Execute the workflow for one non-empty request."""
    cleaned = request.strip()
    if not cleaned:
        raise ValueError("Request must not be empty.")

    return build_agent().invoke(
        {"request": cleaned, "intent": "", "response": ""}
    )
