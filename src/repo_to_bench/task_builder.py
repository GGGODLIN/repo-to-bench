from pathlib import Path, PurePosixPath
import hashlib
import math
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from .foundation import FoundationError, prepare_run_directory, redact_path, scan_credentials, scan_credentials_bytes


HARBOR_VERSION = "0.22.0"
HARBOR_SCHEMA_VERSION = "1.4"
HARBOR_IMAGE = "python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"
GIT_PACKAGE_VERSION = "1:2.47.3-0+deb13u1"
READY_MARKER = ".repo-to-bench-ready"
BUILD_LOCK = ".repo-to-bench-build-lock"


class TaskBuilderError(Exception):
  def __init__(self, category: str, detail: Optional[str] = None, report: Optional[Dict[str, Any]] = None) -> None:
    super().__init__(category)
    self.category = category
    self.detail = detail
    self.report = report


def _git_environment() -> Dict[str, str]:
  blocked = {
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_SYSTEM",
    "GIT_DIR",
    "GIT_DIFF_OPTS",
    "GIT_EXTERNAL_DIFF",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PAGER",
    "GIT_WORK_TREE",
  }
  environment = {
    key: value
    for key, value in os.environ.items()
    if key not in blocked
    and not key.startswith("GIT_CONFIG_KEY_")
    and not key.startswith("GIT_CONFIG_VALUE_")
  }
  environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_PAGER": ":", "GIT_TERMINAL_PROMPT": "0"})
  return environment


def _safe_git_directory(path: Path) -> bool:
  try:
    details = path.lstat()
  except OSError:
    return False
  return stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode)


def _resolved_git_path(repo: Path, value: str) -> Path:
  path = Path(value)
  if not path.is_absolute():
    path = repo / path
  try:
    return path.resolve(strict=True)
  except (OSError, RuntimeError) as error:
    raise TaskBuilderError("invalid_repo") from error


def _registered_worktree(repo: Path, git_dir: Path, common_dir: Path) -> bool:
  worktrees = common_dir / "worktrees"
  if not _safe_git_directory(worktrees):
    return False
  try:
    git_dir.relative_to(worktrees)
  except ValueError:
    return False
  try:
    entries = list(worktrees.iterdir())
  except OSError:
    return False
  for entry in entries:
    if entry.resolve() != git_dir:
      continue
    if not _safe_git_directory(entry):
      return False
    marker = entry / "gitdir"
    try:
      marker_details = marker.lstat()
      if not stat.S_ISREG(marker_details.st_mode) or stat.S_ISLNK(marker_details.st_mode):
        return False
      marker_text = marker.read_text(encoding="utf-8").strip()
      target_value = marker_text.removeprefix("gitdir: ")
      if not target_value:
        return False
      target = Path(target_value)
      if not target.is_absolute():
        target = entry / target
      return target.resolve(strict=True) == (repo / ".git").resolve(strict=True)
    except (OSError, RuntimeError, UnicodeError):
      return False
  return False


def _validate_git_object_store(git_dir: Path, common_dir: Path) -> None:
  if not _safe_git_directory(common_dir):
    raise TaskBuilderError("invalid_repo")
  objects = common_dir / "objects"
  if not _safe_git_directory(objects):
    raise TaskBuilderError("invalid_repo")
  for name in ("alternates", "http-alternates"):
    alternate = objects / "info" / name
    try:
      if alternate.exists() or alternate.is_symlink():
        raise TaskBuilderError("invalid_repo")
    except OSError as error:
      raise TaskBuilderError("invalid_repo") from error
  if git_dir != common_dir and not _safe_git_directory(git_dir):
    raise TaskBuilderError("invalid_repo")


def _validate_git_layout(repo: Path) -> None:
  marker = repo / ".git"
  try:
    marker_details = marker.lstat()
  except OSError as error:
    raise TaskBuilderError("invalid_repo") from error
  try:
    git_dir = _resolved_git_path(repo, str(_run_git(repo, ["rev-parse", "--git-dir"]).strip()))
    common_dir = _resolved_git_path(repo, str(_run_git(repo, ["rev-parse", "--git-common-dir"]).strip()))
  except TaskBuilderError as error:
    raise TaskBuilderError("invalid_repo") from error
  if stat.S_ISLNK(marker_details.st_mode):
    raise TaskBuilderError("invalid_repo")
  if stat.S_ISDIR(marker_details.st_mode):
    if not _safe_git_directory(marker) or git_dir != marker.resolve(strict=True) or common_dir != git_dir:
      raise TaskBuilderError("invalid_repo")
  elif stat.S_ISREG(marker_details.st_mode):
    if not _registered_worktree(repo, git_dir, common_dir):
      raise TaskBuilderError("invalid_repo")
  else:
    raise TaskBuilderError("invalid_repo")
  _validate_git_object_store(git_dir, common_dir)


