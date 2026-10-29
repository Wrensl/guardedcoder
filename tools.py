"""Small allowlisted worker tools; never invokes a user-supplied shell command."""
from pathlib import Path
import os
import re
import stat
import subprocess
import sys

MAX_OUTPUT = 64 * 1024
MAX_PATCH = 32 * 1024
TIMEOUT = 20
MAX_SUGGESTION_FILES = 3
TOOLS = {"read_file", "search_text", "list_files", "git_status", "git_diff",
         "run_tests", "run_python_compile", "apply_patch"}


class ToolError(ValueError):
    pass


def normalize_test_argv(command):
    """Allow only Python unittest discovery; never accept a shell command."""
    if command is None:
        command = [sys.executable, "-m", "unittest", "discover", "-s", ".", "-v"]
    if (not isinstance(command, (list, tuple)) or not 4 <= len(command) <= 16
            or any(not isinstance(part, str) or not part or len(part) > 256 or "\x00" in part
                   for part in command)):
        raise ToolError("test_command must be a bounded argv array")
    if command[0] not in {"python", "python3", sys.executable}:
        raise ToolError("test_command must use Python unittest discovery")
    if list(command[1:4]) != ["-m", "unittest", "discover"]:
        raise ToolError("test_command must run python -m unittest discover")
    args = list(command[4:])
    index = 0
    seen = set()
    while index < len(args):
        option = args[index]
        if option in {"-v", "-q"}:
            if option in seen or ("-v" in seen or "-q" in seen):
                raise ToolError("test_command has conflicting verbosity options")
            seen.add(option)
            index += 1
        elif option == "-s":
            if option in seen or index + 1 >= len(args) or args[index + 1] != ".":
                raise ToolError("test_command discovery start must remain the repository root")
            seen.add(option)
            index += 2
        elif option == "-p":
            if (option in seen or index + 1 >= len(args)
                    or not re.fullmatch(r"[A-Za-z0-9_.*-]{1,128}", args[index + 1])):
                raise ToolError("test_command pattern must be a simple filename pattern")
            seen.add(option)
            index += 2
        else:
            raise ToolError("test_command contains an unsupported unittest option")
    return [sys.executable, "-m", "unittest", "discover"] + args


def _matches_path_rule(path, rule):
    if rule.endswith("/"):
        return path.startswith(rule)
    return path == rule


def enforce_path_policy(path, *, allowed_paths=None, forbidden_paths=()):
    """Apply additive user path rules; built-in path checks run separately."""
    if not isinstance(path, str) or not path:
        raise ToolError("target path must be a non-empty relative path")
    if allowed_paths is not None and not any(_matches_path_rule(path, rule) for rule in allowed_paths):
        raise ToolError("target path is outside configured allowed_paths")
    if any(_matches_path_rule(path, rule) for rule in forbidden_paths):
        raise ToolError("target path is blocked by configured forbidden_paths")


def repo_root(value):
    root = Path(value).resolve(strict=True)
    if not root.is_dir():
        raise ToolError("assigned repository is not a directory")
    return root


def safe_path(root, value, *, must_exist=False):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ToolError("invalid path")
    p = Path(value)
    if p.is_absolute():
        candidate = p
    else:
        candidate = root / p
    try:
        resolved = candidate.resolve(strict=must_exist)
        resolved.relative_to(root)
    except (OSError, ValueError, RuntimeError):
        raise ToolError("path escapes repository or cannot be resolved") from None
    # Resolve existing parents so a not-yet-created target cannot traverse a symlink.
    parent = resolved if resolved.exists() and resolved.is_dir() else resolved.parent
    try:
        parent.resolve(strict=True).relative_to(root)
    except (OSError, ValueError, RuntimeError):
        raise ToolError("path parent escapes repository") from None
    return resolved


