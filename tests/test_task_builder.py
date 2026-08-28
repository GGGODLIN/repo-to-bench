from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from repo_to_bench import cli


def git_command(repo, *arguments):
  return subprocess.run(
    ["git", "-C", str(repo), *arguments],
    check=True,
    capture_output=True,
    text=True,
  )


def create_synthetic_repo(directory):
  fixture = ROOT / "examples" / "synthetic-project"
  repo = directory / "synthetic-repo"
  repo.mkdir()
  shutil.copy(fixture / "base" / "app.py", repo / "app.py")
  git_command(repo, "init", "-b", "main")
  git_command(repo, "add", "app.py")
  git_command(
    repo,
    "-c",
    "user.name=synthetic",
    "-c",
    "user.email=synthetic@example.invalid",
    "commit",
    "-m",
    "base",
  )
  base = git_command(repo, "rev-parse", "HEAD").stdout.strip()
  shutil.copy(fixture / "head" / "app.py", repo / "app.py")
  git_command(repo, "add", "app.py")
  git_command(
    repo,
    "-c",
    "user.name=synthetic",
    "-c",
    "user.email=synthetic@example.invalid",
    "commit",
    "-m",
    "Make answer return new",
  )
  head = git_command(repo, "rev-parse", "HEAD").stdout.strip()
  return repo, base, head


def create_synthetic_repo_with_secret(directory):
  repo = directory / "synthetic-secret-repo"
  repo.mkdir()
  git_command(repo, "init", "-b", "main")
  (repo / "app.py").write_text("def answer():\n  return 'old'\n", encoding="utf-8")
  credential = "synthetic-" + ("a" * 24)
  key = "API" + "_" + "TOKEN"
  (repo / "config.txt").write_bytes((key + "=" + credential + "\n").encode("utf-16"))
  git_command(repo, "add", "app.py", "config.txt")
  git_command(
    repo,
    "-c",
    "user.name=synthetic",
    "-c",
    "user.email=synthetic@example.invalid",
    "commit",
    "-m",
    "base",
  )
  base = git_command(repo, "rev-parse", "HEAD").stdout.strip()
  (repo / "app.py").write_text("def answer():\n  return 'new'\n", encoding="utf-8")
  git_command(repo, "add", "app.py")
  git_command(
    repo,
    "-c",
    "user.name=synthetic",
    "-c",
    "user.email=synthetic@example.invalid",
    "commit",
    "-m",
    "change",
  )
  head = git_command(repo, "rev-parse", "HEAD").stdout.strip()
  return repo, base, head


def create_synthetic_repo_with_oracle_test_mutation(directory):
  repo = directory / "synthetic-test-mutation-repo"
  repo.mkdir()
  git_command(repo, "init", "-b", "main")
  (repo / "app.py").write_text("def answer():\n  return 'old'\n", encoding="utf-8")
  git_command(repo, "add", "app.py")
  git_command(
    repo,
    "-c",
    "user.name=synthetic",
    "-c",
    "user.email=synthetic@example.invalid",
    "commit",
    "-m",
    "base",
  )
  base = git_command(repo, "rev-parse", "HEAD").stdout.strip()
  (repo / "app.py").write_text(
    "from pathlib import Path\n\ndef answer():\n  Path('../test.sh').write_text('#!/bin/sh\\nexit 1\\n')\n  return 'old'\n",
    encoding="utf-8",
  )
  git_command(repo, "add", "app.py")
  git_command(
    repo,
    "-c",
    "user.name=synthetic",
    "-c",
    "user.email=synthetic@example.invalid",
    "commit",
    "-m",
    "change",
  )
  head = git_command(repo, "rev-parse", "HEAD").stdout.strip()
  return repo, base, head


def create_synthetic_repo_with_base_mutation(directory):
  repo = directory / "synthetic-base-mutation-repo"
  repo.mkdir()
  git_command(repo, "init", "-b", "main")
  (repo / "app.py").write_text("def answer():\n  return 'old'\n", encoding="utf-8")
  git_command(repo, "add", "app.py")
  git_command(
    repo,
    "-c",
    "user.name=synthetic",
    "-c",
    "user.email=synthetic@example.invalid",
    "commit",
    "-m",
    "base",
  )
  base = git_command(repo, "rev-parse", "HEAD").stdout.strip()
  (repo / "app.py").write_text(
    "from pathlib import Path\n\ndef answer():\n  Path('../base-workspace/app.py').write_text(\"def answer():\\n  return 'broken'\\n\")\n  return 'old'\n",
    encoding="utf-8",
  )
  git_command(repo, "add", "app.py")
  git_command(
    repo,
    "-c",
    "user.name=synthetic",
    "-c",
    "user.email=synthetic@example.invalid",
    "commit",
    "-m",
    "change",
  )
  head = git_command(repo, "rev-parse", "HEAD").stdout.strip()
  return repo, base, head