def _run_git(repo: Path, arguments: List[str], binary: bool = False) -> Any:
  try:
    result = subprocess.run(
      ["git", "-C", str(repo), "-c", "core.fsmonitor=false", *arguments],
      capture_output=True,
      check=True,
      env=_git_environment(),
      text=not binary,
      timeout=30.0,
    )
  except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, UnicodeError, ValueError, OverflowError) as error:
    raise TaskBuilderError("invalid_git_input") from error
  return result.stdout


def _resolve_repo(repo: str) -> Path:
  candidate = Path(repo)
  try:
    resolved = candidate.resolve(strict=True)
  except (OSError, RuntimeError) as error:
    raise TaskBuilderError("invalid_repo") from error
  if not resolved.is_dir():
    raise TaskBuilderError("invalid_repo")
  try:
    root = str(_run_git(resolved, ["rev-parse", "--show-toplevel"]).strip())
  except TaskBuilderError:
    raise TaskBuilderError("invalid_repo")
  if not root:
    raise TaskBuilderError("invalid_repo")
  repository = Path(root).resolve()
  try:
    resolved.relative_to(repository)
  except ValueError as error:
    raise TaskBuilderError("invalid_repo") from error
  try:
    marker_exists = (resolved / ".git").lstat()
  except FileNotFoundError:
    marker_exists = None
  except OSError as error:
    raise TaskBuilderError("invalid_repo") from error
  if marker_exists is not None and repository != resolved:
    raise TaskBuilderError("invalid_repo")
  _validate_git_layout(repository)
  return repository


def _resolve_revision(repo: Path, revision: str) -> str:
  if not revision or "\x00" in revision:
    raise TaskBuilderError("invalid_revision")
  try:
    value = str(_run_git(repo, ["rev-parse", "--verify", f"{revision}^{{commit}}"]).strip())
  except TaskBuilderError:
    raise TaskBuilderError("invalid_revision")
  if not re.fullmatch(r"[0-9a-f]{40}", value):
    raise TaskBuilderError("invalid_revision")
  return value


def _resolve_instruction(repo: Path, revision: str, supplied: Optional[str]) -> str:
  if supplied is None:
    try:
      commit_message = str(_run_git(repo, ["show", "-s", "--format=%B", revision]).strip())
    except TaskBuilderError:
      raise TaskBuilderError("invalid_instruction")
    if commit_message:
      quoted_message = "\n".join("> " + line for line in commit_message.splitlines())
      instruction = "Implement the requested change in the supplied repository.\n\nUntrusted revision metadata:\n" + quoted_message
    else:
      instruction = "Implement the requested change in the supplied repository."
  else:
    instruction = supplied
  if not instruction or "\x00" in instruction:
    raise TaskBuilderError("invalid_instruction")
  instruction = instruction.strip()
  if not instruction:
    raise TaskBuilderError("invalid_instruction")
  _validate_text_safety("instruction", instruction)
  return instruction + "\n"


