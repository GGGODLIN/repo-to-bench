from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
SCANNER = ROOT / "scripts" / "release_scan.py"


class CiContractTests(unittest.TestCase):
  def test_ci_checks_installed_cli_and_release_history(self):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    self.assertIn("fetch-depth: 0", workflow)
    self.assertIn("python-version: [\"3.9\", \"3.11\"]", workflow)
    self.assertIn("patch-ledger --help", workflow)
    self.assertIn("python scripts/release_scan.py", workflow)
    self.assertIn("Operating System :: POSIX", project)
    self.assertNotIn("Operating System :: Microsoft :: Windows", project)
    self.assertTrue(SCANNER.is_file())

  def test_release_scan_passes_current_intent_tracked_tree(self):
    result = subprocess.run(
      [sys.executable, str(SCANNER)],
      cwd=ROOT,
      env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
      capture_output=True,
      text=True,
    )
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(json.loads(result.stdout)["status"], "clean")

  def test_release_scan_checks_forbidden_paths_in_duplicate_history_blobs(self):
    with tempfile.TemporaryDirectory() as directory:
      repo = Path(directory) / "repo"
      repo.mkdir()
      git = ["git", "-C", str(repo)]
      subprocess.run(git + ["init", "-b", "main"], check=True, capture_output=True)
      (repo / "safe.txt").write_text("same content\n", encoding="utf-8")
      subprocess.run(git + ["add", "safe.txt"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "safe"], check=True, capture_output=True)
      (repo / "transcript").write_text("same content\n", encoding="utf-8")
      subprocess.run(git + ["add", "transcript"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "forbidden"], check=True, capture_output=True)
      subprocess.run(git + ["rm", "transcript"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "remove"], check=True, capture_output=True)
      result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo)],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
      )
      findings = [json.loads(line) for line in result.stdout.splitlines() if line]
      self.assertNotEqual(result.returncode, 0)
      self.assertTrue(any(finding.get("category") == "forbidden_name" and finding.get("path", "").startswith("history/") for finding in findings))

  def test_release_scan_rejects_tracked_symlink_without_reading_target(self):
    with tempfile.TemporaryDirectory() as directory:
      repo = Path(directory) / "repo"
      external = Path(directory) / "external.txt"
      repo.mkdir()
      external.write_text("API_TOKEN" + "=" + "synthetic-" + ("a" * 24) + "\n", encoding="utf-8")
      (repo / "link.txt").symlink_to(external)
      git = ["git", "-C", str(repo)]
      subprocess.run(git + ["init", "-b", "main"], check=True, capture_output=True)
      subprocess.run(git + ["add", "link.txt"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "symlink"], check=True, capture_output=True)
      result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo)],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
      )
      findings = [json.loads(line) for line in result.stdout.splitlines() if line]
      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(findings, [{"category": "tracked_symlink", "path": "link.txt"}])

  def test_release_scan_symlink_history_target_is_not_scanned_as_content(self):
    with tempfile.TemporaryDirectory() as directory:
      repo = Path(directory) / "repo"
      repo.mkdir()
      (repo / "link.txt").symlink_to("password" + "=synthetic")
      git = ["git", "-C", str(repo)]
      subprocess.run(git + ["init", "-b", "main"], check=True, capture_output=True)
      subprocess.run(git + ["add", "link.txt"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "symlink"], check=True, capture_output=True)
      result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo)],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
      )
      findings = [json.loads(line) for line in result.stdout.splitlines() if line]
      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(findings, [{"category": "tracked_symlink", "path": "link.txt"}])
      self.assertEqual(result.stderr, "")

  def test_release_scan_flags_deleted_historical_symlink_name(self):
    with tempfile.TemporaryDirectory() as directory:
      repo = Path(directory) / "repo"
      repo.mkdir()
      (repo / "transcript").symlink_to("outside-target")
      git = ["git", "-C", str(repo)]
      subprocess.run(git + ["init", "-b", "main"], check=True, capture_output=True)
      subprocess.run(git + ["add", "transcript"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "symlink"], check=True, capture_output=True)
      subprocess.run(git + ["rm", "transcript"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "remove"], check=True, capture_output=True)
      result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo)],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
      )
      findings = [json.loads(line) for line in result.stdout.splitlines() if line]
      self.assertNotEqual(result.returncode, 0)
      self.assertEqual([finding.get("category") for finding in findings], ["forbidden_name"])
      self.assertTrue(findings[0]["path"].startswith("history/"))
      self.assertTrue(findings[0]["path"].endswith(":transcript"))

  def test_release_scan_rejects_tracked_fifo_without_blocking(self):
    if not hasattr(os, "mkfifo"):
      self.skipTest("FIFO is unavailable on this platform")
    with tempfile.TemporaryDirectory() as directory:
      repo = Path(directory) / "repo"
      repo.mkdir()
      tracked = repo / "pipe"
      tracked.write_text("placeholder\n", encoding="utf-8")
      git = ["git", "-C", str(repo)]
      subprocess.run(git + ["init", "-b", "main"], check=True, capture_output=True)
      subprocess.run(git + ["add", "pipe"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fifo"], check=True, capture_output=True)
      tracked.unlink()
      os.mkfifo(tracked)
      result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo)],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=2,
      )
      findings = [json.loads(line) for line in result.stdout.splitlines() if line]
      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(findings, [{"category": "non_regular_file", "path": "pipe"}])
      self.assertEqual(result.stderr, "")

  def test_release_scan_rejects_current_tree_credential_without_value_output(self):
    with tempfile.TemporaryDirectory() as directory:
      repo = Path(directory) / "repo"
      repo.mkdir()
      credential = "synthetic-" + ("a" * 24)
      (repo / "config.txt").write_text("AWS_SECRET_ACCESS_KEY" + "=" + credential + "\n", encoding="utf-8")
      git = ["git", "-C", str(repo)]
      subprocess.run(git + ["init", "-b", "main"], check=True, capture_output=True)
      subprocess.run(git + ["add", "config.txt"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "credential"], check=True, capture_output=True)
      result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo)],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
      )
      findings = [json.loads(line) for line in result.stdout.splitlines() if line]
      self.assertNotEqual(result.returncode, 0)
      self.assertTrue(any(finding.get("category") == "credential_assignment" for finding in findings))
      self.assertNotIn(credential, result.stdout + result.stderr)

  def test_release_scan_rejects_punctuation_leading_current_tree_credential(self):
    with tempfile.TemporaryDirectory() as directory:
      repo = Path(directory) / "repo"
      repo.mkdir()
      credential = "!" + "synthetic"
      (repo / "config.txt").write_text("password" + "=" + credential + "\n", encoding="utf-8")
      git = ["git", "-C", str(repo)]
      subprocess.run(git + ["init", "-b", "main"], check=True, capture_output=True)
      subprocess.run(git + ["add", "config.txt"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "credential"], check=True, capture_output=True)
      result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo)],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
      )
      findings = [json.loads(line) for line in result.stdout.splitlines() if line]
      self.assertNotEqual(result.returncode, 0)
      self.assertTrue(any(finding.get("category") == "credential_assignment" for finding in findings))
      self.assertNotIn(credential, result.stdout + result.stderr)

  def test_release_scan_rejects_quoted_whitespace_current_tree_credential(self):
    with tempfile.TemporaryDirectory() as directory:
      repo = Path(directory) / "repo"
      repo.mkdir()
      key = "pass" + "word"
      value = '"' + " secret value" + '"'
      (repo / "config.txt").write_text(key + "=" + value + "\n", encoding="utf-8")
      git = ["git", "-C", str(repo)]
      subprocess.run(git + ["init", "-b", "main"], check=True, capture_output=True)
      subprocess.run(git + ["add", "config.txt"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "credential"], check=True, capture_output=True)
      result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo)],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
      )
      findings = [json.loads(line) for line in result.stdout.splitlines() if line]
      self.assertNotEqual(result.returncode, 0)
      self.assertTrue(any(finding.get("category") == "credential_assignment" for finding in findings))
      self.assertNotIn(value, result.stdout + result.stderr)

  def test_release_scan_redacts_current_policy_finding_path(self):
    with tempfile.TemporaryDirectory() as directory:
      repo = Path(directory) / "repo"
      repo.mkdir()
      credential = "gh" + "p_" + ("0123456789abcdef" * 2)
      filename = "config-" + credential
      (repo / filename).write_text("/" + "Users/synthetic\n", encoding="utf-8")
      git = ["git", "-C", str(repo)]
      subprocess.run(git + ["init", "-b", "main"], check=True, capture_output=True)
      subprocess.run(git + ["add", filename], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "policy"], check=True, capture_output=True)
      result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo)],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
      )
      findings = [json.loads(line) for line in result.stdout.splitlines() if line]
      self.assertNotEqual(result.returncode, 0)
      self.assertTrue(any(finding.get("category") == "private_path" and finding.get("path") == "config-[redacted]" for finding in findings))
      self.assertNotIn(credential, result.stdout + result.stderr)

  def test_release_scan_rejects_punctuation_leading_history_credential(self):
    with tempfile.TemporaryDirectory() as directory:
      repo = Path(directory) / "repo"
      repo.mkdir()
      credential = "!" + "synthetic"
      (repo / "config.txt").write_text("password" + "=" + credential + "\n", encoding="utf-8")
      git = ["git", "-C", str(repo)]
      subprocess.run(git + ["init", "-b", "main"], check=True, capture_output=True)
      subprocess.run(git + ["add", "config.txt"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "credential"], check=True, capture_output=True)
      subprocess.run(git + ["rm", "config.txt"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "remove"], check=True, capture_output=True)
      result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo)],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
      )
      findings = [json.loads(line) for line in result.stdout.splitlines() if line]
      self.assertNotEqual(result.returncode, 0)
      self.assertTrue(any(finding.get("category") == "credential_assignment" and finding.get("path", "").startswith("history/") for finding in findings))
      self.assertNotIn(credential, result.stdout + result.stderr)

  def test_release_scan_rejects_quoted_whitespace_history_credential(self):
    with tempfile.TemporaryDirectory() as directory:
      repo = Path(directory) / "repo"
      repo.mkdir()
      key = "pass" + "word"
      value = '"' + " secret value" + '"'
      (repo / "config.txt").write_text(key + "=" + value + "\n", encoding="utf-8")
      git = ["git", "-C", str(repo)]
      subprocess.run(git + ["init", "-b", "main"], check=True, capture_output=True)
      subprocess.run(git + ["add", "config.txt"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "credential"], check=True, capture_output=True)
      subprocess.run(git + ["rm", "config.txt"], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "remove"], check=True, capture_output=True)
      result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo)],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
      )
      findings = [json.loads(line) for line in result.stdout.splitlines() if line]
      self.assertNotEqual(result.returncode, 0)
      self.assertTrue(any(finding.get("category") == "credential_assignment" and finding.get("path", "").startswith("history/") for finding in findings))
      self.assertNotIn(value, result.stdout + result.stderr)

  def test_release_scan_redacts_history_policy_finding_path(self):
    with tempfile.TemporaryDirectory() as directory:
      repo = Path(directory) / "repo"
      repo.mkdir()
      credential = "gh" + "p_" + ("0123456789abcdef" * 2)
      filename = "config-" + credential
      (repo / filename).write_text("/" + "Users/synthetic\n", encoding="utf-8")
      git = ["git", "-C", str(repo)]
      subprocess.run(git + ["init", "-b", "main"], check=True, capture_output=True)
      subprocess.run(git + ["add", filename], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "policy"], check=True, capture_output=True)
      subprocess.run(git + ["rm", filename], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "remove"], check=True, capture_output=True)
      result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo)],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
      )
      findings = [json.loads(line) for line in result.stdout.splitlines() if line]
      self.assertNotEqual(result.returncode, 0)
      self.assertTrue(any(finding.get("category") == "private_path" and finding.get("path", "").startswith("history/") and finding.get("path", "").endswith("config-[redacted]") for finding in findings))
      self.assertNotIn(credential, result.stdout + result.stderr)

  def test_release_scan_rejects_credential_shaped_filename_with_benign_content(self):
    with tempfile.TemporaryDirectory() as directory:
      repo = Path(directory) / "repo"
      repo.mkdir()
      credential = "gh" + "p_" + ("0123456789abcdef" * 2)
      filename = "config-" + credential
      (repo / filename).write_text("hello\n", encoding="utf-8")
      git = ["git", "-C", str(repo)]
      subprocess.run(git + ["init", "-b", "main"], check=True, capture_output=True)
      subprocess.run(git + ["add", filename], check=True, capture_output=True)
      subprocess.run(git + ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "benign"], check=True, capture_output=True)
      result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo)],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
      )
      findings = [json.loads(line) for line in result.stdout.splitlines() if line]
      self.assertNotEqual(result.returncode, 0)
      self.assertTrue(any(finding.get("category") == "credential_path" and finding.get("path") == "config-[redacted]" for finding in findings))
      self.assertNotIn(credential, result.stdout + result.stderr)


if __name__ == "__main__":

  unittest.main()