def create_synthetic_repo_with_symlink(directory):
  repo = directory / "synthetic-symlink-repo"
  outside = directory / "outside"
  repo.mkdir()
  outside.mkdir()
  (outside / "secret.txt").write_text("not for the task\n", encoding="utf-8")
  git_command(repo, "init", "-b", "main")
  (repo / "app.py").write_text("def answer():\n  return 'old'\n", encoding="utf-8")
  git_command(repo, "add", "app.py")
  git_command(
    repo,
    "-c",
    "user.name=synthetic",
    "-c",
    "user.email=synthetic@example.invalid",
    "commit",
    "-m",
    "base",
  )
  base = git_command(repo, "rev-parse", "HEAD").stdout.strip()
  (repo / "app.py").write_text("def answer():\n  return 'new'\n", encoding="utf-8")
  (repo / "link.txt").symlink_to(outside / "secret.txt")
  git_command(repo, "add", "app.py", "link.txt")
  git_command(
    repo,
    "-c",
    "user.name=synthetic",
    "-c",
    "user.email=synthetic@example.invalid",
    "commit",
    "-m",
    "change",
  )
  head = git_command(repo, "rev-parse", "HEAD").stdout.strip()
  return repo, base, head


def run_builder(repo, base, head, output, test_command, output_inputs=None, timeout=30.0):
  environment = {"PATH": os.environ.get("PATH", "")}
  if os.environ.get("REPO_TO_BENCH_INSTALLED_E2E") != "1":
    environment["PYTHONPATH"] = str(SRC)
  return subprocess.run(
    [
      sys.executable,
      "-m",
      "repo_to_bench",
      "build-task",
      "--repo",
      str(repo),
      "--base-revision",
      base,
      "--head-revision",
      head,
      "--test-command",
      test_command,
      "--output-dir",
      str(output),
      "--timeout",
      str(timeout),
      *sum((["--output-input", value] for value in (output_inputs or [])), []),
    ],
    cwd=ROOT,
    env=environment,
    capture_output=True,
    text=True,
    timeout=10,
  )


