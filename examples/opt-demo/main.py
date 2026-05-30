"""
Opt-Demo — CLI
==============
Run the orchestrator from the command line.

Usage:
    python main.py --input "search for quantum computing news"
    python main.py --input "find info on Federer and summarize in 20 words"
    python main.py --input "search climate change, summarize 15 words, translate to French"
    python main.py --interactive
"""

import argparse
import os
import sys

from dotenv import load_dotenv

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
load_dotenv(os.path.join(_here, ".env"))


def main():
    parser = argparse.ArgumentParser(description="Opt-Demo CLI")
    parser.add_argument("--input", "-i", type=str, help="Query to send to the orchestrator")
    parser.add_argument("--interactive", action="store_true", help="Interactive chat mode")
    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY is not set. Add it to a .env file.")
        sys.exit(1)

    from runner import run_agent

    if args.interactive:
        print("🤖 Opt-Demo — interactive mode (type 'exit' to quit)\n")
        while True:
            try:
                user_input = input("You> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nGoodbye!")
                break
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                print("Goodbye!")
                break
            response, trace_id = run_agent(user_input=user_input)
            print(f"\n🤖 {response}")
            if trace_id:
                print(f"   [trace: {trace_id}]\n")
            else:
                print()

    elif args.input:
        print(f"Input: {args.input}")
        print("-" * 60)
        response, trace_id = run_agent(user_input=args.input)
        print(response)
        if trace_id:
            print(f"\n[trace: {trace_id}]")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
