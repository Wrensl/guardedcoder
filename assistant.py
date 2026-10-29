"""GuardedCoder approval gates, separate from provider access and model modes."""
import hashlib
import secrets
from pathlib import Path
from tools import ToolError, check_patch, dispatch, repo_root, validate_patch

QUESTION_TOOLS = frozenset({"read_file", "search_text", "list_files"})

class NonAutonomousAssistant:
    def __init__(self, repository=None):
        # The selected root is the entire authority boundary for local tools.
        self.root=repo_root(Path.cwd() if repository is None else repository)
        if not (self.root / ".git").exists():
            raise ToolError("assigned path must be a Git working-tree root")
        self._pending={}

    def question(self, tool, arguments):
        if tool not in QUESTION_TOOLS:
            raise ToolError("QUESTION mode is read-only; tool is unavailable")
        return dispatch(self.root,{"tool":tool,"arguments":arguments})

    def suggest_fix(self, patch, *, allowed_prefixes=("",)):
        paths=check_patch(self.root,patch,allowed_prefixes=allowed_prefixes)
        token=secrets.token_urlsafe(18)
        self._pending[token]=(hashlib.sha256(patch.encode()).hexdigest(),patch,allowed_prefixes)
        return {"approval_token":token,"files":[str(x.relative_to(self.root)) for x in paths],"diff":patch}

    def apply_approved(self, token, *, approved=False):
        if approved is not True:
            raise ToolError("explicit approval is required")
        try: digest,patch,prefixes=self._pending.pop(token)
        except KeyError: raise ToolError("unknown or already used approval token") from None
        if hashlib.sha256(patch.encode()).hexdigest()!=digest:
            raise ToolError("approved patch integrity check failed")
        validate_patch(self.root,patch,allowed_prefixes=prefixes)
        applied=dispatch(self.root,{"tool":"apply_patch","arguments":{"patch":patch}})
        tests=dispatch(self.root,{"tool":"run_tests","arguments":{}})
        diff=dispatch(self.root,{"tool":"git_diff","arguments":{}})
        return {"applied":applied,"tests":tests,"diff":diff}
