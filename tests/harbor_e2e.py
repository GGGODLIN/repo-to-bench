import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

from harbor.models.task.task import Task

from tests.test_task_builder import (
  create_synthetic_repo,
  create_synthetic_repo_with_snapshot_attributes,
  run_builder,
)


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = "repo_to_bench.harbor_verifier:ExitCodeVerifier"


def run_real_builder(repo, base, head, output, test_command):
  home = output.parent / "builder-home"
  home.mkdir(exist_ok=True)
  environment = {
    "HOME": str(home),
    "PATH": os.environ.get("PATH", ""),
    "PYTHONPATH": str(ROOT / "src"),
  }
  arguments = [
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
  ]
  return subprocess.run(
    arguments,
    cwd=ROOT,
    env=environment,
    capture_output=True,
    text=True,
    timeout=600,
  )


def run_harbor(task, jobs, agent, job_name):
  home = jobs.parent / "home"
  home.mkdir(exist_ok=True)
  environment = {
    "HOME": str(home),
    "PATH": os.environ.get("PATH", ""),
    "PYTHONPATH": os.environ.get("PYTHONPATH", str(ROOT / "src")),
  }
  result = subprocess.run(
    [
      shutil.which("harbor") or "harbor",
      "run",
      "--path",
      str(task),
      "--agent",
      agent,
      "--env",
      "docker",
      "--jobs-dir",
      str(jobs),
      "--job-name",
      job_name,
      "--n-attempts",
      "1",
      "--n-concurrent",
      "1",
      "--max-retries",
      "0",
      "--quiet",
      "--yes",
      "--verifier",
      VERIFIER,
    ],
    cwd=ROOT,
    env=environment,
    capture_output=True,
    text=True,
    timeout=600,
  )
  result_path = jobs / job_name / "result.json"
  report = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
  return result, report


def evaluation(report, agent):
  return report["stats"]["evals"][agent + "__adhoc"]


def write_script(path, content):
  path.write_text(content, encoding="utf-8")
  path.chmod(0o755)