def _snapshot_tree(repo: Path, revision: str) -> bytes:
  try:
    records = _run_git(
      repo,
      ["ls-tree", "-rz", "--full-tree", revision, "--"],
      binary=True,
    )
  except TaskBuilderError as error:
    raise TaskBuilderError("source_archive_failed") from error

  entries = []
  for record in records.split(b"\0"):
    if not record:
      continue
    metadata, separator, raw_path = record.partition(b"\t")
    fields = metadata.split()
    if separator != b"\t" or len(fields) != 3:
      raise TaskBuilderError("source_archive_failed")
    mode, object_type, object_id = fields
    if mode not in {b"100644", b"100755"} or object_type != b"blob":
      raise TaskBuilderError("unsupported_source_entry")
    if not re.fullmatch(rb"[0-9a-f]{40}", object_id):
      raise TaskBuilderError("source_archive_failed")
    try:
      name = os.fsdecode(raw_path)
      _safe_member_path(name)
      content = _run_git(repo, ["cat-file", "blob", object_id.decode("ascii")], binary=True)
    except TaskBuilderError as error:
      raise TaskBuilderError("source_archive_failed") from error
    except (UnicodeError, ValueError) as error:
      raise TaskBuilderError("unsafe_source_path") from error
    entries.append((raw_path, name, mode, content))

  entries.sort(key=lambda entry: entry[0])
  output = io.BytesIO()
  try:
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
      for _, name, mode, content in entries:
        info = tarfile.TarInfo(name=name)
        info.mode = 0o755 if mode == b"100755" else 0o644
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mtime = 0
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
  except (OSError, tarfile.TarError, ValueError) as error:
    raise TaskBuilderError("source_archive_failed") from error
  return output.getvalue()


def _safe_member_path(name: str) -> Tuple[str, ...]:
  path = PurePosixPath(name)
  if path.is_absolute() or "\\" in name or any(part in {"", ".", ".."} for part in path.parts):
    raise TaskBuilderError("unsafe_source_path")
  return path.parts


def _extract_archive(content: bytes, destination: Path) -> None:
  destination.mkdir(parents=True, exist_ok=False)
  try:
    archive = tarfile.open(fileobj=io.BytesIO(content), mode="r:")
  except (OSError, tarfile.TarError) as error:
    raise TaskBuilderError("source_archive_failed") from error
  try:
    for member in archive.getmembers():
      parts = _safe_member_path(member.name)
      target = destination.joinpath(*parts)
      if member.isdir():
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(member.mode & 0o777)
        continue
      if not member.isreg():
        raise TaskBuilderError("unsupported_source_entry")
      target.parent.mkdir(parents=True, exist_ok=True)
      source = archive.extractfile(member)
      if source is None:
        raise TaskBuilderError("source_archive_failed")
      with target.open("xb") as output:
        shutil.copyfileobj(source, output)
      target.chmod(member.mode & 0o777)
  except (OSError, tarfile.TarError) as error:
    raise TaskBuilderError("source_archive_failed") from error
  finally:
    archive.close()


def _write_bytes(path: Path, content: bytes, mode: Optional[int] = None) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("xb") as output:
    output.write(content)
  if mode is not None:
    path.chmod(mode)


def _write_text(path: Path, content: str, mode: Optional[int] = None) -> None:
  _write_bytes(path, content.encode("utf-8"), mode)


def _test_script(test_command: str) -> str:
  return "\n".join([
    "#!/bin/sh",
    "set +e",
    "sh -c " + shlex.quote(test_command),
    "status=$?",
    "exit \"$status\"",
  ]) + "\n"


_HARBOR_VERIFIER = "repo_to_bench.harbor_verifier:ExitCodeVerifier"
_HARBOR_ENVIRONMENT = "docker"
_HARBOR_PROCESS_TIMEOUT = 600.0
_HARBOR_RESULT_KEYS = {
  "id",
  "started_at",
  "updated_at",
  "finished_at",
  "n_total_trials",
  "stats",
}
_HARBOR_STATS_KEYS = {
  "n_completed_trials",
  "n_errored_trials",
  "n_running_trials",
  "n_pending_trials",
  "n_cancelled_trials",
  "n_retries",
  "evals",
  "n_input_tokens",
  "n_cache_tokens",
  "n_output_tokens",
  "cost_usd",
}
_HARBOR_EVAL_KEYS = {
  "n_trials",
  "n_errors",
  "metrics",
  "pass_at_k",
  "reward_stats",
  "exception_stats",
}


def _harbor_executable() -> Optional[str]:
  configured = os.environ.get("REPO_TO_BENCH_HARBOR")
  return configured or shutil.which("harbor")


def _strict_object(value: Any, keys: set[str]) -> Dict[str, Any]:
  if not isinstance(value, dict) or set(value) != keys:
    raise ValueError("invalid Harbor result fields")
  return value


def _is_integer(value: Any) -> bool:
  return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    return False
  if isinstance(value, int):
    return True
  return math.isfinite(value)


