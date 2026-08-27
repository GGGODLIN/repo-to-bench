import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from harbor.models.task.task import Task

from tests.test_task_builder import create_synthetic_repo, run_builder


ROOT = Path(__file__).resolve().parents[1]


class HarborE2ETests(unittest.TestCase):
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
      home = workspace / "home"
      home.mkdir()
      environment = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": "",
      }
      expected = {"oracle": 1.0, "nop": 0.0}
      for agent, expected_mean in expected.items():
        result = subprocess.run(
          [
            harbor,
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
            agent + "-check",
            "--n-attempts",
            "1",
            "--n-concurrent",
            "1",
            "--max-retries",
            "0",
            "--quiet",
            "--yes",
          ],
          cwd=ROOT,
          env=environment,
          capture_output=True,
          text=True,
          timeout=600,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((jobs / (agent + "-check") / "result.json").read_text(encoding="utf-8"))
        stats = report["stats"]
        evaluation = stats["evals"][next(iter(stats["evals"]))]
        self.assertEqual(evaluation["n_errors"], 0)
        self.assertEqual(evaluation["metrics"][0]["mean"], expected_mean)
        self.assertIsNone(stats["cost_usd"])
        self.assertIsNone(stats["n_input_tokens"])
        self.assertIsNone(stats["n_output_tokens"])


if __name__ == "__main__":
  unittest.main()
