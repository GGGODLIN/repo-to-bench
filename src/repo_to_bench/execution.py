from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .foundation import FoundationError, contains_symlink_component, prepare_run_directory, redact_path


HARBOR_VERSION = "0.22.0"
PLAN_SCHEMA = "repo-to-bench-plan/v1"
EXECUTION_SCHEMA = "repo-to-bench-execution/v1"
READY_MARKER = ".repo-to-bench-ready"
LOCK_BINDING = ".repo-to-bench-plan-digest"
REPLACEMENT_CONTRACT = [
  "task_agent_configuration",
  "isolated_environment_lifecycle",
  "verifier_injection",
  "native_trajectory_capture",
  "reward_result_persistence",
  "concurrency",
  "retry",
  "resume",
]


class ExecutionError(Exception):
  def __init__(self, category: str, path: Optional[str] = None) -> None:
    super().__init__(category)
    self.category = category
    self.path = path


def _json_bytes(value: Any) -> bytes:
  return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _digest_bytes(value: bytes) -> str:
  return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest_value(value: Any) -> str:
  return _digest_bytes(_json_bytes(value))


def _atomic_write(path: Path, content: bytes) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".repo-to-bench-plan-", suffix=".tmp")
  try:
    with os.fdopen(descriptor, "wb") as output:
      descriptor = None
      output.write(content)
      output.flush()
      os.fsync(output.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
  finally:
    if descriptor is not None:
      os.close(descriptor)
    try:
      os.unlink(temporary)
    except OSError:
      pass


def _read_json(path: Path, category: str) -> Dict[str, Any]:
  if contains_symlink_component(path):
    raise ExecutionError(category, redact_path(path.name))
  try:
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
      raise ExecutionError(category, redact_path(path.name))
    value = json.loads(path.read_text(encoding="utf-8"))
  except ExecutionError:
    raise
  except (OSError, UnicodeError, ValueError) as error:
    raise ExecutionError(category, redact_path(path.name)) from error
  if not isinstance(value, dict):
    raise ExecutionError(category, redact_path(path.name))
  return value


def _load_task(task_root: str) -> Tuple[Path, Dict[str, Any]]:
  root = Path(task_root)
  if contains_symlink_component(root):
    raise ExecutionError("invalid_task")
  try:
    root = root.resolve(strict=True)
  except OSError as error:
    raise ExecutionError("invalid_task") from error
  marker = root / READY_MARKER
  try:
    if marker.read_text(encoding="utf-8") != "repo-to-bench-ready/v1\naccepted\n":
      raise ExecutionError("invalid_task")
  except (OSError, UnicodeError) as error:
    raise ExecutionError("invalid_task") from error
  manifest = _read_json(root / "task-manifest.json", "invalid_task_manifest")
  if manifest.get("schema") != "repo-to-bench-task-manifest/v1":
    raise ExecutionError("invalid_task_manifest")
  task = manifest.get("task")
  revisions = manifest.get("revisions")
  format_value = manifest.get("format")
  if not isinstance(task, dict) or not isinstance(revisions, dict) or not isinstance(format_value, dict):
    raise ExecutionError("invalid_task_manifest")
  if task.get("path") != "task" or format_value.get("harbor_version") != HARBOR_VERSION:
    raise ExecutionError("unsupported_task_contract")
  task_path = root / "task"
  if not task_path.is_dir() or task_path.is_symlink():
    raise ExecutionError("invalid_task")
  return root, manifest


def _harbor_executable() -> str:
  configured = os.environ.get("REPO_TO_BENCH_HARBOR")
  executable = configured if configured else shutil.which("harbor")
  if not executable:
    raise ExecutionError("harbor_unavailable")
  return executable


def _run_harbor(arguments: Sequence[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
  try:
    return subprocess.run(
      [_harbor_executable(), *arguments],
      capture_output=True,
      text=True,
      timeout=timeout,
      env=dict(os.environ),
    )
  except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
    raise ExecutionError("harbor_process_error") from error


def _validate_harbor_version() -> None:
  result = _run_harbor(["--version"], timeout=30)
  match = re.search(r"\b\d+\.\d+\.\d+\b", result.stdout + "\n" + result.stderr)
  if result.returncode != 0 or match is None or match.group(0) != HARBOR_VERSION:
    raise ExecutionError("unsupported_harbor_version")


def _resolved_harbor_config(config: Dict[str, Any], directory: Path) -> Dict[str, Any]:
  input_path = directory / ".harbor-job-input.json"
  _atomic_write(input_path, _json_bytes(config))
  try:
    result = _run_harbor(["run", "--config", str(input_path), "--print-config"], timeout=30)
  finally:
    try:
      input_path.unlink()
    except OSError:
      pass
  if result.returncode != 0:
    raise ExecutionError("invalid_harbor_config")
  try:
    resolved = json.loads(result.stdout)
  except (TypeError, ValueError) as error:
    raise ExecutionError("invalid_harbor_config") from error
  if not isinstance(resolved, dict):
    raise ExecutionError("invalid_harbor_config")
  return resolved


def _arms(agents: Sequence[str], models: Sequence[str]) -> List[Dict[str, Optional[str]]]:
  if not agents or any(not value for value in agents):
    raise ExecutionError("invalid_matrix")
  if models:
    return [{"name": agent, "model": model} for agent in agents for model in models]
  return [{"name": agent, "model": None} for agent in agents]


def _cells(task_name: str, arms: Sequence[Dict[str, Optional[str]]], attempts: int) -> List[Dict[str, Any]]:
  return [
    {"task": task_name, "agent": arm["name"], "model": arm["model"], "attempt": attempt}
    for arm in arms
    for attempt in range(1, attempts + 1)
  ]


def plan_execution(
  task_root: str,
  agents: Sequence[str],
  models: Sequence[str],
  attempts: int,
  concurrency: int,
  output_dir: str,
) -> Dict[str, Any]:
  if attempts < 1 or concurrency < 1:
    raise ExecutionError("invalid_matrix")
  root, manifest = _load_task(task_root)
  try:
    output = prepare_run_directory(output_dir)
  except FoundationError as error:
    raise ExecutionError(error.category, error.path) from error
  _validate_harbor_version()
  arm_values = _arms(agents, models)
  task_name = str(manifest["task"]["name"])
  cells = _cells(task_name, arm_values, attempts)
  matrix_identity = {
    "task": {"name": task_name, "path": str(root / "task"), "revisions": manifest["revisions"]},
    "agents": arm_values,
    "attempts": attempts,
    "concurrency": concurrency,
  }
  job_name = "repo-to-bench-" + _digest_value(matrix_identity).split(":", 1)[1][:12]
  config = {
    "job_name": job_name,
    "jobs_dir": str(output / "harbor"),
    "n_attempts": attempts,
    "n_concurrent_trials": concurrency,
    "quiet": True,
    "retry": {"max_retries": 0},
    "environment": {"type": "docker"},
    "verifier": {"import_path": "repo_to_bench.harbor_verifier:ExitCodeVerifier"},
    "agents": [
      {"name": arm["name"], **({"model_name": arm["model"]} if arm["model"] is not None else {})}
      for arm in arm_values
    ],
    "tasks": [{"path": str(root / "task")}],
  }
  resolved = _resolved_harbor_config(config, output)
  config_bytes = _json_bytes(resolved)
  plan = {
    "schema": PLAN_SCHEMA,
    "status": "planned",
    "backend": "harbor",
    "harbor_version": HARBOR_VERSION,
    "task": {"name": task_name, "revisions": manifest["revisions"]},
    "matrix": {
      "agents": arm_values,
      "models": list(models),
      "attempts": attempts,
      "concurrency": concurrency,
      "total_trials": len(cells),
      "cells": cells,
    },
    "harbor": {
      "config": "harbor-job.json",
      "config_sha256": _digest_bytes(config_bytes),
      "job_name": job_name,
      "jobs_dir": "harbor",
    },
    "replacement_contract": REPLACEMENT_CONTRACT,
  }
  plan["digest"] = _digest_value(plan)
  _atomic_write(output / "harbor-job.json", config_bytes)
  _atomic_write(output / "plan.json", _json_bytes(plan))
  return plan


def _load_plan(plan_path: str, approved_digest: str) -> Tuple[Path, Dict[str, Any], Path, Dict[str, Any]]:
  path = Path(plan_path)
  plan = _read_json(path, "invalid_plan")
  if plan.get("schema") != PLAN_SCHEMA:
    raise ExecutionError("invalid_plan")
  stored_digest = plan.get("digest")
  without_digest = dict(plan)
  without_digest.pop("digest", None)
  if not isinstance(stored_digest, str) or stored_digest != _digest_value(without_digest) or approved_digest != stored_digest:
    raise ExecutionError("plan_digest_mismatch")
  harbor = plan.get("harbor")
  if not isinstance(harbor, dict) or harbor.get("config") != "harbor-job.json":
    raise ExecutionError("invalid_plan")
  config_path = path.parent / "harbor-job.json"
  try:
    config_bytes = config_path.read_bytes()
  except OSError as error:
    raise ExecutionError("invalid_plan") from error
  if _digest_bytes(config_bytes) != harbor.get("config_sha256"):
    raise ExecutionError("plan_digest_mismatch")
  config = _read_json(config_path, "invalid_plan")
  expected_jobs_dir = (path.parent / "harbor").resolve()
  try:
    configured_jobs_dir = Path(config["jobs_dir"]).resolve()
  except (KeyError, TypeError, OSError) as error:
    raise ExecutionError("invalid_plan") from error
  if configured_jobs_dir != expected_jobs_dir or config.get("job_name") != harbor.get("job_name"):
    raise ExecutionError("invalid_plan")
  return path.parent, plan, config_path, config


def _completed_counts(job_dir: Path) -> Dict[Tuple[str, str, Optional[str]], int]:
  counts: Dict[Tuple[str, str, Optional[str]], int] = {}
  if not job_dir.is_dir():
    return counts
  for result_path in sorted(job_dir.glob("*/result.json")):
    try:
      result = _read_json(result_path, "invalid_harbor_result")
      agent_info = result.get("agent_info")
      if not isinstance(agent_info, dict):
        continue
      model_info = agent_info.get("model_info")
      model = model_info.get("name") if isinstance(model_info, dict) else None
      task = result.get("task_name")
      agent = agent_info.get("name")
      if not isinstance(task, str) or not isinstance(agent, str) or model is not None and not isinstance(model, str):
        continue
      key = (task, agent, model)
      counts[key] = counts.get(key, 0) + 1
    except ExecutionError:
      continue
  return counts


def _matrix_result(plan: Dict[str, Any], job_dir: Path, allow_partial: bool) -> Dict[str, Any]:
  expected = plan["matrix"]["cells"]
  counts = _completed_counts(job_dir)
  completed = []
  missing = []
  for cell in expected:
    key = (cell["task"], cell["agent"], cell["model"])
    if counts.get(key, 0) > 0:
      completed.append(cell)
      counts[key] -= 1
    else:
      missing.append(cell)
  complete = not missing
  status = "complete" if complete else "partial" if allow_partial else "incomplete"
  return {
    "schema": EXECUTION_SCHEMA,
    "status": status,
    "complete": complete,
    "normal_review_eligible": complete,
    "plan_digest": plan["digest"],
    "expected_trials": len(expected),
    "completed_trials": len(completed),
    "missing_cells": missing,
  }


def execute_plan(plan_path: str, approved_digest: str, allow_partial: bool = False) -> Dict[str, Any]:
  output, plan, config_path, config = _load_plan(plan_path, approved_digest)
  _validate_harbor_version()
  job_dir = Path(config["jobs_dir"]) / config["job_name"]
  binding = job_dir / LOCK_BINDING
  resume = (job_dir / "config.json").is_file()
  if resume:
    try:
      if not (job_dir / "lock.json").is_file() or binding.read_text(encoding="utf-8") != approved_digest + "\n":
        raise ExecutionError("plan_lock_mismatch")
    except OSError as error:
      raise ExecutionError("plan_lock_mismatch") from error
    arguments = ["job", "resume", "--job-path", str(job_dir)]
    action = "resume"
  else:
    arguments = ["run", "--config", str(config_path), "--quiet", "--yes"]
    action = "run"
  result = _run_harbor(arguments)
  if (job_dir / "lock.json").is_file():
    _atomic_write(binding, (approved_digest + "\n").encode("utf-8"))
  if result.returncode != 0:
    report = {
      "schema": EXECUTION_SCHEMA,
      "status": "interrupted",
      "complete": False,
      "normal_review_eligible": False,
      "plan_digest": approved_digest,
      "harbor_action": action,
      "harbor_exit_code": result.returncode,
      "missing_cells": plan["matrix"]["cells"],
    }
    _atomic_write(output / "execution-report.json", _json_bytes(report))
    return report
  report = _matrix_result(plan, job_dir, allow_partial)
  report["harbor_action"] = action
  report["artifacts"] = {
    "harbor_job": (Path("harbor") / config["job_name"]).as_posix(),
    "harbor_lock": (Path("harbor") / config["job_name"] / "lock.json").as_posix(),
    "harbor_result": (Path("harbor") / config["job_name"] / "result.json").as_posix(),
    "normalized_report": "execution-report.json",
  }
  _atomic_write(output / "execution-report.json", _json_bytes(report))
  return report
