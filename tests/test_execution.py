from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAKE_HARBOR = ROOT / "tests" / "fake_harbor_job.py"


class ExecutionCliTests(unittest.TestCase):
  def create_task(self, root: Path) -> Path:
    task_root = root / "built-task"
    (task_root / "task").mkdir(parents=True)
    manifest = {
      "schema": "repo-to-bench-task-manifest/v1",
      "format": {"harbor_version": "0.22.0"},
      "task": {"name": "repo-to-bench/synthetic-task", "path": "task"},
      "revisions": {"base": "a" * 40, "head": "b" * 40, "patch_sha256": "c" * 64},
    }
    (task_root / "task-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (task_root / ".repo-to-bench-ready").write_text("repo-to-bench-ready/v1\naccepted\n", encoding="utf-8")
    return task_root

  def run_cli(self, *arguments: str, extra_env=None):
    environment = {
      "PATH": os.environ.get("PATH", ""),
      "PYTHONPATH": str(ROOT / "src"),
      "REPO_TO_BENCH_HARBOR": str(FAKE_HARBOR),
    }
    environment.update(extra_env or {})
    return subprocess.run(
      [sys.executable, "-m", "repo_to_bench", *arguments],
      cwd=ROOT,
      env=environment,
      capture_output=True,
      text=True,
    )

  def plan(self, task_root: Path, output: Path, attempts: int = 1, log: Path = None):
    extra_env = {"FAKE_HARBOR_LOG": str(log)} if log is not None else None
    result = self.run_cli(
      "plan",
      "--task-root",
      str(task_root),
      "--agent",
      "oracle",
      "--agent",
      "nop",
      "--attempts",
      str(attempts),
      "--concurrency",
      "2",
      "--output-dir",
      str(output),
      extra_env=extra_env,
    )
    self.assertEqual(result.returncode, 0, result.stderr)
    return json.loads(result.stdout)

  def execute(self, plan: Path, digest: str, extra_env=None, allow_partial: bool = False):
    arguments = ["execute", "--plan", str(plan), "--approve-digest", digest]
    if allow_partial:
      arguments.append("--allow-partial")
    return self.run_cli(*arguments, extra_env=extra_env)

  def test_plan_resolves_matrix_and_emits_stable_digest(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      task_root = self.create_task(root)
      output = root / "run"
      log = root / "harbor.jsonl"
      first = self.plan(task_root, output, log=log)
      first_bytes = (output / "plan.json").read_bytes()
      second = self.plan(task_root, output, log=log)
      self.assertEqual(first, second)
      self.assertEqual((output / "plan.json").read_bytes(), first_bytes)
      self.assertEqual(first["status"], "planned")
      self.assertEqual(first["digest"].split(":", 1)[0], "sha256")
      self.assertEqual(first["task"]["name"], "repo-to-bench/synthetic-task")
      self.assertEqual(first["task"]["revisions"], {"base": "a" * 40, "head": "b" * 40, "patch_sha256": "c" * 64})
      self.assertEqual(first["matrix"]["agents"], [{"name": "oracle", "model": None}, {"name": "nop", "model": None}])
      self.assertEqual(first["matrix"]["cells"][0]["task"], "repo-to-bench/synthetic-task")
      self.assertEqual(first["matrix"]["attempts"], 1)
      self.assertEqual(first["matrix"]["concurrency"], 2)
      self.assertEqual(first["matrix"]["total_trials"], 2)
      self.assertEqual(len(first["replacement_contract"]), 8)
      self.assertTrue((output / "harbor-job.json").is_file())
      self.assertEqual([json.loads(line)["action"] for line in log.read_text(encoding="utf-8").splitlines()], ["print-config", "print-config"])
      self.assertEqual(list(output.glob(".repo-to-bench-plan-*")), [])

  def test_execute_rejects_mismatched_digest_before_harbor(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      output = root / "run"
      log = root / "harbor.jsonl"
      self.plan(self.create_task(root), output, log=log)
      before = log.read_bytes()
      result = self.execute(output / "plan.json", "sha256:" + "0" * 64, {"FAKE_HARBOR_LOG": str(log)})
      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "plan_digest_mismatch")
      self.assertEqual(log.read_bytes(), before)
      self.assertFalse((output / "execution-report.json").exists())

  def test_interrupted_same_plan_delegates_to_harbor_resume(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      output = root / "run"
      log = root / "harbor.jsonl"
      plan = self.plan(self.create_task(root), output, log=log)
      interrupted = self.execute(
        output / "plan.json",
        plan["digest"],
        {"FAKE_HARBOR_LOG": str(log), "FAKE_HARBOR_INTERRUPT": "1"},
      )
      self.assertNotEqual(interrupted.returncode, 0)
      self.assertEqual(json.loads(interrupted.stdout)["status"], "interrupted")
      resumed = self.execute(output / "plan.json", plan["digest"], {"FAKE_HARBOR_LOG": str(log)})
      self.assertEqual(resumed.returncode, 0, resumed.stderr)
      response = json.loads(resumed.stdout)
      self.assertEqual(response["status"], "complete")
      self.assertEqual(response["missing_cells"], [])
      actions = [json.loads(line)["action"] for line in log.read_text(encoding="utf-8").splitlines()]
      self.assertEqual(actions[-2:], ["run", "resume"])
      job = output / response["artifacts"]["harbor_job"]
      self.assertTrue((job / "lock.json").is_file())
      self.assertTrue((job / "result.json").is_file())

  def test_changed_plan_creates_new_harbor_job_instead_of_resuming_old_lock(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      output = root / "run"
      log = root / "harbor.jsonl"
      task_root = self.create_task(root)
      first = self.plan(task_root, output, attempts=1, log=log)
      first_result = self.execute(
        output / "plan.json",
        first["digest"],
        {"FAKE_HARBOR_LOG": str(log), "FAKE_HARBOR_INTERRUPT": "1"},
      )
      self.assertNotEqual(first_result.returncode, 0)
      second = self.plan(task_root, output, attempts=2, log=log)
      self.assertNotEqual(first["digest"], second["digest"])
      result = self.execute(output / "plan.json", second["digest"], {"FAKE_HARBOR_LOG": str(log)})
      self.assertEqual(result.returncode, 0, result.stderr)
      actions = [json.loads(line)["action"] for line in log.read_text(encoding="utf-8").splitlines()]
      self.assertEqual(actions[-1], "run")
      jobs = sorted(path.name for path in (output / "harbor").iterdir() if path.is_dir())
      self.assertEqual(len(jobs), 2)

  def test_partial_matrix_is_rejected_unless_explicitly_allowed(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      output = root / "run"
      plan = self.plan(self.create_task(root), output, attempts=2)
      environment = {"FAKE_HARBOR_MISSING_CELLS": "1"}
      rejected = self.execute(output / "plan.json", plan["digest"], environment)
      self.assertNotEqual(rejected.returncode, 0)
      rejected_payload = json.loads(rejected.stdout)
      self.assertEqual(rejected_payload["status"], "incomplete")
      self.assertFalse(rejected_payload["complete"])
      self.assertEqual(len(rejected_payload["missing_cells"]), 1)
      allowed = self.execute(output / "plan.json", plan["digest"], environment, allow_partial=True)
      self.assertEqual(allowed.returncode, 0, allowed.stderr)
      allowed_payload = json.loads(allowed.stdout)
      self.assertEqual(allowed_payload["status"], "partial")
      self.assertFalse(allowed_payload["complete"])
      self.assertEqual(allowed_payload["missing_cells"], rejected_payload["missing_cells"])


if __name__ == "__main__":
  unittest.main()
