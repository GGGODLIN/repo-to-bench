from pathlib import Path, PurePosixPath
import hashlib
import math
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from .foundation import FoundationError, prepare_run_directory, redact_path, scan_credentials, scan_credentials_bytes


HARBOR_VERSION = "0.22.0"
HARBOR_SCHEMA_VERSION = "1.4"
HARBOR_IMAGE = "python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"
GIT_PACKAGE_VERSION = "1:2.47.3-0+deb13u1"
READY_MARKER = ".patch-ledger-ready"
BUILD_LOCK = ".patch-ledger-build-lock"


class TaskBuilderError(Exception):
  def __init__(self, category: str, detail: Optional[str] = None, report: Optional[Dict[str, Any]] = None) -> None:
    super().__init__(category)
    self.category = category
    self.detail = detail
    self.report = report


def _git_environment() -> Dict[str, str]:
  blocked = {
    "GIT_EXTERNAL_DIFF",
    "GIT_DIFF_OPTS",
    "GIT_PAGER",
    "GIT_CONFIG_COUNT",
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
  return Path(root).resolve()


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


def _read_archive(repo: Path, revision: str) -> bytes:
  try:
    return _run_git(repo, ["archive", "--format=tar", revision], binary=True)
  except TaskBuilderError:
    raise TaskBuilderError("source_archive_failed")


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


def _runtime_environment(home: Path) -> Dict[str, str]:
  return {
    "HOME": str(home),
    "PATH": os.environ.get("PATH", ""),
    "TMPDIR": str(home.parent / (home.name + "-tmp")),
    "XDG_CACHE_HOME": str(home.parent / (home.name + "-cache")),
    "LANG": "C",
    "LC_ALL": "C",
  }


def _test_script(test_command: str) -> str:
  return "\n".join([
    "#!/bin/sh",
    "set -eu",
    "set +e",
    test_command,
    "status=$?",
    "set -e",
    "reward_file=\"${REWARD_FILE:-/logs/verifier/reward.json}\"",
    "mkdir -p \"$(dirname \"$reward_file\")\"",
    "if [ \"$status\" -eq 0 ]; then",
    "  printf '{\"reward\":1}\\n' > \"$reward_file\"",
    "else",
    "  printf '{\"reward\":0}\\n' > \"$reward_file\"",
    "fi",
    "exit \"$status\"",
  ]) + "\n"


def _execute(command: List[str], cwd: Path, environment: Dict[str, str], timeout: float) -> Dict[str, Any]:
  try:
    result = subprocess.run(
      command,
      cwd=cwd,
      env=environment,
      capture_output=True,
      text=True,
      timeout=timeout,
    )
  except subprocess.TimeoutExpired:
    return {"status": "timeout", "exit_code": None}
  except (OSError, ValueError, OverflowError):
    return {"status": "error", "exit_code": None}
  return {
    "status": "passed" if result.returncode == 0 else "failed",
    "exit_code": result.returncode,
  }


def _run_gate(staging: Path, test_command: str, timeout: float) -> Dict[str, Any]:
  with tempfile.TemporaryDirectory(prefix=".patch-ledger-gate-oracle-") as oracle_directory:
    with tempfile.TemporaryDirectory(prefix=".patch-ledger-gate-base-") as base_directory:
      oracle_root = Path(oracle_directory)
      base_root = Path(base_directory)
      base_workspace = base_root / "workspace"
      oracle_workspace = oracle_root / "workspace"
      (oracle_root / "base-workspace").mkdir()
      _write_text(oracle_root / "base-workspace" / "app.py", "")
      archive = (staging / "source-base.tar").read_bytes()
      _extract_archive(archive, base_workspace)
      _extract_archive(archive, oracle_workspace)
      base_home = base_root / "home"
      oracle_home = oracle_root / "home"
      base_home.mkdir()
      oracle_home.mkdir()
      for home in (base_home, oracle_home):
        (home.parent / (home.name + "-tmp")).mkdir()
        (home.parent / (home.name + "-cache")).mkdir()
      base_environment = _runtime_environment(base_home)
      oracle_environment = _runtime_environment(oracle_home)
      patch_path = oracle_root / "solution" / "patch.diff"
      solve_path = oracle_root / "solution" / "solve.sh"
      oracle_test_path = oracle_root / "runner" / "oracle-test.sh"
      base_test_path = base_root / "runner" / "base-test.sh"
      test_script = (staging / "task" / "tests" / "test.sh").read_text()
      _write_bytes(patch_path, (staging / "task" / "solution" / "patch.diff").read_bytes())
      _write_text(solve_path, (staging / "task" / "solution" / "solve.sh").read_text(), 0o700)
      _write_text(oracle_test_path, test_script, 0o700)
      _write_text(base_test_path, test_script, 0o700)
      _write_text(oracle_root / "test.sh", test_script, 0o700)
      oracle_apply = _execute(
        ["sh", str(solve_path)],
        oracle_workspace,
        {**oracle_environment, "PATCH_FILE": str(patch_path)},
        timeout,
      )
      oracle_test = {"status": "not-run", "exit_code": None}
      if oracle_apply["status"] == "passed":
        oracle_test = _execute(
          ["sh", str(oracle_test_path)],
          oracle_workspace,
          {**oracle_environment, "REWARD_FILE": str(oracle_root / "oracle-reward.json")},
          timeout,
        )
      base_test = _execute(
        ["sh", str(base_test_path)],
        base_workspace,
        {**base_environment, "REWARD_FILE": str(base_root / "base-reward.json")},
        timeout,
      )
      oracle_status = "passed" if oracle_apply["status"] == "passed" and oracle_test["status"] == "passed" else "failed"
      oracle_exit_code = oracle_test["exit_code"] if oracle_apply["status"] == "passed" else oracle_apply["exit_code"]
      base_status = base_test["status"]
      accepted = oracle_status == "passed" and base_status == "failed"
      return {
        "schema": "patch-ledger-dual-gate/v1",
        "verification": "deterministic-local",
        "paid_model_calls": 0,
        "oracle": {
          "status": oracle_status,
          "apply": oracle_apply,
          "test": oracle_test,
          "exit_code": oracle_exit_code,
        },
        "base": {
          "status": base_status,
          "test": base_test,
          "exit_code": base_test["exit_code"],
        },
        "accepted": accepted,
        "reason": "oracle_pass_base_fail" if accepted else "oracle_base_gate_failed",
      }


def _task_toml(task_name: str, output_inputs: List[str]) -> str:
  lines = [f"schema_version = {json.dumps(HARBOR_SCHEMA_VERSION)}"]
  if output_inputs:
    artifacts = ", ".join(
      "{ source = " + json.dumps("/app/" + value) + ", destination = " + json.dumps(value) + " }"
      for value in output_inputs
    )
    lines.extend(["", "artifacts = [" + artifacts + "]"])
  lines.extend([
    "",
    "[task]",
    f"name = {json.dumps('patch-ledger/' + task_name)}",
    "version = \"1.0.0\"",
    "description = \"Synthetic patch evaluation task\"",
    "",
    "[agent]",
    "timeout_sec = 120.0",
    "",
    "[verifier]",
    "timeout_sec = 120.0",
    "",
    "[environment]",
    "workdir = \"/app\"",
  ])
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
  for path in root.rglob("*"):
    if path.is_dir():
      path.chmod(0o700)
    elif path.is_file():
      path.chmod(0o700 if path.suffix == ".sh" else 0o600)


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
  archive = _read_archive(repository, base)
  head_archive = _read_archive(repository, head)
  try:
    with tempfile.TemporaryDirectory(prefix=".patch-ledger-head-") as directory:
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
    staging = Path(tempfile.mkdtemp(prefix=".patch-ledger-build-", dir=str(run_root)))
  except OSError as error:
    try:
      _release_build_lock(lock)
    except TaskBuilderError:
      raise
    raise TaskBuilderError("filesystem_error") from error
  try:
    _write_bytes(staging / "source-base.tar", archive)
    task = staging / "task"
    _write_text(task / "task.toml", _task_toml(task_name, inputs))
    _write_text(task / "instruction.md", task_instruction)
    _write_text(
      task / "environment" / "Dockerfile",
      f"FROM {HARBOR_IMAGE}\nRUN apt-get update && apt-get install -y --no-install-recommends git={GIT_PACKAGE_VERSION} && rm -rf /var/lib/apt/lists/*\nWORKDIR /app\nCOPY workspace/ /app/\n",
    )
    _extract_archive(archive, task / "environment" / "workspace")
    _write_text(task / "tests" / "test.sh", _test_script(test_command), 0o755)
    _write_text(task / "solution" / "solve.sh", "#!/bin/sh\nset -eu\ngit apply --binary \"${PATCH_FILE:-/solution/patch.diff}\"\n", 0o755)
    _write_bytes(task / "solution" / "patch.diff", patch)
    _scan_artifacts(staging)
    gate = _run_gate(staging, test_command, timeout)
    _scan_artifacts(staging)
    manifest = {
      "schema": "patch-ledger-task-manifest/v1",
      "format": {
        "name": "harbor-task",
        "harbor_version": HARBOR_VERSION,
        "task_schema_version": HARBOR_SCHEMA_VERSION,
      },
      "environment": {"image": HARBOR_IMAGE, "git_package": GIT_PACKAGE_VERSION},
      "task": {"name": "patch-ledger/" + task_name, "path": "task"},
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
      "schema": "patch-ledger-controller-metadata/v1",
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
      "patch-ledger-ready/v1\n" + ("accepted" if gate["accepted"] else "rejected") + "\n",
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
