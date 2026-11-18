"""LangGraph workflow with an optional, injected chat model.

Without a model, the workflow remains fully offline and makes no API calls.
"""
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
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


def build_agent(model=None):
    """Build the workflow, optionally injecting a LangChain chat model."""

    def generate_response(state: AgentState) -> dict[str, str]:
        if model is None:
            return {
                "response": (
                    "Offline mode: request classified as "
                    f"{state['intent']}. No LLM or external service was called. "
                    "Configure MODEL_API_KEY and MODEL_NAME to enable inference."
                )
            }

        messages = [
            SystemMessage(
                content=(
                    "You are the assistant for a technical AI workspace. "
                    "Answer accurately, state uncertainty, and do not claim "
                    "to have used tools that were not provided."
                )
            ),
            HumanMessage(
                content=(
                    f"Detected intent: {state['intent']}\n"
                    f"User request: {state['request']}"
                )
            ),
        ]
        result = model.invoke(messages)
        content = getattr(result, "content", result)
        response = content if isinstance(content, str) else str(content)
        return {"response": response}

    workflow = StateGraph(AgentState)
    workflow.add_node("classify", classify_request)
    workflow.add_node("generate_response", generate_response)
    workflow.add_edge(START, "classify")
    workflow.add_edge("classify", "generate_response")
    workflow.add_edge("generate_response", END)
    return workflow.compile()


def run_agent(request: str, model=None) -> AgentState:
    """Execute the workflow for one non-empty request."""
    cleaned = request.strip()
    if not cleaned:
        raise ValueError("Request must not be empty.")

    return build_agent(model=model).invoke(
        {"request": cleaned, "intent": "", "response": ""}
    )
