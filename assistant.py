"""GuardedCoder approval gates, separate from provider access and model modes."""
import hashlib
import secrets
from pathlib import Path
from policy import load_policy
from tools import ToolError, check_patch, dispatch, repo_root

QUESTION_TOOLS = frozenset({"read_file", "search_text", "list_files"})

class NonAutonomousAssistant:
    def __init__(self, repository=None):
        # The selected root is the entire authority boundary for local tools.
        selected = Path.cwd() if repository is None else Path(repository).expanduser()
        try:
            self.root=repo_root(selected)
        except (OSError, ValueError, RuntimeError):
            raise ToolError("invalid repository path: directory does not exist or cannot be accessed") from None
        if not self.root.is_dir():
            raise ToolError("invalid repository path: expected a directory")
        if not (self.root / ".git").exists():
            raise ToolError("not a Git repository: select the working-tree root")
        self.policy = load_policy(self.root)
        self._pending={}

    def question(self, tool, arguments):
        if tool not in QUESTION_TOOLS:
            raise ToolError("QUESTION mode is read-only; tool is unavailable")
        return dispatch(self.root,{"tool":tool,"arguments":arguments})

    def suggest_fix(self, patch):
        paths=check_patch(self.root,patch,allowed_paths=self.policy.allowed_paths,
                          forbidden_paths=self.policy.forbidden_paths)
        unique_paths=tuple(dict.fromkeys(paths))
        if len(unique_paths) > self.policy.max_files_per_suggestion:
            raise ToolError("suggestion rejected: exceeds configured max_files_per_suggestion")
        token=secrets.token_urlsafe(18)
        self._pending[token]=(hashlib.sha256(patch.encode()).hexdigest(),patch,self.policy)
        return {"approval_token":token,"files":[str(x.relative_to(self.root)) for x in unique_paths],"diff":patch}

    def apply_approved(self, token, *, approved=False):
        if approved is not True:
            raise ToolError("explicit approval is required")
        try: digest,patch,policy=self._pending.pop(token)
        except KeyError: raise ToolError("unknown or already used approval token") from None
        if hashlib.sha256(patch.encode()).hexdigest()!=digest:
            raise ToolError("approved patch integrity check failed")
        check_patch(self.root,patch,allowed_paths=policy.allowed_paths,
                    forbidden_paths=policy.forbidden_paths)
        applied=dispatch(self.root,{"tool":"apply_patch","arguments":{
            "patch":patch, "allowed_paths":policy.allowed_paths,
            "forbidden_paths":policy.forbidden_paths,
        }})
        tests=dispatch(self.root,{"tool":"run_tests","arguments":{"command":list(policy.test_command)}})
        diff=dispatch(self.root,{"tool":"git_diff","arguments":{}})
        return {"applied":applied,"tests":tests,"diff":diff}
