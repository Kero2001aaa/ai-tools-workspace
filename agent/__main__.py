"""Run the AI workspace agent from the command line."""
import argparse

from agent.graph import run_agent
from agent.model import load_chat_model


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the AI workspace agent (offline or configured LLM mode)."
    )
    parser.add_argument("request", help="A request to process")
    args = parser.parse_args()

    model = load_chat_model()
    result = run_agent(args.request, model=model)
    print(result["response"])


if __name__ == "__main__":
    main()