class HarborE2ETests(unittest.TestCase):
  def test_exit_code_verifier_uses_only_remote_exit_codes(self):
    from types import SimpleNamespace
    from repo_to_bench.harbor_verifier import ExitCodeVerifier

    class Environment:
      def __init__(self, results):
        self.results = iter(results)
        self.calls = []

      async def exec(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(return_code=next(self.results))

    verifier = object.__new__(ExitCodeVerifier)
    verifier.task = SimpleNamespace(config=SimpleNamespace(verifier=SimpleNamespace(timeout_sec=30)))
    verifier.environment = Environment([0, 1])
    result = __import__("asyncio").run(verifier.verify())

    self.assertEqual(result.rewards, {"reward": 0})
    self.assertEqual([call["command"] for call in verifier.environment.calls], ["test -d /app && test ! -L /app", "/tests/test.sh"])
    self.assertEqual(verifier.environment.calls[1]["cwd"], "/app")

  def test_generated_task_runs_with_oracle_and_nop_without_model_calls(self):
    harbor = shutil.which("harbor")
    self.assertIsNotNone(harbor)
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      built = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
      )
      self.assertEqual(built.returncode, 0, built.stderr)
      task = output / "task"
      self.assertTrue(Task.is_valid_dir(task))
      jobs = workspace / "jobs"
      expected = {"oracle": 1.0, "nop": 0.0}
      for agent, expected_mean in expected.items():
        result, report = run_harbor(task, jobs, agent, agent + "-check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(report)
        stats = report["stats"]
        result_evaluation = evaluation(report, agent)
        self.assertEqual(result_evaluation["n_errors"], 0)
        self.assertEqual(result_evaluation["metrics"][0]["mean"], expected_mean)
        self.assertIsNone(stats["cost_usd"])
        self.assertIsNone(stats["n_input_tokens"])
        self.assertIsNone(stats["n_cache_tokens"])
        self.assertIsNone(stats["n_output_tokens"])


  def test_real_builder_rejects_nonzero_mutation_without_host_execution(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      host_marker = Path("/tmp") / ("repo-to-bench-host-marker-" + uuid.uuid4().hex)
      if host_marker.exists():
        host_marker.unlink()
      command = f"touch {host_marker}; printf mutated > /tests/test.sh; exit 1"
      result = run_real_builder(repo, base, head, output, command)

      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(json.loads(result.stdout)["error"]["category"], "invalid_task_gate")
      self.assertFalse(host_marker.exists())
      report = json.loads((output / "dual-gate-report.json").read_text(encoding="utf-8"))
      self.assertEqual(report["oracle"]["mean"], 0.0)
      self.assertEqual(report["nop"]["mean"], 0.0)

  def test_real_verifier_ignores_reward_file_and_mutable_interpreters(self):
    cases = [
      (
        "fake-reward",
        "#!/bin/sh\nset +e\nsh -c 'printf {\\\"reward\\\":1} > /logs/verifier/reward.json; exit 1'\nstatus=$?\nexit \"$status\"\n",
        None,
      ),
      (
        "interpreter-mutation",
        "#!/bin/sh\nexit 1\n",
        "#!/bin/sh\nset -eu\nmkdir -p /tests\nprintf '%s\\n' '#!/bin/sh' 'exit 0' > /tests/test.sh\n",
      ),
      (
        "background-writer",
        "#!/bin/sh\nexit 1\n",
        "#!/bin/sh\nset -eu\n( sleep 5; printf {\\\"reward\\\":1} > /logs/verifier/reward.json ) &\n",
      ),
      (
        "kill-shell",
        "#!/bin/sh\nset +e\nsh -c 'kill -TERM $$'\nstatus=$?\nexit \"$status\"\n",
        None,
      ),
      (
        "exit-one",
        "#!/bin/sh\nset +e\nsh -c 'exit 1'\nstatus=$?\nexit \"$status\"\n",
        None,
      ),
      (
        "exec-one",
        "#!/bin/sh\nset +e\nsh -c 'exec false'\nstatus=$?\nexit \"$status\"\n",
        None,
      ),
    ]
    for name, test_script, solution_script in cases:
      with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        repo, base, head = create_synthetic_repo(workspace)
        output = workspace / "run"
        built = run_builder(
          repo,
          base,
          head,
          output,
          "python3 -c \"import app; assert app.answer() == 'new'\"",
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        write_script(output / "task" / "tests" / "test.sh", test_script)
        if solution_script is not None:
          write_script(output / "task" / "solution" / "solve.sh", solution_script)
        result, report = run_harbor(output / "task", workspace / "jobs", "oracle", name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(evaluation(report, "oracle")["metrics"][0]["mean"], 0.0)

  def test_fresh_verifier_rejects_symlinked_app(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      built = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
      )
      self.assertEqual(built.returncode, 0, built.stderr)
      dockerfile = output / "task" / "tests" / "Dockerfile"
      dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8") + "RUN rm -rf /app && ln -s /tmp /app\n",
        encoding="utf-8",
      )
      result, report = run_harbor(output / "task", workspace / "jobs", "nop", "symlink-app")
      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(evaluation(report, "nop")["metrics"][0]["mean"], 0.0)

  def test_fresh_verifier_has_no_agent_tmp_or_oracle_log_state(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      built = run_builder(
        repo,
        base,
        head,
        output,
        "python3 -c \"import app; assert app.answer() == 'new'\"",
      )
      self.assertEqual(built.returncode, 0, built.stderr)
      write_script(
        output / "task" / "solution" / "solve.sh",
        "#!/bin/sh\nset -eu\ntouch /tmp/agent-marker\n",
      )
      write_script(
        output / "task" / "tests" / "test.sh",
        "#!/bin/sh\nset +e\n[ ! -e /tmp/agent-marker ] && [ ! -e /logs/agent/oracle.txt ]\nstatus=$?\nexit \"$status\"\n",
      )
      result, report = run_harbor(output / "task", workspace / "jobs", "oracle", "fresh-state")
      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(evaluation(report, "oracle")["metrics"][0]["mean"], 1.0)

  def test_container_runtime_version_is_the_gate_truth(self):
    self.assertNotEqual(sys.version_info[:2], (3, 12))
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo(workspace)
      output = workspace / "run"
      command = "python3 -c \"import app, sys; assert sys.version_info[:2] == (3, 12) and app.answer() == 'new'\""
      built = run_builder(repo, base, head, output, command)
      self.assertEqual(built.returncode, 0, built.stderr)
      report = json.loads((output / "dual-gate-report.json").read_text(encoding="utf-8"))
      self.assertTrue(report["accepted"])

  def test_extensionless_executable_survives_real_container_snapshot(self):
    with tempfile.TemporaryDirectory() as directory:
      workspace = Path(directory)
      repo, base, head = create_synthetic_repo_with_snapshot_attributes(workspace)
      output = workspace / "run"
      built = run_builder(
        repo,
        base,
        head,
        output,
        "test -x /app/run && python3 -c \"import app; assert app.answer() == 'new'\"",
      )
      self.assertEqual(built.returncode, 0, built.stderr)
      result, report = run_harbor(output / "task", workspace / "jobs", "oracle", "extensionless")
      self.assertEqual(result.returncode, 0, result.stderr)
      self.assertEqual(evaluation(report, "oracle")["metrics"][0]["mean"], 1.0)


if __name__ == "__main__":
  unittest.main()
