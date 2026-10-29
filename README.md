# GuardedCoder

Human-approved AI coding assistant that analyzes repositories, proposes validated code changes, and applies them only after explicit approval.

GuardedCoder is intentionally **non-autonomous**. The model cannot run shell commands, write files, or apply a patch.

## What it is

AI thinks and proposes. Local deterministic code validates. A human decides whether anything gets applied.

GuardedCoder provides a middle ground between read-only AI assistants and highly autonomous coding agents with broad shell and write access. It gathers a small amount of relevant repository context, asks a hosted OpenAI-compatible model for an answer or structured text edits, and keeps all file-changing actions behind a separate approval step.

## Core workflow

```text
QUESTION → SUGGEST → local validation → approval token → explicit APPLY → tests → diff
```

- **QUESTION** sends bounded, relevant repository context for an explanation. It does not write.
- **SUGGEST** asks for a JSON list of edits, each containing an existing file path, exact old text, exact new text, and optional reason. Local code requires each old-text value to occur exactly once, creates a combined unified diff, and validates it. It does not write. Suggestions are limited to three files by default.
- **APPLY** requires the proposal's approval token and a separate explicit `APPLY` confirmation. Local code applies the validated diff, runs the repository's tests, and shows the resulting diff.

QUESTION and SUGGEST send the prompt and bounded selected source snippets to the configured model provider. Use them only with code you are allowed to share with that provider.

## Safety model

- There is no arbitrary model-controlled shell and no model-driven tool loop.
- QUESTION and SUGGEST do not write repository files.
- SUGGEST accepts structured text replacements, not model-generated diffs.
- Local code generates the diff and checks it with `git apply --check`.
- Paths are restricted to the selected repository; symlink targets and sensitive paths are rejected.
- Binary files are rejected. The current edit flow does not create, delete, rename, or change modes on files.
- Applying requires a single-use approval token plus the user's explicit confirmation.
- Tests run after APPLY. A test failure does not automatically revert the approved edit; review the diff and restore files if needed.

These controls reduce risk; GuardedCoder is **not a formal security sandbox**. It runs as the current user, and tests execute code from the selected repository. Start with a disposable or non-critical Git repository.

## Requirements

- Python 3.9 or later
- Git
- An OpenAI-compatible chat-completions API endpoint and API key for QUESTION/SUGGEST
- No third-party Python packages are required

## Installation

```sh
git clone https://github.com/OWNER/guardedcoder.git
cd guardedcoder
python3 -m venv .venv
source .venv/bin/activate
```

The implementation uses Python's standard library. The local test suite makes no provider/API requests.

## Configuration

Set the API key in the environment; never commit it or put it in a project file.

Provide `LEVEL2_API_KEY` through your shell's secure environment or secret manager before starting GuardedCoder. Do not put its value in this repository, a prompt, or shell history.

Supported environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `LEVEL2_API_KEY` | — | API credential; sent only to the configured provider |
| `LEVEL2_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API base URL; remote endpoints must use HTTPS |
| `LEVEL2_MODEL` | `gpt-6-luna` | Provider model name |
| `LEVEL2_TIMEOUT` | `45` | Request timeout in seconds (1–180) |
| `LEVEL2_API_KEY_ENV` | `LEVEL2_API_KEY` | Name of the environment variable containing the API key |
| `LEVEL2_INPUT_USD_PER_MILLION` | Model default when using default OpenAI GPT-6 Luna; otherwise unset | Optional input-token price for estimates |
| `LEVEL2_OUTPUT_USD_PER_MILLION` | Model default when using default OpenAI GPT-6 Luna; otherwise unset | Optional output-token price for estimates |

Both pricing variables must be set together. Estimates use provider-reported token usage and are not billing guarantees.

### Optional repository policy

A repository may define `.guardedcoder.json`. This is a repository-specific policy for the repository GuardedCoder is working on, not GuardedCoder’s global configuration. GuardedCoder works without it. To use the included example, copy `.guardedcoder.example.json` into the target repository as `.guardedcoder.json`, then adapt the paths and test settings for that repository. The example file is not active configuration. Unknown fields and invalid values are rejected.

`allowed_paths` restricts suggestion and apply targets. `forbidden_paths` adds restrictions; neither setting can disable GuardedCoder's built-in protections. The file limit defaults to 3 and may be lowered, but cannot be raised. `test_command` must be an argv array and is deliberately limited to Python `unittest discover` with a repository-root start, optional filename pattern, and verbosity flag. It is never interpreted by a shell. Sensitive files, `.git`, symlinks, binary targets, and file creation/deletion/rename/mode changes remain prohibited regardless of policy settings.

## Usage

Start with a repository path:

```sh
python3 assistant_cli.py /path/to/repository
```

If no path is supplied, GuardedCoder prompts for one. Choose a mode:

1. **question** — ask a question about the repository. The assistant searches and reads bounded context, then returns an explanation.
2. **suggest** — describe a small fix. Review the validated diff and its approval token. No file is changed.
3. **apply** — enter that token, then type `APPLY` at the confirmation prompt. The patch is applied, tests run, and the diff is shown.
4. **quit** — exit.

Example: for a clamp function whose upper-bound test fails, ask QUESTION why it fails. Then request SUGGEST FIX for the smallest correction. Inspect the proposed diff. Only choose APPLY if you approve that exact proposal.

Approval tokens are held in memory for the current CLI session and are single-use. They do not replace reviewing the diff before approval.

## Development and tests

Run the full local test suite:

```sh
python3 -m unittest discover -s . -v
```

Tests use temporary Git repositories and mocked provider responses. They do not require an API key or make network requests.

The pre-release CI gate also runs a Python version matrix, compile and whitespace checks, a redacted Gitleaks scan of repository contents and history, local Semgrep security rules, and CLI/policy smoke checks. To run the scanners locally, install the corresponding tools and run:

```sh
gitleaks git . --redact=100 --no-banner
semgrep scan --config .semgrep.yml --error --metrics=off .
```

The local Semgrep command uses the checked-in rules and does not require cloud login. Gitleaks needs a full Git checkout to scan the complete history. See [RELEASING.md](RELEASING.md) for the release checklist. A release should be created only from a commit with a green **Pre-release safety gate** run.

## Limitations

- Context collection is intentionally bounded and may omit relevant code in larger repositories.
- A SUGGEST proposal is limited to three exact text replacements across existing UTF-8 text files by default; every old-text value must occur exactly once. Optional policy can lower this bound.
- Changes that Git cannot validate, including some newline-format edge cases, are rejected.
- Tests are run with a bounded Python `unittest discover` argv. Projects using other test runners need adaptation; arbitrary test commands are not supported.
- APPLY does not automatically roll back when tests fail.

## Security

Do not expose API keys, credentials, private keys, or secrets in prompts or repository context. Use a disposable or non-critical repository first, review every proposed diff, and understand that project tests execute locally with your user permissions.

## License

MIT. See [LICENSE](LICENSE).
