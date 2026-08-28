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
FAKE_HARBOR = ROOT / "tests" / "fake_harbor.py"
os.environ.setdefault("REPO_TO_BENCH_HARBOR", str(FAKE_HARBOR))
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


def create_synthetic_repo_with_snapshot_attributes(directory):
  repo = directory / "synthetic-snapshot-repo"
  repo.mkdir()
  git_command(repo, "init", "-b", "main")
  (repo / "app.py").write_text("def answer():\n  return 'old'\n", encoding="utf-8")
  executable = repo / "run"
  executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
  executable.chmod(0o755)
  (repo / ".gitattributes").write_text(
    "ignored.txt export-ignore\nsubstituted.txt export-subst\n",
    encoding="utf-8",
  )
  (repo / "ignored.txt").write_text("keep this exact\n", encoding="utf-8")
  (repo / "substituted.txt").write_text("$Format:%H$\n", encoding="utf-8")
  git_command(repo, "add", "app.py", "run", ".gitattributes", "ignored.txt", "substituted.txt")
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


def create_synthetic_repo_with_submodule(directory):
  repo = directory / "synthetic-submodule-repo"
  nested = directory / "nested-repo"
  repo.mkdir()
  nested.mkdir()
  git_command(nested, "init", "-b", "main")
  (nested / "nested.txt").write_text("nested\n", encoding="utf-8")
  git_command(nested, "add", "nested.txt")
  git_command(
    nested,
    "-c",
    "user.name=synthetic",
    "-c",
    "user.email=synthetic@example.invalid",
    "commit",
    "-m",
    "nested",
  )
  nested_revision = git_command(nested, "rev-parse", "HEAD").stdout.strip()
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
  git_command(
    repo,
    "update-index",
    "--add",
    "--cacheinfo",
    "160000," + nested_revision + ",nested",
  )
  git_command(
    repo,
    "-c",
    "user.name=synthetic",
    "-c",
    "user.email=synthetic@example.invalid",
    "commit",
    "-m",
    "submodule",
  )
  head = git_command(repo, "rev-parse", "HEAD").stdout.strip()
  return repo, base, head


