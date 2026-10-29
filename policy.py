"""Strict optional repository-local policy for GuardedCoder."""
from dataclasses import dataclass
import json
from typing import Optional
from pathlib import Path
import stat

from tools import MAX_SUGGESTION_FILES, ToolError, normalize_test_argv

CONFIG_NAME = ".guardedcoder.json"
MAX_CONFIG_BYTES = 16 * 1024
MAX_PATH_RULES = 100


class PolicyError(ToolError):
    """The optional repository policy is invalid or unsafe."""


@dataclass(frozen=True)
class RepositoryPolicy:
    allowed_paths: Optional[tuple] = None
    forbidden_paths: tuple = ()
    test_command: tuple = ()
    max_files_per_suggestion: int = MAX_SUGGESTION_FILES


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result


def _path_rules(value, field):
    if not isinstance(value, list) or len(value) > MAX_PATH_RULES:
        raise PolicyError(f"{field} must be a list of at most {MAX_PATH_RULES} relative paths")
    normalized = []
    for rule in value:
        if (not isinstance(rule, str) or not rule or len(rule) > 512
                or "\\" in rule or any(ch.isspace() or ord(ch) < 32 for ch in rule)):
            raise PolicyError(f"{field} contains an invalid path rule")
        is_directory = rule.endswith("/")
        relative = rule[:-1] if is_directory else rule
        parts = relative.split("/")
        if (not relative or relative.startswith("/") or any(p in {"", ".", ".."} for p in parts)
                or any(p.lower() == ".git" for p in parts)):
            raise PolicyError(f"{field} paths must be normalized repository-relative paths")
        item = relative + ("/" if is_directory else "")
        if item in normalized:
            raise PolicyError(f"{field} contains a duplicate path rule")
        normalized.append(item)
    return tuple(normalized)


def load_policy(root):
    """Load strict JSON configuration, or return safe defaults when absent."""
    path = Path(root) / CONFIG_NAME
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return RepositoryPolicy(test_command=tuple(normalize_test_argv(None)))
    except OSError:
        raise PolicyError(f"cannot inspect {CONFIG_NAME}") from None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise PolicyError(f"{CONFIG_NAME} must be a regular non-symlink file")
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise PolicyError(f"{CONFIG_NAME} exceeds the 16 KiB limit")
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError):
        raise PolicyError(f"{CONFIG_NAME} must contain valid UTF-8 JSON") from None
    if not isinstance(data, dict):
        raise PolicyError(f"{CONFIG_NAME} must contain a JSON object")
    allowed = {"allowed_paths", "forbidden_paths", "test_command", "max_files_per_suggestion"}
    unknown = set(data) - allowed
    if unknown:
        raise PolicyError(f"{CONFIG_NAME} contains unknown fields: {', '.join(sorted(unknown))}")

    allowed_paths = None
    if "allowed_paths" in data:
        allowed_paths = _path_rules(data["allowed_paths"], "allowed_paths")
    forbidden_paths = _path_rules(data.get("forbidden_paths", []), "forbidden_paths")
    try:
        command = normalize_test_argv(data.get("test_command"))
    except ToolError as exc:
        raise PolicyError(str(exc)) from None
    max_files = data.get("max_files_per_suggestion", MAX_SUGGESTION_FILES)
    if isinstance(max_files, bool) or not isinstance(max_files, int):
        raise PolicyError("max_files_per_suggestion must be an integer")
    if not 1 <= max_files <= MAX_SUGGESTION_FILES:
        raise PolicyError(f"max_files_per_suggestion must be between 1 and {MAX_SUGGESTION_FILES}")
    return RepositoryPolicy(allowed_paths, forbidden_paths, tuple(command), max_files)
