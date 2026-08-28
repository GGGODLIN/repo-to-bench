#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path


def option(name):
  index = sys.argv.index(name)
  return sys.argv[index + 1]


def result_payload(agent, mean=0.0, n_errors=0, stats_overrides=None, eval_name=None):
  errored_trials = 1 if n_errors else 0
  completed_trials = 0 if n_errors else 1
  stats = {
    "n_completed_trials": completed_trials,
    "n_errored_trials": errored_trials,
    "n_running_trials": 0,
    "n_pending_trials": 0,
    "n_cancelled_trials": 0,
    "n_retries": 0,
    "evals": {
      eval_name or agent + "__adhoc": {
        "n_trials": 1,
        "n_errors": n_errors,
        "metrics": [{"mean": mean}],
        "pass_at_k": {},
        "reward_stats": {},
        "exception_stats": {},
      }
    },
    "n_input_tokens": None,
    "n_cache_tokens": None,
    "n_output_tokens": None,
    "cost_usd": None,
  }
  stats.update(stats_overrides or {})
  return {
    "id": "00000000-0000-0000-0000-000000000000",
    "started_at": "2026-01-01T00:00:00",
    "updated_at": "2026-01-01T00:00:01",
    "finished_at": "2026-01-01T00:00:01",
    "n_total_trials": 1,
    "stats": stats,
  }


def main():
  agent = option("--agent")
  task_path = Path(option("--path"))
  jobs_dir = Path(option("--jobs-dir"))
  job_name = option("--job-name")
  profile = json.loads(os.environ.get("FAKE_HARBOR_PROFILE", "{}"))
  spec = profile.get(agent, {})
  if "payload" in spec:
    payload = spec["payload"]
  else:
    test_script = (task_path / "tests" / "test.sh").read_text(encoding="utf-8")
    assertion = test_script.split("answer() ==", 1)[1] if "answer() ==" in test_script else ""
    if "old" in assertion and "time.sleep" not in test_script:
      defaults = {"oracle": 0.0, "nop": 1.0}
    elif "time.sleep" in test_script:
      defaults = {"oracle": 1.0, "nop": 0.0}
    elif "assert True" in test_script:
      defaults = {"oracle": 1.0, "nop": 1.0}
    else:
      defaults = {"oracle": 1.0, "nop": 0.0}
    payload = result_payload(
      agent,
      mean=spec.get("mean", defaults[agent]),
      n_errors=spec.get("n_errors", 1 if "time.sleep" in test_script else 0),
      stats_overrides=spec.get("stats"),
      eval_name=spec.get("eval_name"),
    )
  sleep_seconds = float(os.environ.get("FAKE_HARBOR_SLEEP", "0"))
  if sleep_seconds:
    time.sleep(sleep_seconds)
  result_path = jobs_dir / job_name / "result.json"
  result_path.parent.mkdir(parents=True, exist_ok=True)
  result_path.write_text(json.dumps(payload), encoding="utf-8")
  symlink_target = os.environ.get("FAKE_HARBOR_SYMLINK_TARGET")
  if symlink_target:
    (result_path.parent / "external-link").symlink_to(symlink_target)
  log_path = os.environ.get("FAKE_HARBOR_LOG")
  if log_path:
    with Path(log_path).open("a", encoding="utf-8") as log:
      log.write(json.dumps({"argv": sys.argv[1:], "agent": agent}) + "\n")
  exit_code = int(os.environ.get("FAKE_HARBOR_EXIT", "0"))
  if agent == os.environ.get("FAKE_HARBOR_EXIT_AGENT"):
    exit_code = int(os.environ.get("FAKE_HARBOR_AGENT_EXIT", "1"))
  return exit_code


if __name__ == "__main__":
  raise SystemExit(main())