def run_builder(repo, base, head, output, test_command, output_inputs=None, timeout=30.0, fake_profile=None, fake_env=None):
  environment = {
    "HOME": os.environ.get("HOME", ""),
    "PATH": os.environ.get("PATH", ""),
  }
  if os.environ.get("REPO_TO_BENCH_INSTALLED_E2E") != "1":
    environment["PYTHONPATH"] = str(SRC)
    environment["REPO_TO_BENCH_HARBOR"] = str(FAKE_HARBOR)
  if fake_profile is not None:
    environment["FAKE_HARBOR_PROFILE"] = json.dumps(fake_profile)
  environment.update(fake_env or {})
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
    timeout=600 if os.environ.get("REPO_TO_BENCH_INSTALLED_E2E") == "1" else 10,
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
      self.assertTrue((task / "tests" / "Dockerfile").is_file())
      self.assertTrue((task / "tests" / "test.sh").is_file())
      self.assertTrue((task / "solution" / "solve.sh").is_file())
      self.assertTrue((task / "solution" / "patch.diff").is_file())
      test_script = (task / "tests" / "test.sh").read_text(encoding="utf-8")
      self.assertNotIn("REWARD_FILE", test_script)
      self.assertNotIn('{\"reward\":1}', test_script)
      self.assertNotIn('{\"reward\":0}', test_script)
      self.assertIn('artifacts = [{ source = "/app", destination = "workspace" }]', (task / "task.toml").read_text(encoding="utf-8"))
      self.assertIn('environment_mode = "separate"', (task / "task.toml").read_text(encoding="utf-8"))
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
        'artifacts = [{ source = "/app", destination = "workspace" }, { source = "/app/result.json", destination = "result.json" }]',
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
      self.assertEqual(report["oracle"]["status"], "passed")
      self.assertEqual(report["oracle"]["mean"], 0.0)
      self.assertEqual(report["nop"]["status"], "passed")
      self.assertEqual(report["nop"]["mean"], 1.0)

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
      self.assertEqual(report["nop"]["status"], "passed")

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
      self.assertEqual(report["nop"]["status"], "passed")
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
      self.assertEqual(report["nop"]["status"], "passed")
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
      self.assertEqual(report["nop"]["status"], "passed")
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
      self.assertEqual(report["oracle"]["status"], "invalid_result")
      self.assertEqual(report["nop"]["status"], "invalid_result")
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


  def test_gitfile_outside_repo_boundary_is_rejected(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      external = workspace / "external"
      alias = workspace / "alias"
      external.mkdir()
      alias.mkdir()
      git_command(external, "init", "-b", "main")
      git_command(external, "config", "user.name", "synthetic")
      git_command(external, "config", "user.email", "synthetic@example.invalid")
      (external / "app.py").write_text("def answer():\n  return 'old'\n", encoding="utf-8")
      git_command(external, "add", "app.py")
      git_command(external, "commit", "-m", "base")
      base = git_command(external, "rev-parse", "HEAD").stdout.strip()
      (external / "app.py").write_text("def answer():\n  return 'new'\n", encoding="utf-8")
      git_command(external, "add", "app.py")
      git_command(external, "commit", "-m", "head")
      head = git_command(external, "rev-parse", "HEAD").stdout.strip()
      (alias / ".git").write_text(
        "gitdir: " + str(external / ".git") + "\n",
        encoding="utf-8",
      )
      git_command(external, "config", "core.worktree", str(external))

      result = run_builder(
        alias,
        base,
        head,
        workspace / "run",
        "python3 -c \"assert True\"",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_repo")
      self.assertFalse((workspace / "run").exists())

  def test_git_object_alternates_are_rejected(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      alternates = repo / ".git" / "objects" / "info" / "alternates"
      alternates.parent.mkdir(parents=True, exist_ok=True)
      alternates.write_text(str(workspace / "external-objects") + "\n", encoding="utf-8")

      result = run_builder(
        repo,
        base,
        head,
        workspace / "run",
        "python3 -c \"assert True\"",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_repo")
      self.assertFalse((workspace / "run").exists())

  def test_registered_git_worktree_remains_supported(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo = workspace / "repo"
      linked = workspace / "linked"
      repo.mkdir()
      git_command(repo, "init", "-b", "main")
      git_command(repo, "config", "user.name", "synthetic")
      git_command(repo, "config", "user.email", "synthetic@example.invalid")
      (repo / "app.py").write_text("def answer():\n  return 'old'\n", encoding="utf-8")
      git_command(repo, "add", "app.py")
      git_command(repo, "commit", "-m", "base")
      base = git_command(repo, "rev-parse", "HEAD").stdout.strip()
      git_command(repo, "worktree", "add", str(linked))
      (linked / "app.py").write_text("def answer():\n  return 'new'\n", encoding="utf-8")
      git_command(linked, "add", "app.py")
      git_command(linked, "commit", "-m", "head")
      head = git_command(linked, "rev-parse", "HEAD").stdout.strip()

      result = run_builder(
        linked,
        base,
        head,
        workspace / "run",
        "python3 -c \"import app; assert app.answer() == 'new'\"",
      )

      self.assertEqual(result.returncode, 0, result.stderr)

  def test_git_snapshot_rejects_submodules(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo_with_submodule(workspace)
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"assert True\"",
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "unsupported_source_entry")
      self.assertFalse(output.exists())

  def test_git_snapshot_preserves_attributes_content_and_executable_mode(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo_with_snapshot_attributes(workspace)
      output = workspace / "run"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      source = output / "task" / "environment" / "workspace"
      self.assertEqual((source / "ignored.txt").read_bytes(), b"keep this exact\n")
      self.assertEqual((source / "substituted.txt").read_bytes(), b"$Format:%H$\n")
      self.assertEqual(stat.S_IMODE((source / "run").stat().st_mode), 0o700)
      self.assertEqual(
        (source / "run").read_bytes(),
        b"#!/bin/sh\nexit 0\n",
      )

  def test_git_snapshot_does_not_use_git_archive(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      from repo_to_bench import task_builder
      original = task_builder._run_git
      calls = []

      def record_git(repository, arguments, binary=False):
        calls.append(arguments)
        return original(repository, arguments, binary=binary)

      stdout = StringIO()
      stderr = StringIO()
      with patch("repo_to_bench.task_builder._run_git", side_effect=record_git):
        with redirect_stdout(stdout), redirect_stderr(stderr):
          return_code = cli.main([
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
            str(output),
          ])

      self.assertEqual(return_code, 0, stderr.getvalue())
      self.assertFalse(any("archive" in arguments for arguments in calls))
      self.assertTrue(any(arguments[:2] == ["ls-tree", "-rz"] for arguments in calls))
      self.assertTrue(any(arguments[:2] == ["cat-file", "blob"] for arguments in calls))

  def test_valid_test_command_is_not_executed_on_host(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      marker = workspace / "host-executed"
      command = f"touch {marker}; python3 -c \\\"import app; assert app.answer() == 'new'\\\""
      result = run_builder(repo, base, head, output, command)

      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertFalse(marker.exists())

  def test_fake_harbor_gate_receives_strict_separate_invocations(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      log = workspace / "harbor.jsonl"
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
        fake_env={"FAKE_HARBOR_LOG": str(log)},
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      invocations = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
      self.assertEqual([entry["agent"] for entry in invocations], ["oracle", "nop"])
      for entry in invocations:
        argv = entry["argv"]
        self.assertIn("--verifier", argv)
        self.assertEqual(argv[argv.index("--verifier") + 1], "repo_to_bench.harbor_verifier:ExitCodeVerifier")
        self.assertIn("--env", argv)
        self.assertEqual(argv[argv.index("--env") + 1], "docker")
        self.assertIn("--n-attempts", argv)
        self.assertEqual(argv[argv.index("--n-attempts") + 1], "1")
        self.assertIn("--max-retries", argv)
        self.assertEqual(argv[argv.index("--max-retries") + 1], "0")
      config = (output / "task" / "task.toml").read_text(encoding="utf-8")
      self.assertIn('artifacts = [{ source = "/app", destination = "workspace" }]', config)
      self.assertIn('environment_mode = "separate"', config)
      self.assertTrue((output / "task" / "tests" / "Dockerfile").is_file())

  def test_harbor_gate_rejects_unexpected_evaluation_name(self):
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
        fake_profile={
          "oracle": {"eval_name": "oracle__wrong-dataset"},
          "nop": {"eval_name": "nop__wrong-dataset"},
        },
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_task_gate")
      report = json.loads((output / "dual-gate-report.json").read_text(encoding="utf-8"))
      self.assertEqual(report["oracle"]["status"], "invalid_result")
      self.assertEqual(report["nop"]["status"], "invalid_result")

  def test_harbor_internal_symlink_cannot_change_external_mode(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      external = workspace / "external-output"
      external.write_text("external\n", encoding="utf-8")
      external.chmod(0o666)
      result = run_builder(
        repo,
        base,
        head,
        workspace / "run",
        "python3 -c \"import app; assert app.answer() == 'new'\"",
        fake_env={"FAKE_HARBOR_SYMLINK_TARGET": str(external)},
      )

      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o666)

  def test_harbor_metadata_paths_are_not_scanned_as_publishable_artifacts(self):
    from repo_to_bench import task_builder
    with tempfile.TemporaryDirectory(dir=str(Path.home())) as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)

      def harbor_gate(staging, agent, timeout):
        job = staging / "jobs" / (agent + "-gate")
        job.mkdir(parents=True)
        for filename in ("config.json", "lock.json", "result.json"):
          (job / filename).write_text(
            json.dumps({"path": str(workspace / "private-output")}),
            encoding="utf-8",
          )
        mean = 1.0 if agent == "oracle" else 0.0
        return {"status": "passed", "mean": mean, "n_errors": 0}

      with patch("repo_to_bench.task_builder._run_harbor_gate", side_effect=harbor_gate):
        result = task_builder.build_task(
          repo=str(repo),
          base_revision=base,
          head_revision=head,
          test_command="python3 -c \"import app; assert app.answer() == 'new'\"",
          output_dir=str(workspace / "run"),
        )

      self.assertEqual(result["status"], "accepted")

  def test_harbor_gate_rejects_unknown_result_fields(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      profile = {
        "oracle": {"payload": {"unexpected": True}},
        "nop": {"payload": {"unexpected": True}},
      }
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
        fake_profile=profile,
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_task_gate")
      report = json.loads((output / "dual-gate-report.json").read_text(encoding="utf-8"))
      self.assertEqual(report["oracle"]["status"], "invalid_result")
      self.assertEqual(report["nop"]["status"], "invalid_result")

  def test_harbor_gate_rejects_missing_result_fields(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      payload = {
        "id": "00000000-0000-0000-0000-000000000000",
        "started_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:01",
        "finished_at": "2026-01-01T00:00:01",
        "n_total_trials": 1,
      }
      result = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
        fake_profile={"oracle": {"payload": payload}, "nop": {"payload": payload}},
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_task_gate")
      report = json.loads((output / "dual-gate-report.json").read_text(encoding="utf-8"))
      self.assertEqual(report["oracle"]["status"], "invalid_result")
      self.assertEqual(report["nop"]["status"], "invalid_result")

  def test_harbor_gate_rejects_nonzero_cost_or_token_result(self):
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
        fake_profile={
          "oracle": {"stats": {"cost_usd": 0.01}},
          "nop": {"stats": {"n_input_tokens": 1}},
        },
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_task_gate")

  def test_harbor_gate_rejects_process_error(self):
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
        fake_env={"FAKE_HARBOR_EXIT_AGENT": "oracle"},
      )

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_task_gate")
      report = json.loads((output / "dual-gate-report.json").read_text(encoding="utf-8"))
      self.assertEqual(report["oracle"]["status"], "process_error")
      self.assertEqual(report["nop"]["status"], "passed")

  def test_harbor_gate_rejects_timeout(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      with patch.dict(
        os.environ,
        {
          "REPO_TO_BENCH_HARBOR": str(FAKE_HARBOR),
          "FAKE_HARBOR_SLEEP": "1",
        },
        clear=False,
      ), patch("repo_to_bench.task_builder._HARBOR_PROCESS_TIMEOUT", 0.01):
        stdout = StringIO()
        stderr = StringIO()
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
          str(output),
          "--timeout",
          "0.01",
        ]
        with redirect_stdout(stdout), redirect_stderr(stderr):
          return_code = cli.main(arguments)

      self.assertNotEqual(return_code, 0)
      self.assertEqual(json.loads(stdout.getvalue())["error"]["category"], "invalid_task_gate")
      report = json.loads((output / "dual-gate-report.json").read_text(encoding="utf-8"))
      self.assertEqual(report["oracle"]["status"], "timeout")
      self.assertEqual(report["nop"]["status"], "timeout")

  def test_generated_task_uses_separate_exit_code_verifier_and_workspace_artifact(self):
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
      task_config = (output / "task" / "task.toml").read_text(encoding="utf-8")
      self.assertIn('artifacts = [{ source = "/app", destination = "workspace" }]', task_config)
      self.assertIn("environment_mode = \"separate\"", task_config)
      self.assertTrue((output / "task" / "tests" / "Dockerfile").is_file())
      test_script = (output / "task" / "tests" / "test.sh").read_text(encoding="utf-8")
      self.assertNotIn("REWARD_FILE", test_script)
      self.assertNotIn('{\"reward\":1}', test_script)
      self.assertNotIn('{\"reward\":0}', test_script)


if __name__ == "__main__":
  unittest.main()
