"""Bounded model context and QUESTION/SUGGEST gates; deliberately no APPLY path."""
import difflib
import json
import re
from pathlib import Path
from tools import ToolError, safe_existing_text_file


MAX_CONTEXT_CHARS = 9000
MAX_QUERY_CHARS = 1000
MAX_FILES = 4
SNIPPET_CHARS = 2200
MAX_ITERATIONS = 1
QUESTION_TOOLS = ("list_files", "search_text", "read_file")
MAX_REPLACEMENT_CHARS = 16_000


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON field")
        obj[key] = value
    return obj


def parse_edit_proposal(raw):
    """Parse the strict model JSON schema without accepting diff-like text."""
    try:
        proposal = json.loads(raw, object_pairs_hook=_unique_object)
    except (TypeError, ValueError):
        raise ToolError("model response must be valid proposal JSON") from None
    if proposal is None:
        return None
    if not isinstance(proposal, dict):
        raise ToolError("proposal JSON must be an object")
    required = {"path", "old_text", "new_text"}
    allowed = required | {"reason"}
    if not required.issubset(proposal) or not set(proposal).issubset(allowed):
        raise ToolError("proposal JSON fields must be path, old_text, new_text, and optional reason")
    if any(not isinstance(proposal.get(key), str) for key in required):
        raise ToolError("proposal path and replacement text fields must be strings")
    if not proposal["old_text"]:
        raise ToolError("old_text must not be empty")
    if len(proposal["old_text"]) > MAX_REPLACEMENT_CHARS or len(proposal["new_text"]) > MAX_REPLACEMENT_CHARS:
        raise ToolError("proposal text exceeds the local edit size limit")
    reason = proposal.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 500:
        raise ToolError("optional reason must be a short string")
    proposal["reason"] = reason
    return proposal


def _occurrence_count(text, needle):
    count = 0
    offset = 0
    while True:
        index = text.find(needle, offset)
        if index < 0:
            return count
        count += 1
        if count > 1:
            return count
        offset = index + 1  # Count overlapping occurrences too.


def _unified_diff_with_eof_markers(original, modified, rel_path):
    lines = difflib.unified_diff(
        original.splitlines(keepends=True), modified.splitlines(keepends=True),
        fromfile="a/" + rel_path, tofile="b/" + rel_path, n=3,
    )
    output = []
    for line in lines:
        if line.startswith(("--- ", "+++ ", "@@")):
            output.append(line)
        elif line and line[0] in " +-" and not line.endswith("\n"):
            # Git requires an explicit marker plus a terminator when a file line
            # itself has no newline; otherwise the next patch control line joins it.
            output.append(line + "\n\\ No newline at end of file\n")
        else:
            output.append(line)
    return "".join(output)


def build_local_diff(root, proposal):
    """Apply an exact-once replacement in memory and produce a deterministic diff."""
    path, original = safe_existing_text_file(root, proposal["path"])
    old_text = proposal["old_text"]
    count = _occurrence_count(original, old_text)
    if count == 0:
        raise ToolError("old_text was not found exactly in the target file")
    if count != 1:
        raise ToolError("old_text must occur exactly once in the target file")
    modified = original.replace(old_text, proposal["new_text"], 1)
    if modified == original:
        raise ToolError("proposal does not change the target file")
    rel = str(path.relative_to(root))
    body = _unified_diff_with_eof_markers(original, modified, rel)
    if not body:
        raise ToolError("proposal did not produce a text diff")
    return "diff --git a/{0} b/{0}\n".format(rel) + body


class ModelModes:
    def __init__(self, assistant, provider):
        self.assistant = assistant
        self.provider = provider
        self.last_metadata = None

    def _context(self, question):
        if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUERY_CHARS:
            raise ToolError("question must be non-empty and at most 1000 characters")
        root = self.assistant.root
        calls = []
        listed = self.assistant.question("list_files", {"path": "."})
        calls.append({"tool": "list_files", "path": "."})
        files = [x for x in listed.get("files", []) if x.endswith(".py") and not x.startswith(".git/")]
        terms = list(dict.fromkeys(re.findall(r"[A-Za-z_][A-Za-z_0-9-]{2,}", question)))[:6]
        ranked = []
        for term in terms:
            result = self.assistant.question("search_text", {"query": term, "path": "."})
            calls.append({"tool": "search_text", "query": term, "path": "."})
            for match in re.finditer(r"([^\n:]+):(\d+):", result.get("output", "")):
                try:
                    path = str(Path(match.group(1)).resolve().relative_to(root))
                except (OSError, ValueError):
                    continue
                if path in files and path not in ranked:
                    ranked.append(path)
        selected = (ranked + [p for p in files if p not in ranked])[:MAX_FILES]
        chunks = []
        budget = MAX_CONTEXT_CHARS
        files_read = []
        for rel in selected:
            if budget <= 0:
                break
            result = self.assistant.question("read_file", {"path": rel})
            calls.append({"tool": "read_file", "path": rel})
            content = result.get("output", "")[:min(SNIPPET_CHARS, budget)]
            chunk = f"\n--- FILE {rel} ---\n{content}"
            chunks.append(chunk)
            budget -= len(chunk)
            files_read.append(rel)
        return "\n".join(chunks)[:MAX_CONTEXT_CHARS], files_read, calls

    def _ask(self, messages, max_tokens):
        result = self.provider.complete(messages, max_tokens=max_tokens)
        self.last_metadata = {k: result.get(k) for k in
                              ("model", "input_tokens", "output_tokens", "estimated_cost_usd", "elapsed_seconds")}
        return result

    def question(self, question):
        context, files_read, tool_calls = self._context(question)
        response = self._ask([
            {"role": "system", "content": "Answer using only supplied repository context. You cannot edit or execute commands. State uncertainty."},
            {"role": "user", "content": f"QUESTION (read-only):\n{question}\n\nREPOSITORY CONTEXT:\n{context}"},
        ], 700)
        return {"answer": response["text"], "files_read": files_read,
                "tool_calls": tool_calls, "usage": self.last_metadata}

    def suggest_fix(self, question):
        context, files_read, tool_calls = self._context(question)
        response = self._ask([
            {"role": "system", "content": "Propose one smallest localized text replacement. Return ONLY valid JSON with exactly these required string fields: path (relative path to one existing repository file), old_text (exact text that occurs once), new_text (replacement text), and optional reason (short explanation). Do not return a unified diff, Markdown fence, or text outside the JSON. Do not propose file creation, deletion, rename, or mode changes. If you cannot propose an exact replacement, return the JSON value null."},
            {"role": "user", "content": f"SUGGEST FIX (proposal only):\n{question}\n\nREPOSITORY CONTEXT:\n{context}"},
        ], 1200)
        structured = parse_edit_proposal(response["text"])
        if structured is None:
            return {"proposal": None, "reason": "model declined to propose an exact edit", "files_read": files_read,
                    "tool_calls": tool_calls, "usage": self.last_metadata}
        patch = build_local_diff(self.assistant.root, structured)
        proposal = self.assistant.suggest_fix(patch)
        return {"proposal": proposal, "files_read": files_read,
                "tool_calls": tool_calls, "usage": self.last_metadata,
                "reason": structured["reason"]}

    def apply(self, *_args, **_kwargs):
        raise ToolError("model-connected modes cannot apply patches; use the separate explicit approval flow")