def _parse_harbor_result(path: Path, agent: str) -> Dict[str, Any]:
  try:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = _strict_object(payload, _HARBOR_RESULT_KEYS)
    for field in ("id", "started_at", "updated_at", "finished_at"):
      if not isinstance(result[field], str) or not result[field]:
        raise ValueError("invalid Harbor result metadata")
    if not _is_integer(result["n_total_trials"]) or result["n_total_trials"] != 1:
      raise ValueError("invalid Harbor trial count")
    stats = _strict_object(result["stats"], _HARBOR_STATS_KEYS)
    for field in (
      "n_completed_trials",
      "n_errored_trials",
      "n_running_trials",
      "n_pending_trials",
      "n_cancelled_trials",
      "n_retries",
    ):
      if not _is_integer(stats[field]) or stats[field] < 0:
        raise ValueError("invalid Harbor stats")
    if stats["n_completed_trials"] != 1 or any(
      stats[field] != 0
      for field in (
        "n_errored_trials",
        "n_running_trials",
        "n_pending_trials",
        "n_cancelled_trials",
        "n_retries",
      )
    ):
      raise ValueError("Harbor trial did not complete cleanly")
    for field in ("n_input_tokens", "n_cache_tokens", "n_output_tokens", "cost_usd"):
      if stats[field] is not None:
        raise ValueError("Harbor result reports cost or tokens")
    evals = stats["evals"]
    if not isinstance(evals, dict) or len(evals) != 1:
      raise ValueError("invalid Harbor evaluations")
    eval_name, evaluation_value = next(iter(evals.items()))
    if not isinstance(eval_name, str) or eval_name != agent + "__adhoc":
      raise ValueError("unexpected Harbor evaluation")
    evaluation = _strict_object(evaluation_value, _HARBOR_EVAL_KEYS)
    if not _is_integer(evaluation["n_trials"]) or evaluation["n_trials"] != 1:
      raise ValueError("invalid Harbor evaluation trial count")
    if not _is_integer(evaluation["n_errors"]) or evaluation["n_errors"] != 0:
      raise ValueError("Harbor evaluation contains errors")
    if not isinstance(evaluation["pass_at_k"], dict):
      raise ValueError("invalid Harbor pass-at-k data")
    if not isinstance(evaluation["reward_stats"], dict):
      raise ValueError("invalid Harbor reward data")
    if not isinstance(evaluation["exception_stats"], dict) or evaluation["exception_stats"]:
      raise ValueError("Harbor evaluation contains exceptions")
    metrics = evaluation["metrics"]
    if not isinstance(metrics, list) or len(metrics) != 1:
      raise ValueError("invalid Harbor metrics")
    metric = _strict_object(metrics[0], {"mean"})
    if not _is_number(metric["mean"]):
      raise ValueError("invalid Harbor mean")
    return {"status": "passed", "mean": metric["mean"], "n_errors": evaluation["n_errors"]}
  except (OSError, UnicodeError, ValueError, TypeError, OverflowError, json.JSONDecodeError):
    return {"status": "invalid_result", "mean": None, "n_errors": None}


def _run_harbor_gate(staging: Path, agent: str, timeout: float) -> Dict[str, Any]:
  executable = _harbor_executable()
  if executable is None:
    return {"status": "process_error", "mean": None, "n_errors": None}
  task_path = staging / "task"
  jobs_dir = staging / "jobs"
  job_name = agent + "-gate"
  command = [
    executable,
    "run",
    "--path",
    str(task_path),
    "--agent",
    agent,
    "--env",
    _HARBOR_ENVIRONMENT,
    "--jobs-dir",
    str(jobs_dir),
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
    _HARBOR_VERIFIER,
  ]
  try:
    result = subprocess.run(
      command,
      cwd=staging,
      capture_output=True,
      text=True,
      timeout=max(_HARBOR_PROCESS_TIMEOUT, timeout),
      env=_git_environment(),
    )
  except subprocess.TimeoutExpired:
    return {"status": "timeout", "mean": None, "n_errors": None}
  except (OSError, UnicodeError, ValueError, OverflowError):
    return {"status": "process_error", "mean": None, "n_errors": None}
  if result.returncode != 0:
    return {"status": "process_error", "mean": None, "n_errors": None}
  result_path = jobs_dir / job_name / "result.json"
  try:
    details = result_path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
      return {"status": "invalid_result", "mean": None, "n_errors": None}
  except OSError:
    return {"status": "invalid_result", "mean": None, "n_errors": None}
  return _parse_harbor_result(result_path, agent)