class TaskBuilderTests(unittest.TestCase):
  def test_staging_creation_failure_returns_structured_error(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      arguments = [
        "build-task",
        "--repo",
        str(repo),
        "--base",
        base,
        "--head",
        head,
        "--test",
        "python3 -c \"assert True\"",
        "--output",
        str(workspace / "run"),
      ]
      stdout = StringIO()
      stderr = StringIO()
      with patch("repo_to_bench.task_builder.tempfile.mkdtemp", side_effect=OSError("staging")):
        with redirect_stdout(stdout), redirect_stderr(stderr):
          return_code = cli.main(arguments)

      self.assertNotEqual(return_code, 0)
      self.assertEqual(json.loads(stdout.getvalue())["error"]["category"], "filesystem_error")
      self.assertEqual(stderr.getvalue(), "")

  def test_cleanup_failure_returns_structured_error(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      arguments = [
        "build-task",
        "--repo",
        str(repo),
        "--base",
        base,
        "--head",
        head,
        "--test",
        "python3 -c \"assert True\"",
        "--output",
        str(workspace / "run"),
      ]
      stdout = StringIO()
      stderr = StringIO()
      original_rmtree = shutil.rmtree

      def fail_staging_cleanup(path, *args, **kwargs):
        if ".repo-to-bench-build-" in str(path):
          raise OSError("cleanup")
        return original_rmtree(path, *args, **kwargs)

      with patch("repo_to_bench.task_builder.shutil.rmtree", side_effect=fail_staging_cleanup):
        with redirect_stdout(stdout), redirect_stderr(stderr):
          return_code = cli.main(arguments)

      self.assertNotEqual(return_code, 0)
      self.assertEqual(json.loads(stdout.getvalue())["error"]["category"], "cleanup_failed")
      output = workspace / "run"
      self.assertFalse((output / "task").exists())
      self.assertFalse((output / "controller").exists())
      self.assertFalse((output / "task-manifest.json").exists())
      self.assertFalse((output / "dual-gate-report.json").exists())
      self.assertEqual(stderr.getvalue(), "")

  def test_publish_failure_does_not_leave_partial_task_output(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      arguments = [
        "build-task",
        "--repo",
        str(repo),
        "--base",
        base,
        "--head",
        head,
        "--test",
        "python3 -c \"import app; assert app.answer() == 'new'\"",
        "--output",
        str(workspace / "run"),
      ]
      stdout = StringIO()
      stderr = StringIO()
      original_rename = os.rename

      def fail_controller_publish(source, destination):
        if Path(destination).name == "controller":
          raise OSError("publish")
        return original_rename(source, destination)

      with patch("repo_to_bench.task_builder.os.rename", side_effect=fail_controller_publish):
        with redirect_stdout(stdout), redirect_stderr(stderr):
          return_code = cli.main(arguments)

      self.assertNotEqual(return_code, 0)
      self.assertEqual(json.loads(stdout.getvalue())["error"]["category"], "task_build_failed")
      output = workspace / "run"
      self.assertFalse((output / "task").exists())
      self.assertFalse((output / "controller").exists())
      self.assertFalse((output / "task-manifest.json").exists())
      self.assertFalse((output / "dual-gate-report.json").exists())
      self.assertEqual(stderr.getvalue(), "")

  def test_public_fixture_contains_one_synthetic_project_change(self):
    fixture = ROOT / "examples" / "synthetic-project"
    self.assertTrue((fixture / "base" / "app.py").is_file())
    self.assertTrue((fixture / "head" / "app.py").is_file())
    self.assertTrue((fixture / "change.patch").is_file())
    self.assertTrue((fixture / "test-command.txt").is_file())
    self.assertTrue((fixture / "create-repo.sh").is_file())
    self.assertTrue(os.access(fixture / "create-repo.sh", os.X_OK))
    self.assertNotEqual(
      (fixture / "base" / "app.py").read_text(encoding="utf-8"),
      (fixture / "head" / "app.py").read_text(encoding="utf-8"),
    )

  def test_build_task_accepts_generic_git_revisions_and_emits_harbor_shape(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      response = json.loads(result.stdout)
      self.assertEqual(response["status"], "accepted")
      task = output / "task"
      self.assertTrue((task / "task.toml").is_file())
      self.assertTrue((task / "instruction.md").is_file())
      self.assertTrue((task / "environment" / "Dockerfile").is_file())
      dockerfile = (task / "environment" / "Dockerfile").read_text(encoding="utf-8")
      self.assertRegex(dockerfile.splitlines()[0], r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$")
      self.assertIn("git=1:2.47.3-0+deb13u1", dockerfile)
      self.assertTrue((task / "environment" / "workspace" / "app.py").is_file())
      self.assertEqual(
        (task / "environment" / "workspace" / "app.py").read_text(encoding="utf-8"),
        (ROOT / "examples" / "synthetic-project" / "base" / "app.py").read_text(encoding="utf-8"),
      )
      self.assertTrue((task / "tests" / "test.sh").is_file())
      self.assertTrue((task / "solution" / "solve.sh").is_file())
      self.assertTrue((task / "solution" / "patch.diff").is_file())
      test_script = (task / "tests" / "test.sh").read_text(encoding="utf-8")
      self.assertIn("REWARD_FILE", test_script)
      self.assertIn('{\"reward\":1}', test_script)
      self.assertIn('{\"reward\":0}', test_script)
      manifest = json.loads((output / "task-manifest.json").read_text(encoding="utf-8"))
      report = json.loads((output / "dual-gate-report.json").read_text(encoding="utf-8"))
      self.assertEqual(manifest["schema"], "repo-to-bench-task-manifest/v1")
      self.assertEqual(manifest["format"]["harbor_version"], "0.22.0")
      self.assertEqual(report["schema"], "repo-to-bench-dual-gate/v1")
      self.assertTrue(report["accepted"])

  def test_unusable_output_root_returns_json_without_traceback(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      blocker = workspace / "blocker"
      blocker.write_text("not a directory\n", encoding="utf-8")
      output = blocker / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "filesystem_error")
      self.assertEqual(result.stderr, "")

  def test_instruction_uses_generic_head_commit_message(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(
        (output / "task" / "instruction.md").read_text(encoding="utf-8"),
        "Implement the requested change in the supplied repository.\n\nUntrusted revision metadata:\n> Make answer return new\n",
      )

  def test_generated_controller_and_task_roots_are_owner_only(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
      )
      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(stat.S_IMODE((output / "task").stat().st_mode), 0o700)
      self.assertEqual(stat.S_IMODE((output / "controller").stat().st_mode), 0o700)
      self.assertEqual(stat.S_IMODE((output / "task-manifest.json").stat().st_mode), 0o600)
      self.assertEqual(stat.S_IMODE((output / "dual-gate-report.json").stat().st_mode), 0o600)

  def test_complete_publish_has_durable_ready_marker(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(
        (output / ".repo-to-bench-ready").read_text(encoding="utf-8"),
        "repo-to-bench-ready/v1\naccepted\n",
      )
      self.assertFalse((output / ".repo-to-bench-build-lock").exists())

  def test_output_inputs_are_root_level_harbor_artifacts(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
        ["result.json"],
      )
      self.assertEqual(result.returncode, 0, result.stderr)
      config = (output / "task" / "task.toml").read_text(encoding="utf-8")
      self.assertIn(
        '\nartifacts = [{ source = "/app/result.json", destination = "result.json" }]\n\n[task]\n',
        config,
      )

  def test_manifest_and_report_are_stable_across_output_roots(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      command = "python3 -c \"import app; assert app.answer() == 'new'\""
      first = run_builder(repo, base, head, workspace / "first", command)
      second = run_builder(repo, base, head, workspace / "second", command)
      self.assertEqual(first.returncode, 0, first.stderr)
      self.assertEqual(second.returncode, 0, second.stderr)
      for relative in ("task-manifest.json", "dual-gate-report.json"):
        self.assertEqual(
          (workspace / "first" / relative).read_bytes(),
          (workspace / "second" / relative).read_bytes(),
        )
      first_files = sorted(path.relative_to(workspace / "first" / "task").as_posix() for path in (workspace / "first" / "task").rglob("*") if path.is_file())
      second_files = sorted(path.relative_to(workspace / "second" / "task").as_posix() for path in (workspace / "second" / "task").rglob("*") if path.is_file())
      self.assertEqual(first_files, second_files)
      for relative in first_files:
        self.assertEqual(
          (workspace / "first" / "task" / relative).read_bytes(),
          (workspace / "second" / "task" / relative).read_bytes(),
        )

  def test_base_pass_oracle_fail_rejects_task_with_diagnostic_gate_report(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'old'\"",
      )

      self.assertNotEqual(result.returncode, 0)
      response = json.loads(result.stdout)
      self.assertEqual(response["error"]["category"], "invalid_task_gate")
      self.assertFalse((output / "task").exists())
      report = json.loads((output / "dual-gate-report.json").read_text(encoding="utf-8"))
      self.assertFalse(report["accepted"])
      self.assertEqual(report["oracle"]["status"], "failed")
      self.assertEqual(report["oracle"]["apply"]["status"], "passed")
      self.assertEqual(report["oracle"]["test"]["status"], "failed")
      self.assertEqual(report["base"]["status"], "passed")
      self.assertEqual(report["base"]["test"]["status"], "passed")

  def test_controller_metadata_is_outside_agent_workspace(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      controller_metadata = output / "controller" / "metadata.json"
      self.assertTrue(controller_metadata.is_file())
      self.assertFalse((output / "task" / "environment" / "workspace" / "controller").exists())
      visible_files = sorted(
        path.relative_to(output / "task" / "environment" / "workspace").as_posix()
        for path in (output / "task" / "environment" / "workspace").rglob("*")
        if path.is_file()
      )
      self.assertEqual(visible_files, ["app.py"])
      metadata = json.loads(controller_metadata.read_text(encoding="utf-8"))
      self.assertEqual(metadata["schema"], "repo-to-bench-controller-metadata/v1")
      self.assertEqual(metadata["agent_workspace"], "task/environment/workspace")

  def test_same_gate_result_rejects_task(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      result = run_builder(repo, base, head, output, "python3 -c \"assert True\"")

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_task_gate")
      self.assertFalse((output / "task").exists())
      report = json.loads((output / "dual-gate-report.json").read_text(encoding="utf-8"))
      self.assertFalse(report["accepted"])
      self.assertEqual(report["oracle"]["status"], "passed")
      self.assertEqual(report["base"]["status"], "passed")

  def test_nonfinite_timeout_returns_structured_error(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      result = run_builder(
        repo,
        base,
        head,
        workspace / "run",
        "python3 -c \"assert True\"",
        timeout=float("nan"),
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_timeout")
      self.assertEqual(result.stderr, "")

  def test_git_diff_does_not_execute_repository_external_diff_command(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      marker = workspace / "external-diff-ran"
      external = workspace / "external-diff.sh"
      external.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
      external.chmod(0o700)
      git_command(repo, "config", "diff.external", str(external))
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertFalse(marker.exists())

  def test_gate_command_cannot_mutate_published_task_tree(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      command = "leak_path=$(dirname \"$0\")/../environment/workspace/leak.txt; printf leak > \"$leak_path\"; python3 -c \"import app; assert app.answer() == 'new'\""
      result = run_builder(repo, base, head, output, command)

      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertFalse((output / "task" / "environment" / "workspace" / "leak.txt").exists())

  def test_oracle_cannot_rewrite_shared_gate_test(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo_with_oracle_test_mutation(workspace)
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'old'\"",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_task_gate")
      report = json.loads((output / "dual-gate-report.json").read_text(encoding="utf-8"))
      self.assertEqual(report["oracle"]["status"], "passed")
      self.assertEqual(report["base"]["status"], "passed")
      self.assertFalse(report["accepted"])

  def test_oracle_cannot_mutate_base_workspace(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo_with_base_mutation(workspace)
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'old'\"",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_task_gate")
      report = json.loads((output / "dual-gate-report.json").read_text(encoding="utf-8"))
      self.assertEqual(report["oracle"]["status"], "passed")
      self.assertEqual(report["base"]["status"], "passed")
      self.assertFalse(report["accepted"])

  def test_gate_runs_have_independent_home_state(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      command = "if [ -f \"$HOME/seen\" ]; then exit 1; fi; touch \"$HOME/seen\"; python3 -c \"assert True\""
      result = run_builder(repo, base, head, output, command)

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_task_gate")
      report = json.loads((output / "dual-gate-report.json").read_text(encoding="utf-8"))
      self.assertEqual(report["oracle"]["status"], "passed")
      self.assertEqual(report["base"]["status"], "passed")
      self.assertFalse(report["accepted"])

  def test_base_timeout_rejects_task_instead_of_counting_as_base_failure(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app, time; time.sleep(1) if app.answer() == 'old' else None\"",
        timeout=0.05,
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_task_gate")
      self.assertFalse((output / "task").exists())
      report = json.loads((output / "dual-gate-report.json").read_text(encoding="utf-8"))
      self.assertTrue(report["oracle"]["status"] == "passed")
      self.assertEqual(report["base"]["status"], "timeout")
      self.assertFalse(report["accepted"])

  def test_private_path_in_test_command_is_rejected_before_execution(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      marker = workspace / "executed"
      output = workspace / "run"
      private_path = "/" + "Users/private"
      result = run_builder(
        repo,
        base,
        head,
        output,
        f"touch {marker}; value='{private_path}'; python3 -c \"import app; assert app.answer() == 'new'\"",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "private_path_detected")
      self.assertFalse(marker.exists())
      self.assertFalse((output / "task").exists())

  def test_credential_shaped_output_input_is_rejected_before_execution(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      credential = "gh" + "p_" + ("0123456789abcdef" * 2)
      marker = workspace / "executed"
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        f"touch {marker}; python3 -c \"import app; assert app.answer() == 'new'\"",
        ["result-" + credential],
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_output_input")
      self.assertFalse(marker.exists())
      self.assertFalse((output / "task").exists())

  def test_dot_artifact_input_is_rejected(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      result = run_builder(
        repo,
        base,
        head,
        workspace / "run",
        "python3 -c \"assert True\"",
        ["."],
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_output_input")

  def test_reserved_artifact_manifest_name_is_rejected(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      result = run_builder(
        repo,
        base,
        head,
        workspace / "run",
        "python3 -c \"assert True\"",
        ["manifest.json"],
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_output_input")

  def test_source_credential_is_rejected_before_test_execution(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo_with_secret(workspace)
      marker = workspace / "executed"
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        f"touch {marker}; python3 -c \"import app; assert app.answer() == 'new'\"",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "credential_detected")
      self.assertFalse(marker.exists())
      self.assertFalse((output / "task").exists())

  def test_head_symlink_is_rejected_before_test_execution(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo_with_symlink(workspace)
      marker = workspace / "executed"
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        f"touch {marker}; python3 -c \"assert True\"",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "unsupported_source_entry")
      self.assertFalse(marker.exists())
      self.assertFalse((output / "task").exists())


if __name__ == "__main__":
  unittest.main()
