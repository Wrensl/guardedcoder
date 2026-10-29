"""Interactive command line for the non-autonomous GuardedCoder workflow."""
import argparse
import json
import os
from pathlib import Path

from assistant import NonAutonomousAssistant
from model_modes import ModelModes, ModelResponseError
from policy import PolicyError
from provider import OpenAICompatibleProvider, ProviderConfig, ProviderError
from tools import ToolError


def model_modes(app):
    def price(name):
        raw = os.environ.get(name)
        return None if raw is None else float(raw)

    base_url = os.environ.get("LEVEL2_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("LEVEL2_MODEL", "gpt-6-luna")
    default_prices = base_url.rstrip("/") == "https://api.openai.com/v1" and model == "gpt-6-luna"
    config = ProviderConfig(
        base_url=base_url,
        model=model,
        api_key_env=os.environ.get("LEVEL2_API_KEY_ENV", "LEVEL2_API_KEY"),
        timeout=float(os.environ.get("LEVEL2_TIMEOUT", "45")),
        input_usd_per_million=price("LEVEL2_INPUT_USD_PER_MILLION") if os.environ.get("LEVEL2_INPUT_USD_PER_MILLION") else (.10 if default_prices else None),
        output_usd_per_million=price("LEVEL2_OUTPUT_USD_PER_MILLION") if os.environ.get("LEVEL2_OUTPUT_USD_PER_MILLION") else (.50 if default_prices else None),
    )
    return ModelModes(app, OpenAICompatibleProvider(config))


def _provider_error(exc):
    message = str(exc)
    if "environment variable" in message and "is not set" in message:
        variable = message.split("variable ", 1)[1].split(" is not set", 1)[0]
        print(f"Missing API key: set {variable} in your environment.")
    elif "invalid chat-completions response" in message or "content was not text" in message:
        print("Invalid model response: the provider did not return a usable text response.")
    else:
        print("Provider/network failure: the request could not be completed. Check provider settings and connectivity.")


def _suggestion_error(exc):
    print(f"Suggestion rejected: {exc}")


def _select_repository(argument):
    selected = argument
    if selected is None:
        selected = input("Repository path (Git working tree; blank to quit): ").strip()
        if not selected:
            return None
    try:
        return NonAutonomousAssistant(Path(selected).expanduser())
    except PolicyError as exc:
        print(f"Invalid repository configuration: {exc}")
    except ToolError as exc:
        message = str(exc)
        if message.startswith("not a Git repository"):
            print(message)
        else:
            print(f"Invalid repository path: {message}")
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="GuardedCoder: human-approved repository suggestions")
    parser.add_argument("repository", nargs="?", help="Git repository path (prompted when omitted)")
    args = parser.parse_args(argv)
    app = _select_repository(args.repository)
    if app is None:
        return 0
    if app is False:
        return 2
    print("Repository:", app.root)
    while True:
        mode = input("Mode [question/suggest/apply/quit]: ").strip().lower()
        if mode == "quit":
            return 0
        try:
            if mode == "question":
                prompt = input("Question: ").strip()
                try:
                    result = model_modes(app).question(prompt)
                except ProviderError as exc:
                    _provider_error(exc)
                    continue
                print(json.dumps(result, indent=2))
            elif mode == "suggest":
                prompt = input("Requested fix: ").strip()
                try:
                    result = model_modes(app).suggest_fix(prompt)
                except ProviderError as exc:
                    _provider_error(exc)
                    continue
                except ModelResponseError:
                    print("Invalid model response: expected a structured JSON edit proposal.")
                    continue
                except ToolError as exc:
                    _suggestion_error(exc)
                    continue
                if result["proposal"] is None:
                    print(result["reason"])
                else:
                    print(result["proposal"]["diff"])
                    print("Validated proposal only; no files were changed.")
                    print("Approval token:", result["proposal"]["approval_token"])
                    if result["reason"]:
                        print("Reason:", result["reason"])
                print("Files read:", ", ".join(result["files_read"]))
                print("Tool calls:", json.dumps(result["tool_calls"]))
                print("Usage:", json.dumps(result["usage"]))
            elif mode == "apply":
                token = input("Approval token: ").strip()
                if input("Type APPLY to approve this patch: ").strip() != "APPLY":
                    print("Not applied")
                    continue
                try:
                    result = app.apply_approved(token, approved=True)
                except ToolError as exc:
                    print(f"Apply rejected: {exc}")
                    continue
                print(json.dumps(result, indent=2))
            else:
                print("Unknown mode. Choose question, suggest, apply, or quit.")
        except (ValueError, OSError) as exc:
            print(f"Unable to complete that action: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