def _run_gate(staging: Path, timeout: float) -> Dict[str, Any]:
  oracle = _run_harbor_gate(staging, "oracle", timeout)
  nop = _run_harbor_gate(staging, "nop", timeout)
  accepted = (
    oracle["status"] == "passed"
    and oracle["mean"] == 1
    and oracle["n_errors"] == 0
    and nop["status"] == "passed"
    and nop["mean"] == 0
    and nop["n_errors"] == 0
  )
  return {
    "schema": "repo-to-bench-dual-gate/v1",
    "verification": "harbor-separate-exit-code",
    "paid_model_calls": 0,
    "oracle": oracle,
    "nop": nop,
    "accepted": accepted,
    "reason": "oracle_pass_nop_fail" if accepted else "oracle_nop_gate_failed",
  }


def _task_toml(task_name: str, output_inputs: List[str], timeout: float) -> str:
  artifacts = [
    '{ source = "/app", destination = "workspace" }',
    *(
      "{ source = " + json.dumps("/app/" + value) + ", destination = " + json.dumps(value) + " }"
      for value in output_inputs
    ),
  ]
  lines = [
    f"schema_version = {json.dumps(HARBOR_SCHEMA_VERSION)}",
    "",
    "artifacts = [" + ", ".join(artifacts) + "]",
    "",
    "[task]",
    f"name = {json.dumps('repo-to-bench/' + task_name)}",
    "version = \"1.0.0\"",
    "description = \"Synthetic patch evaluation task\"",
    "",
    "[agent]",
    f"timeout_sec = {timeout}",
    "",
    "[verifier]",
    f"timeout_sec = {timeout}",
    "environment_mode = \"separate\"",
    "",
    "[environment]",
    "workdir = \"/app\"",
  ]
  return "\n".join(lines) + "\n"


def _validate_output_inputs(values: List[str]) -> List[str]:
  normalized = []
  for value in values:
    path = PurePosixPath(value)
    if (
      not value
      or value == "."
      or path.is_absolute()
      or "\\" in value
      or any(part in {"", ".", ".."} for part in path.parts)
      or path.name == "manifest.json"
      or redact_path(value) != value
      or _PRIVATE_PATH_PATTERN.search(value)
    ):
      raise TaskBuilderError("invalid_output_input")
    normalized.append(path.as_posix())
  return normalized


_PRIVATE_PATH_PATTERN = re.compile(r"(?i)(?:/" + "users/" + r"|/" + "home/" + r"|[a-z]:\\users\\)")


def _validate_text_safety(source: str, text: str) -> None:
  if scan_credentials(source, text.encode("utf-8")):
    raise TaskBuilderError("credential_detected")
  if _PRIVATE_PATH_PATTERN.search(text):
    raise TaskBuilderError("private_path_detected")


def _scan_artifacts(root: Path) -> None:
  for path in sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix()):
    if not path.is_file() or path.is_symlink():
      continue
    try:
      content = path.read_bytes()
    except OSError as error:
      raise TaskBuilderError("artifact_unreadable") from error
    relative = path.relative_to(root).as_posix()
    if redact_path(relative) != relative or scan_credentials_bytes(relative, content):
      raise TaskBuilderError("credential_detected")
    try:
      text = content.decode("utf-8")
    except UnicodeDecodeError:
      continue
    _validate_text_safety(relative, text)


def _ensure_new_outputs(run_root: Path) -> None:
  for name in ("task", "controller", "task-manifest.json", "dual-gate-report.json", READY_MARKER):
    if (run_root / name).exists() or (run_root / name).is_symlink():
      raise TaskBuilderError("output_exists")


def _acquire_build_lock(run_root: Path) -> Path:
  lock = run_root / BUILD_LOCK
  try:
    lock.mkdir(mode=0o700)
  except FileExistsError as error:
    raise TaskBuilderError("output_busy") from error
  except OSError as error:
    raise TaskBuilderError("filesystem_error") from error
  try:
    lock.chmod(0o700)
  except OSError as error:
    try:
      lock.rmdir()
    except OSError as cleanup_error:
      raise TaskBuilderError("cleanup_failed") from cleanup_error
    raise TaskBuilderError("filesystem_error") from error
  return lock