def _run(argv, root, *, input_text=None):
    try:
        p = subprocess.run(argv, cwd=root, input=input_text, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=TIMEOUT, check=False, shell=False,
                           env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(root),
                                "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        raise ToolError(f"command timed out after {TIMEOUT}s") from None
    out = (p.stdout + ("\n[stderr]\n" + p.stderr if p.stderr else ""))[:MAX_OUTPUT]
    return {"returncode": p.returncode, "output": out, "truncated": len(p.stdout)+len(p.stderr)>MAX_OUTPUT}


_SENSITIVE_NAMES = {".env", ".netrc", "credentials", "id_rsa", "id_ed25519"}
_SENSITIVE_DIRECTORIES = {"secrets", ".ssh", ".aws"}
_SENSITIVE_SUFFIXES = {".pem", ".p12", ".key"}


def safe_existing_text_file(root, value):
    """Return a regular UTF-8 repo file, rejecting symlinks and sensitive targets."""
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
        raise ToolError("invalid target path")
    if "\\" in value or any(ord(ch) < 32 or ch.isspace() for ch in value):
        raise ToolError("target path contains unsupported characters")
    rel = Path(value)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ToolError("target must be a relative path inside the repository")
    if any(part == ".git" for part in rel.parts):
        raise ToolError("patch target is a forbidden sensitive path")
    if (any(part.lower() in _SENSITIVE_NAMES | _SENSITIVE_DIRECTORIES
            or part.lower().startswith(".env.")
            or Path(part).suffix.lower() in _SENSITIVE_SUFFIXES for part in rel.parts)):
        raise ToolError("patch target is a forbidden sensitive path")

    candidate = Path(root)
    for part in rel.parts:
        candidate = candidate / part
        try:
            mode = candidate.lstat().st_mode
        except OSError:
            raise ToolError("target file does not exist") from None
        if stat.S_ISLNK(mode):
            raise ToolError("symlink targets are not allowed")
    resolved = safe_path(root, value, must_exist=True)
    try:
        mode = resolved.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ToolError("target must be a regular file")
        data = resolved.read_bytes()
    except OSError:
        raise ToolError("target file cannot be read") from None
    if len(data) > MAX_OUTPUT:
        raise ToolError("target file exceeds the local edit size limit")
    if b"\x00" in data:
        raise ToolError("binary patch targets are not allowed")
    try:
        content = data.decode("utf-8")
    except UnicodeError:
        raise ToolError("binary patch targets are not allowed") from None
    return resolved, content


def validate_patch(root, patch, *, allowed_paths=None, forbidden_paths=()):
    """Validate a text-only existing-file unified diff without applying it."""
    if not isinstance(patch, str) or len(patch.encode()) > MAX_PATCH:
        raise ToolError("patch missing or exceeds 32 KiB")
    if any(x in patch for x in ("GIT binary patch", "new file mode", "deleted file mode", "old mode ", "new mode ", "rename from ", "rename to ", "\x00")):
        raise ToolError("binary, mode change, create/delete, rename, and NUL patches are not allowed")
    if "\r" in patch:
        raise ToolError("CR characters are not allowed in patches")
    paths=[]
    for line in patch.splitlines():
        if line.startswith(("--- ", "+++ ")):
            name=line[4:].split("\t",1)[0]
            if name == "/dev/null":
                raise ToolError("file creation/deletion is not allowed")
            if name.startswith(("a/", "b/")): name=name[2:]
            enforce_path_policy(name, allowed_paths=allowed_paths, forbidden_paths=forbidden_paths)
            p, _ = safe_existing_text_file(root, name)
            paths.append(p)
    if not paths or len(paths)%2:
        raise ToolError("patch must contain existing-file unified diff headers")
    targets=[]
    for old_path,new_path in zip(paths[0::2],paths[1::2]):
        if old_path != new_path:
            raise ToolError("patch must not rename files")
        if old_path in targets:
            raise ToolError("patch contains duplicate edit targets")
        targets.append(old_path)
    return tuple(paths)


_check_patch_paths = validate_patch

def check_patch(root, patch, *, allowed_paths=None, forbidden_paths=()):
    """Validate policy and ensure Git can apply the diff without changing files."""
    paths=validate_patch(root,patch,allowed_paths=allowed_paths,forbidden_paths=forbidden_paths)
    checked=_run(["/usr/bin/git","apply","--check","--whitespace=error","-"],root,input_text=patch)
    if checked["returncode"]:
        raise ToolError("patch check failed: "+checked["output"][:2000])
    return paths

def dispatch(root, request):
    if not isinstance(request, dict): raise ToolError("request must be an object")
    name=request.get("tool")
    if name not in TOOLS: raise ToolError("tool is not allowlisted")
    args=request.get("arguments", {})
    if not isinstance(args, dict): raise ToolError("arguments must be an object")
    if name == "read_file":
        p=safe_path(root,args.get("path"),must_exist=True)
        if not p.is_file(): raise ToolError("path is not a file")
        data=p.read_bytes()
        if len(data)>MAX_OUTPUT: data=data[:MAX_OUTPUT]
        return {"output":data.decode("utf-8",errors="replace"),"truncated":p.stat().st_size>MAX_OUTPUT}
    if name == "list_files":
        p=safe_path(root,args.get("path", "."),must_exist=True)
        if not p.is_dir(): raise ToolError("path is not a directory")
        names=sorted(str(x.relative_to(root)) for x in p.iterdir())[:2000]
        return {"files":names,"truncated":len(names)==2000}
    if name == "search_text":
        query=args.get("query")
        if not isinstance(query,str) or not query or len(query)>1000: raise ToolError("invalid query")
        target=safe_path(root,args.get("path","."),must_exist=True)
        return _run(["/usr/bin/grep","-RIn","--exclude-dir=.git","--",query,str(target)],root)
    if name == "git_status": return _run(["/usr/bin/git","status","--short","--branch"],root)
    if name == "git_diff": return _run(["/usr/bin/git","diff","--no-ext-diff","--"],root)
    if name == "run_tests": return _run(normalize_test_argv(args.get("command")),root)
    if name == "run_python_compile": return _run([sys.executable,"-m","compileall","-q","."],root)
    patch=args.get("patch")
    allowed_paths=args.get("allowed_paths")
    forbidden_paths=args.get("forbidden_paths",())
    _check_patch_paths(root,patch,allowed_paths=allowed_paths,forbidden_paths=forbidden_paths)
    checked=_run(["/usr/bin/git","apply","--check","--whitespace=error","-"],root,input_text=patch)
    if checked["returncode"]: raise ToolError("patch check failed: "+checked["output"][:2000])
    result=_run(["/usr/bin/git","apply","--whitespace=error","-"],root,input_text=patch)
    if result["returncode"]: raise ToolError("patch application failed: "+result["output"][:2000])
    return {"applied":True}
