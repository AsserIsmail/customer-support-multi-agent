"""Local conversation CLI. All retrieval is performed through an MCP subprocess."""

import argparse
import asyncio
import json
import uuid

from support_ai.agent_models import OpenAIDecisions
from support_ai.agents import SupportAssistant
from support_ai.config import Settings, configure_logging
from support_ai.mcp_client import connect_support_tools


async def run_chat(questions: list[str]) -> None:
    configure_logging(Settings.from_env().log_level)
    model = OpenAIDecisions()
    async with connect_support_tools() as tools:
        assistant = SupportAssistant(tools, model)
        thread = uuid.uuid4().hex
        if questions:
            for question in questions:
                print(json.dumps({"question": question, **await assistant.ask(question, thread_id=thread)}, indent=2), flush=True)
            return
        print("Support chat. Type /quit to exit or /new to clear the conversation.")
        while True:
            try:
                question = await asyncio.to_thread(input, "You: ")
            except EOFError:
                break
            if question.strip() == "/quit":
                break
            if question.strip() == "/new":
                await assistant.forget(thread)
                thread = uuid.uuid4().hex
                print("Started a new conversation.")
                continue
            if not question.strip():
                continue
            try:
                result = await assistant.ask(question, thread_id=thread)
                print("Assistant:", result["answer"])
            except ValueError as exc:
                print(str(exc))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", action="append", default=[], help="Repeat for follow-ups in the same conversation")
    args = parser.parse_args()
    try:
        asyncio.run(run_chat(args.question))
    except KeyboardInterrupt:
        pass
    except Exception:
        parser.exit(1, "Chat could not start. Check your API key, model, MCP setup, and application logs.\n")


if __name__ == "__main__":
    main()