def _release_build_lock(lock: Path) -> None:
  try:
    lock.rmdir()
  except FileNotFoundError:
    return
  except OSError as error:
    raise TaskBuilderError("cleanup_failed") from error


def _lock_permissions(root: Path) -> None:
  harbor_jobs = root / "jobs"
  for path in root.rglob("*"):
    if path == harbor_jobs or harbor_jobs in path.parents:
      continue
    try:
      details = path.lstat()
    except OSError as error:
      raise TaskBuilderError("unsafe_generated_entry") from error
    if stat.S_ISLNK(details.st_mode):
      raise TaskBuilderError("unsafe_generated_entry")
    if stat.S_ISDIR(details.st_mode):
      path.chmod(0o700)
      continue
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
      raise TaskBuilderError("unsafe_generated_entry")
    mode = 0o700 if details.st_mode & stat.S_IXUSR else 0o600
    path.chmod(mode)


def _publish(staging: Path, run_root: Path, accepted: bool) -> None:
  entries = []
  if accepted:
    entries.append((staging / "task", run_root / "task"))
  entries.extend([
    (staging / "controller", run_root / "controller"),
    (staging / "task-manifest.json", run_root / "task-manifest.json"),
    (staging / "dual-gate-report.json", run_root / "dual-gate-report.json"),
    (staging / READY_MARKER, run_root / READY_MARKER),
  ])
  moved = []
  try:
    for source, destination in entries:
      os.rename(source, destination)
      moved.append((source, destination))
  except OSError as error:
    rollback_error = None
    for source, destination in reversed(moved):
      try:
        os.rename(destination, source)
      except OSError as current:
        rollback_error = current
    if rollback_error is not None:
      raise TaskBuilderError("publication_failed") from rollback_error
    raise error


