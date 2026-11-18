"""Run the starter agent from the command line."""
import argparse

from agent.graph import run_agent


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the provider-free AI workspace workflow."
    )
    parser.add_argument("request", help="A request to classify and process")
    args = parser.parse_args()

    result = run_agent(args.request)
    print(result["response"])


if __name__ == "__main__":
    main()
