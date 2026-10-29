"""CLI argument and interactive fallback tests; no provider calls are made."""
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import assistant_cli
from model_modes import ModelResponseError
from provider import ProviderError
from tools import ToolError


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="guardedcoder-cli-")
        self.repo = Path(self.temp.name).resolve()
        subprocess.run(["/usr/bin/git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "sample.py").write_text("VALUE = 1\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_repository_argument_is_accepted(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=["quit"]), patch("sys.stdout", output):
            self.assertEqual(assistant_cli.main([str(self.repo)]), 0)
        self.assertIn(str(self.repo), output.getvalue())

    def test_no_argument_uses_interactive_repository_prompt(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=[str(self.repo), "quit"]) as prompt, patch("sys.stdout", output):
            self.assertEqual(assistant_cli.main([]), 0)
        self.assertIn("Repository path", prompt.call_args_list[0].args[0])
        self.assertIn("Repository:", output.getvalue())

    def test_invalid_path_and_non_git_path_have_clear_messages(self):
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(assistant_cli.main([str(self.repo / "missing")]), 2)
        self.assertIn("Invalid repository path", output.getvalue())
        plain = self.repo / "plain"
        plain.mkdir()
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(assistant_cli.main([str(plain)]), 2)
        self.assertIn("not a Git repository", output.getvalue())

    def test_question_and_suggest_modes_are_retained(self):
        class FakeModes:
            def __init__(self): self.invoked = []
            def question(self, text):
                self.invoked.append(("question", text))
                return {"answer": "read only"}
            def suggest_fix(self, text):
                self.invoked.append(("suggest", text))
                return {"proposal": None, "reason": "declined", "files_read": [], "tool_calls": [], "usage": {}}
        modes = FakeModes()
        output = io.StringIO()
        with patch("assistant_cli.model_modes", return_value=modes), \
             patch("builtins.input", side_effect=["question", "explain", "suggest", "propose", "quit"]), \
             patch("sys.stdout", output):
            self.assertEqual(assistant_cli.main([str(self.repo)]), 0)
        self.assertEqual(modes.invoked, [("question", "explain"), ("suggest", "propose")])
        self.assertIn("declined", output.getvalue())

    def test_missing_key_provider_and_invalid_response_errors_are_clear(self):
        cases = (
            (ProviderError("API key environment variable LEVEL2_API_KEY is not set"), "Missing API key"),
            (ProviderError("provider request failed (URLError)"), "Provider/network failure"),
            (ProviderError("provider returned an invalid chat-completions response"), "Invalid model response"),
        )
        for error, expected in cases:
            class BrokenModes:
                def question(self, _text): raise error
            output = io.StringIO()
            with self.subTest(expected=expected), \
                 patch("assistant_cli.model_modes", return_value=BrokenModes()), \
                 patch("builtins.input", side_effect=["question", "why", "quit"]), \
                 patch("sys.stdout", output):
                self.assertEqual(assistant_cli.main([str(self.repo)]), 0)
            self.assertIn(expected, output.getvalue())

    def test_invalid_structured_suggestion_is_explained(self):
        class BrokenModes:
            def suggest_fix(self, _text): raise ModelResponseError("bad response")
        output = io.StringIO()
        with patch("assistant_cli.model_modes", return_value=BrokenModes()), \
             patch("builtins.input", side_effect=["suggest", "fix it", "quit"]), \
             patch("sys.stdout", output):
            self.assertEqual(assistant_cli.main([str(self.repo)]), 0)
        self.assertIn("Invalid model response", output.getvalue())

    def test_locally_rejected_suggestion_is_explained(self):
        class RejectedModes:
            def suggest_fix(self, _text): raise ToolError("target path is outside configured allowed_paths")
        output = io.StringIO()
        with patch("assistant_cli.model_modes", return_value=RejectedModes()), \
             patch("builtins.input", side_effect=["suggest", "fix it", "quit"]), \
             patch("sys.stdout", output):
            self.assertEqual(assistant_cli.main([str(self.repo)]), 0)
        self.assertIn("Suggestion rejected", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