def build_task(
  repo: str,
  base_revision: str,
  head_revision: str,
  test_command: str,
  output_dir: str,
  instruction: Optional[str] = None,
  output_inputs: Optional[List[str]] = None,
  task_name: str = "synthetic-task",
  timeout: float = 30.0,
) -> Dict[str, Any]:
  if not test_command or "\x00" in test_command:
    raise TaskBuilderError("invalid_test_command")
  _validate_text_safety("test-command", test_command)
  if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", task_name):
    raise TaskBuilderError("invalid_task_name")
  if not math.isfinite(timeout) or timeout <= 0:
    raise TaskBuilderError("invalid_timeout")
  inputs = _validate_output_inputs(output_inputs or [])
  repository = _resolve_repo(repo)
  base = _resolve_revision(repository, base_revision)
  head = _resolve_revision(repository, head_revision)
  task_instruction = _resolve_instruction(repository, head, instruction)
  if base == head:
    raise TaskBuilderError("empty_revision_range")
  patch = _run_git(
    repository,
    ["diff", "--no-ext-diff", "--no-textconv", "--binary", base, head, "--"],
    binary=True,
  )
  if not patch:
    raise TaskBuilderError("empty_revision_range")
  archive = _snapshot_tree(repository, base)
  head_archive = _snapshot_tree(repository, head)
  try:
    with tempfile.TemporaryDirectory(prefix=".repo-to-bench-head-") as directory:
      head_workspace = Path(directory) / "workspace"
      _extract_archive(head_archive, head_workspace)
      _scan_artifacts(head_workspace)
  except OSError as error:
    raise TaskBuilderError("filesystem_error") from error
  try:
    run_root = prepare_run_directory(output_dir)
  except FoundationError as error:
    raise TaskBuilderError(error.category) from error
  except OSError as error:
    raise TaskBuilderError("filesystem_error") from error
  lock = _acquire_build_lock(run_root)
  try:
    _ensure_new_outputs(run_root)
  except BaseException:
    try:
      _release_build_lock(lock)
    except TaskBuilderError:
      raise
    raise
  try:
    staging = Path(tempfile.mkdtemp(prefix=".repo-to-bench-build-", dir=str(run_root)))
  except OSError as error:
    try:
      _release_build_lock(lock)
    except TaskBuilderError:
      raise
    raise TaskBuilderError("filesystem_error") from error
  try:
    _write_bytes(staging / "source-base.tar", archive)
    task = staging / "task"
    _write_text(task / "task.toml", _task_toml(task_name, inputs, timeout))
    _write_text(task / "instruction.md", task_instruction)
    _write_text(
      task / "environment" / "Dockerfile",
      f"FROM {HARBOR_IMAGE}\nRUN apt-get update && apt-get install -y --no-install-recommends git={GIT_PACKAGE_VERSION} && rm -rf /var/lib/apt/lists/*\nWORKDIR /app\nCOPY workspace/ /app/\n",
    )
    _extract_archive(archive, task / "environment" / "workspace")
    _write_text(
      task / "tests" / "Dockerfile",
      f"FROM {HARBOR_IMAGE}\nRUN apt-get update && apt-get install -y --no-install-recommends git={GIT_PACKAGE_VERSION} && rm -rf /var/lib/apt/lists/*\nWORKDIR /app\nCOPY test.sh /tests/test.sh\nRUN chmod 755 /tests/test.sh\n",
    )
    _write_text(task / "tests" / "test.sh", _test_script(test_command), 0o755)
    _write_text(task / "solution" / "solve.sh", "#!/bin/sh\nset -eu\ngit apply --binary \"${PATCH_FILE:-/solution/patch.diff}\"\n", 0o755)
    _write_bytes(task / "solution" / "patch.diff", patch)
    _scan_artifacts(staging)
    gate = _run_gate(staging, timeout)
    _scan_artifacts(task)
    manifest = {
      "schema": "repo-to-bench-task-manifest/v1",
      "format": {
        "name": "harbor-task",
        "harbor_version": HARBOR_VERSION,
        "task_schema_version": HARBOR_SCHEMA_VERSION,
      },
      "environment": {"image": HARBOR_IMAGE, "git_package": GIT_PACKAGE_VERSION},
      "task": {"name": "repo-to-bench/" + task_name, "path": "task"},
      "revisions": {"base": base, "head": head, "patch_sha256": hashlib.sha256(patch).hexdigest()},
      "inputs": {
        "instruction": task_instruction.rstrip("\n"),
        "test_command": test_command,
        "output_inputs": inputs,
      },
      "containment": {
        "agent_workspace": "task/environment/workspace",
        "hidden_tests": "task/tests",
        "oracle": "task/solution",
        "controller": "controller",
      },
      "reports": {"task_manifest": "task-manifest.json", "dual_gate": "dual-gate-report.json"},
      "publication": {"ready_marker": READY_MARKER},
    }
    _write_text(staging / "task-manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    _write_text(staging / "dual-gate-report.json", json.dumps(gate, sort_keys=True, separators=(",", ":")) + "\n")
    metadata = {
      "schema": "repo-to-bench-controller-metadata/v1",
      "agent_workspace": "task/environment/workspace",
      "hidden_tests": "task/tests",
      "oracle": "task/solution",
      "task_manifest": "task-manifest.json",
      "dual_gate_report": "dual-gate-report.json",
      "ready_marker": READY_MARKER,
    }
    _write_text(staging / "controller" / "metadata.json", json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n")
    _write_text(
      staging / READY_MARKER,
      "repo-to-bench-ready/v1\n" + ("accepted" if gate["accepted"] else "rejected") + "\n",
    )
    try:
      (staging / "source-base.tar").unlink()
      if not gate["accepted"]:
        shutil.rmtree(staging / "task")
    except OSError as error:
      raise TaskBuilderError("cleanup_failed") from error
    _lock_permissions(staging)
    _publish(staging, run_root, gate["accepted"])
    if not gate["accepted"]:
      raise TaskBuilderError("invalid_task_gate", report=gate)
    return {
      "status": "accepted",
      "task": "task",
      "manifest": "task-manifest.json",
      "dual_gate_report": "dual-gate-report.json",
    }
  except TaskBuilderError:
    raise
  except (OSError, ValueError) as error:
    raise TaskBuilderError("task_build_failed") from error
  finally:
    try:
      if staging.exists():
        try:
          shutil.rmtree(staging)
        except OSError as error:
          raise TaskBuilderError("cleanup_failed") from error
    finally:
      _release_build_lock(lock)
