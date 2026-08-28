from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "replay-fixtures"
EXPECTED_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def answer():
-  return \"old\"
+  return \"new\"
diff --git a/notes.txt b/notes.txt
--- /dev/null
+++ b/notes.txt
@@ -0,0 +1 @@
+synthetic replay
"""


class ReplayCliTests(unittest.TestCase):
  def run_replay(self, output: Path, trajectory: str, agent: str, diagnostic: bool = False):
    arguments = [
      sys.executable,
      "-m",
      "repo_to_bench",
      "replay",
      "--workspace",
      str(FIXTURES / "base"),
      "--trajectory",
      str(FIXTURES / trajectory),
      "--agent",
      agent,
      "--output-dir",
      str(output),
    ]
    if diagnostic:
      arguments.append("--diagnostic-override")
    environment = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(arguments, cwd=ROOT, env=environment, capture_output=True, text=True)

  def assert_complete(self, result, output: Path):
    self.assertEqual(result.returncode, 0, result.stderr)
    response = json.loads(result.stdout)
    self.assertEqual(response["status"], "COMPLETE")
    self.assertTrue(response["normal_review_eligible"])
    self.assertEqual((output / "workspace" / "app.py").read_text(encoding="utf-8"), "def answer():\n  return \"new\"\n")
    self.assertEqual((output / "workspace" / "notes.txt").read_text(encoding="utf-8"), "synthetic replay\n")
    self.assertEqual((output / "diff.patch").read_text(encoding="utf-8"), EXPECTED_DIFF)

  def test_claude_code_fixture_reconstructs_complete_workspace_and_diff(self):
    with tempfile.TemporaryDirectory() as directory:
      output = Path(directory) / "replay"
      self.assert_complete(self.run_replay(output, "claude-code.jsonl", "claude-code"), output)

  def test_codex_fixture_reconstructs_complete_workspace_and_diff(self):
    with tempfile.TemporaryDirectory() as directory:
      output = Path(directory) / "replay"
      self.assert_complete(self.run_replay(output, "codex.jsonl", "codex"), output)

  def test_unsupported_mutation_is_incomplete_without_workspace(self):
    with tempfile.TemporaryDirectory() as directory:
      output = Path(directory) / "replay"
      result = self.run_replay(output, "unsupported.jsonl", "claude-code")
      self.assertNotEqual(result.returncode, 0)
      response = json.loads(result.stdout)
      self.assertEqual(response["status"], "INCOMPLETE")
      self.assertEqual(response["unsupported_operations"], [{"line": 1, "operation": "git apply"}])
      self.assertFalse(response["normal_review_eligible"])
      self.assertFalse((output / "workspace").exists())

  def test_malformed_trajectory_is_failed_without_workspace(self):
    with tempfile.TemporaryDirectory() as directory:
      output = Path(directory) / "replay"
      result = self.run_replay(output, "malformed.jsonl", "claude-code")
      self.assertNotEqual(result.returncode, 0)
      response = json.loads(result.stdout)
      self.assertEqual(response["status"], "FAILED")
      self.assertEqual(response["failure"], {"category": "malformed_trajectory", "line": 1})
      self.assertFalse((output / "workspace").exists())

  def test_repeated_replay_is_deterministic_and_drops_stale_state(self):
    with tempfile.TemporaryDirectory() as directory:
      output = Path(directory) / "replay"
      first = self.run_replay(output, "claude-code.jsonl", "claude-code")
      self.assert_complete(first, output)
      first_status = (output / "status.json").read_bytes()
      first_diff = (output / "diff.patch").read_bytes()
      (output / "workspace" / "stale.txt").write_text("stale\n", encoding="utf-8")
      second = self.run_replay(output, "claude-code.jsonl", "claude-code")
      self.assert_complete(second, output)
      self.assertEqual(json.loads(second.stdout), json.loads(first.stdout))
      self.assertEqual((output / "status.json").read_bytes(), first_status)
      self.assertEqual((output / "diff.patch").read_bytes(), first_diff)
      self.assertFalse((output / "workspace" / "stale.txt").exists())

  def test_diagnostic_override_exports_incomplete_but_remains_ineligible(self):
    with tempfile.TemporaryDirectory() as directory:
      output = Path(directory) / "replay"
      result = self.run_replay(output, "unsupported.jsonl", "claude-code", diagnostic=True)
      self.assertEqual(result.returncode, 0, result.stderr)
      response = json.loads(result.stdout)
      self.assertEqual(response["status"], "INCOMPLETE")
      self.assertFalse(response["normal_review_eligible"])
      self.assertTrue(response["diagnostic_override"])
      self.assertTrue((output / "workspace").is_dir())
      self.assertEqual((output / ".repo-to-bench-replay-owned").read_text(encoding="utf-8"), "repo-to-bench-replay/v1\nincomplete-diagnostic\n")


if __name__ == "__main__":
  unittest.main()
