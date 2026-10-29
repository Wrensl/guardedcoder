"""Strict local repository policy tests; never use the network or a model."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from assistant import NonAutonomousAssistant
from model_modes import build_local_diff
from policy import PolicyError, load_policy
from tools import ToolError, dispatch, normalize_test_argv


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="guardedcoder-policy-")
        self.root = Path(self.temp.name).resolve()
        subprocess.run(["/usr/bin/git", "init", "-q"], cwd=self.root, check=True)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src" / "mod.py").write_text("VALUE = 1\n")
        (self.root / "src" / "blocked.py").write_text("VALUE = 8\n")
        (self.root / "tests" / "test_mod.py").write_text("import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n")
        subprocess.run(["/usr/bin/git", "add", "src", "tests"], cwd=self.root, check=True)
        subprocess.run(["/usr/bin/git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "baseline"], cwd=self.root, check=True)

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, content):
        (self.root / ".guardedcoder.json").write_text(content)

    def test_config_absent_uses_safe_defaults(self):
        policy = load_policy(self.root)
        self.assertIsNone(policy.allowed_paths)
        self.assertEqual(policy.forbidden_paths, ())
        self.assertEqual(policy.max_files_per_suggestion, 3)
        self.assertIn("unittest", policy.test_command)

    def test_valid_config_and_max_file_limit(self):
        self.write_config(json.dumps({
            "allowed_paths": ["src/", "tests/"],
            "forbidden_paths": ["src/generated.py"],
            "test_command": ["python3", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py", "-v"],
            "max_files_per_suggestion": 2,
        }))
        policy = load_policy(self.root)
        self.assertEqual(policy.allowed_paths, ("src/", "tests/"))
        self.assertEqual(policy.max_files_per_suggestion, 2)
        self.assertEqual(policy.test_command[1:4], ("-m", "unittest", "discover"))

    def test_malformed_unknown_and_unsafe_config_rejected(self):
        for value in ("{bad", "[]", '{"unexpected": true}',
                      json.dumps({"test_command": "python -m unittest"}),
                      json.dumps({"test_command": ["python3", "-c", "print(1)", "x"]}),
                      json.dumps({"max_files_per_suggestion": 4}),
                      '{"allowed_paths": [], "allowed_paths": ["src/"]}'):
            with self.subTest(value=value):
                self.write_config(value)
                with self.assertRaises(PolicyError):
                    load_policy(self.root)

    def test_allowed_and_forbidden_paths_enforced_for_suggestions(self):
        self.write_config(json.dumps({"allowed_paths": ["src/"], "forbidden_paths": ["src/blocked.py"]}))
        app = NonAutonomousAssistant(self.root)
        allowed = build_local_diff(self.root, {"path": "src/mod.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
                                   allowed_paths=app.policy.allowed_paths, forbidden_paths=app.policy.forbidden_paths)
        app.suggest_fix(allowed)
        outside = build_local_diff(self.root, {"path": "tests/test_mod.py", "old_text": "assertTrue(True)", "new_text": "assertTrue(1)"})
        with self.assertRaisesRegex(ToolError, "allowed_paths"):
            app.suggest_fix(outside)
        blocked = build_local_diff(self.root, {"path": "src/blocked.py", "old_text": "VALUE = 8", "new_text": "VALUE = 9"})
        with self.assertRaisesRegex(ToolError, "forbidden_paths"):
            app.suggest_fix(blocked)

    def test_builtin_sensitive_rules_cannot_be_overridden(self):
        (self.root / ".env").write_text("TOKEN=fake\n")
        self.write_config(json.dumps({"allowed_paths": [".env"], "forbidden_paths": []}))
        app = NonAutonomousAssistant(self.root)
        with self.assertRaisesRegex(ToolError, "sensitive"):
            build_local_diff(self.root, {"path": ".env", "old_text": "TOKEN=fake", "new_text": "TOKEN=other"},
                             allowed_paths=app.policy.allowed_paths, forbidden_paths=app.policy.forbidden_paths)
        (self.root / ".env").unlink()

    def test_apply_uses_the_validated_policy_snapshot(self):
        self.write_config(json.dumps({"allowed_paths": ["src/"]}))
        app = NonAutonomousAssistant(self.root)
        patch_text = build_local_diff(self.root, {
            "path": "src/mod.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"
        }, allowed_paths=app.policy.allowed_paths, forbidden_paths=app.policy.forbidden_paths)
        suggestion = app.suggest_fix(patch_text)
        # Changing the config after SUGGEST cannot widen the stored approval's scope.
        self.write_config(json.dumps({"allowed_paths": ["tests/"]}))
        result = app.apply_approved(suggestion["approval_token"], approved=True)
        self.assertEqual(result["applied"], {"applied": True})
        self.assertEqual((self.root / "src" / "mod.py").read_text(), "VALUE = 2\n")

    def test_safe_test_argv_is_used_without_shell(self):
        command = ["python3", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py", "-v"]
        normalized = normalize_test_argv(command)
        self.assertEqual(normalized[1:4], ["-m", "unittest", "discover"])
        with patch("tools.subprocess.run", wraps=subprocess.run) as run:
            result = dispatch(self.root, {"tool": "run_tests", "arguments": {"command": command}})
        self.assertEqual(result["returncode"], 0)
        self.assertIsInstance(run.call_args.args[0], list)
        self.assertIs(run.call_args.kwargs["shell"], False)
        with self.assertRaises(ToolError):
            normalize_test_argv(["python3", "-c", "print('unsafe')", "x"])

    def test_config_symlink_is_rejected(self):
        other = self.root / "other.json"
        other.write_text("{}")
        (self.root / ".guardedcoder.json").symlink_to(other)
        with self.assertRaisesRegex(PolicyError, "non-symlink"):
            load_policy(self.root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
