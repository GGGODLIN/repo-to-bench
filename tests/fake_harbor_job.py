#!/usr/bin/env python3
from pathlib import Path
import json
import os
import sys


def option(arguments, name):
  return arguments[arguments.index(name) + 1]


def log(action, arguments):
  path = os.environ.get("FAKE_HARBOR_LOG")
  if path:
    with Path(path).open("a", encoding="utf-8") as output:
      output.write(json.dumps({"action": action, "argv": arguments}) + "\n")


def cells(config):
  task_path = Path(config["tasks"][0]["path"])
  manifest_path = task_path.parent / "task-manifest.json"
  if manifest_path.is_file():
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    task_name = manifest["task"]["name"]
  else:
    task_name = task_path.name
  values = []
  for agent in config["agents"]:
    for attempt in range(1, config.get("n_attempts", 1) + 1):
      values.append({
        "task": task_name,
        "agent": agent["name"],
        "model": agent.get("model_name"),
        "attempt": attempt,
      })
  return values


def write_results(job_dir, config):
  expected = cells(config)
  missing = int(os.environ.get("FAKE_HARBOR_MISSING_CELLS", "0"))
  complete = expected[:max(0, len(expected) - missing)]
  for index, cell in enumerate(complete, 1):
    trial = job_dir / ("trial-" + str(index))
    trial.mkdir(parents=True, exist_ok=True)
    payload = {
      "task_name": cell["task"],
      "agent_info": {
        "name": cell["agent"],
        "model_info": {"name": cell["model"]} if cell["model"] is not None else None,
      },
    }
    (trial / "result.json").write_text(json.dumps(payload), encoding="utf-8")
  result = {
    "n_total_trials": len(expected),
    "stats": {
      "n_completed_trials": len(complete),
      "n_errored_trials": 0,
      "n_running_trials": 0,
      "n_pending_trials": len(expected) - len(complete),
      "n_cancelled_trials": 0,
    },
  }
  (job_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")


def run_config(arguments):
  config_path = Path(option(arguments, "--config"))
  config = json.loads(config_path.read_text(encoding="utf-8"))
  if "--print-config" in arguments:
    log("print-config", arguments)
    print(json.dumps(config))
    return 0
  job_dir = Path(config["jobs_dir"]) / config["job_name"]
  job_dir.mkdir(parents=True, exist_ok=True)
  (job_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
  (job_dir / "lock.json").write_text(json.dumps({"schema_version": 3, "trials": cells(config)}), encoding="utf-8")
  log("run", arguments)
  if os.environ.get("FAKE_HARBOR_INTERRUPT") == "1":
    return 130
  write_results(job_dir, config)
  return 0


def resume(arguments):
  job_dir = Path(option(arguments, "--job-path"))
  config = json.loads((job_dir / "config.json").read_text(encoding="utf-8"))
  log("resume", arguments)
  write_results(job_dir, config)
  return 0


def main():
  arguments = sys.argv[1:]
  if arguments == ["--version"]:
    print("0.22.0")
    return 0
  if arguments and arguments[0] == "run":
    return run_config(arguments)
  if arguments[:2] == ["job", "resume"]:
    return resume(arguments)
  return 2


if __name__ == "__main__":
  raise SystemExit(main())
