from pathlib import Path, PurePosixPath, PureWindowsPath
import difflib
import json
import os
import shutil
import stat
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .foundation import contains_symlink_component, is_owned_marker_file, redact_path, scan_credentials_bytes


SCHEMA = "repo-to-bench-replay/v1"
REPORT_SCHEMA = "repo-to-bench-replay-report/v1"
OUTPUT_MARKER = ".repo-to-bench-replay-owned"
READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "LS", "Task", "TodoWrite"}


class ReplayError(Exception):
  def __init__(self, category: str, path: Optional[str] = None, line: Optional[int] = None) -> None:
    super().__init__(category)
    self.category = category
    self.path = path
    self.line = line


def _relative_path(value: Any) -> str:
  if not isinstance(value, str) or not value or "\x00" in value:
    raise ReplayError("unsafe_mutation_path")
  normalized = value.replace("\\", "/")
  windows_path = PureWindowsPath(value)
  if normalized.startswith("/app/"):
    normalized = normalized[5:]
  elif normalized.startswith(("/", "~", "$")) or windows_path.is_absolute() or windows_path.drive:
    raise ReplayError("unsafe_mutation_path", redact_path(value))
  path = PurePosixPath(normalized)
  if normalized in {"", ".", ".."} or any(part in {"", ".", ".."} for part in path.parts):
    raise ReplayError("unsafe_mutation_path", redact_path(value))
  if any(part.casefold().startswith(".repo-to-bench-") for part in path.parts):
    raise ReplayError("reserved_mutation_path", path.as_posix())
  relative = path.as_posix()
  if redact_path(relative) != relative:
    raise ReplayError("credential_path", redact_path(relative))
  return relative


def _read_regular(path: Path, relative: str, category: str) -> bytes:
  try:
    details = path.lstat()
  except OSError as error:
    raise ReplayError(category, relative) from error
  if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
    raise ReplayError(category, relative)
  try:
    return path.read_bytes()
  except OSError as error:
    raise ReplayError(category, relative) from error


def _read_tree(root: Path) -> Dict[str, bytes]:
  if contains_symlink_component(root):
    raise ReplayError("workspace_unreadable")
  try:
    details = root.lstat()
  except OSError as error:
    raise ReplayError("workspace_unreadable") from error
  if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
    raise ReplayError("workspace_unreadable")
  files: Dict[str, bytes] = {}

  def visit(directory: Path, prefix: str) -> None:
    try:
      entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as error:
      raise ReplayError("workspace_unreadable", prefix or ".") from error
    for entry in entries:
      relative = entry.name if not prefix else prefix + "/" + entry.name
      try:
        entry_details = entry.lstat()
      except OSError as error:
        raise ReplayError("workspace_unreadable", relative) from error
      if stat.S_ISLNK(entry_details.st_mode):
        raise ReplayError("unsafe_workspace_entry", relative)
      if stat.S_ISDIR(entry_details.st_mode):
        visit(entry, relative)
      elif stat.S_ISREG(entry_details.st_mode):
        content = _read_regular(entry, relative, "workspace_unreadable")
        if scan_credentials_bytes(relative, content):
          raise ReplayError("credential_detected", relative)
        files[relative] = content
      else:
        raise ReplayError("unsafe_workspace_entry", relative)

  visit(root, "")
  return files


def _materialize(files: Dict[str, bytes], root: Path) -> None:
  root.mkdir(parents=True)
  for relative, content in files.items():
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _read_records(path: Path) -> List[Tuple[int, Dict[str, Any]]]:
  if contains_symlink_component(path):
    raise ReplayError("trajectory_unreadable")
  raw = _read_regular(path, path.name, "trajectory_unreadable")
  if scan_credentials_bytes(path.name, raw):
    raise ReplayError("credential_detected", path.name)
  try:
    text = raw.decode("utf-8")
  except UnicodeError as error:
    raise ReplayError("trajectory_unreadable", path.name) from error
  records = []
  for line_number, line in enumerate(text.splitlines(), 1):
    if not line.strip():
      continue
    try:
      value = json.loads(line)
    except (TypeError, ValueError) as error:
      raise ReplayError("malformed_trajectory", line=line_number) from error
    if not isinstance(value, dict):
      raise ReplayError("malformed_trajectory", line=line_number)
    records.append((line_number, value))
  if not records:
    raise ReplayError("malformed_trajectory", line=1)
  return records


def _write_file(root: Path, relative: str, content: str) -> None:
  target = root / relative
  target.parent.mkdir(parents=True, exist_ok=True)
  if target.exists() and (target.is_symlink() or not target.is_file()):
    raise ReplayError("unsafe_mutation_path", relative)
  target.write_text(content, encoding="utf-8")


def _claude_blocks(record: Dict[str, Any]) -> Sequence[Dict[str, Any]]:
  message = record.get("message")
  content = message.get("content") if isinstance(message, dict) else None
  if not isinstance(content, list):
    return []
  return [block for block in content if isinstance(block, dict)]


def _shell_operation(command: str) -> str:
  lowered = " ".join(command.casefold().split())
  if "git apply" in lowered:
    return "git apply"
  return "shell mutation"


def _apply_claude(root: Path, records: Sequence[Tuple[int, Dict[str, Any]]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
  results = set()
  failed_results = set()
  for _, record in records:
    for block in _claude_blocks(record):
      if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
        results.add(block["tool_use_id"])
        if block.get("is_error"):
          failed_results.add(block["tool_use_id"])
  unsupported = []
  failed = []
  events = 0
  for line_number, record in records:
    for block in _claude_blocks(record):
      if block.get("type") != "tool_use":
        continue
      events += 1
      event_id = block.get("id")
      name = block.get("name")
      inputs = block.get("input")
      if not isinstance(event_id, str) or not isinstance(name, str) or not isinstance(inputs, dict):
        raise ReplayError("malformed_trajectory", line=line_number)
      if name in READ_ONLY_TOOLS:
        continue
      if event_id not in results:
        failed.append({"line": line_number, "operation": name, "reason": "missing_tool_result"})
        continue
      if event_id in failed_results:
        continue
      if name == "Write":
        relative = _relative_path(inputs.get("file_path") or inputs.get("filePath"))
        content = inputs.get("content")
        if not isinstance(content, str):
          raise ReplayError("malformed_trajectory", line=line_number)
        _write_file(root, relative, content)
      elif name == "Edit":
        relative = _relative_path(inputs.get("file_path") or inputs.get("filePath"))
        target = root / relative
        try:
          current = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
          raise ReplayError("workspace_unreadable", relative) from error
        old = inputs.get("old_string")
        new = inputs.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
          raise ReplayError("malformed_trajectory", line=line_number)
        if old not in current or current.count(old) != 1:
          failed.append({"line": line_number, "operation": "Edit", "reason": "old_string_not_found"})
          continue
        _write_file(root, relative, current.replace(old, new, 1))
      elif name == "Bash":
        command = inputs.get("command")
        if not isinstance(command, str):
          raise ReplayError("malformed_trajectory", line=line_number)
        unsupported.append({"line": line_number, "operation": _shell_operation(command)})
      else:
        unsupported.append({"line": line_number, "operation": name})
  if not events:
    raise ReplayError("malformed_trajectory", line=1)
  return unsupported, failed


def _apply_codex(root: Path, records: Sequence[Tuple[int, Dict[str, Any]]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
  unsupported = []
  failed = []
  events = 0
  seen = set()
  for line_number, record in records:
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "patch_apply_end":
      continue
    events += 1
    call_id = payload.get("call_id")
    if isinstance(call_id, str) and call_id in seen:
      continue
    if isinstance(call_id, str):
      seen.add(call_id)
    if payload.get("success") is not True:
      continue
    changes = payload.get("changes")
    if not isinstance(changes, dict):
      raise ReplayError("malformed_trajectory", line=line_number)
    for raw_path, change in changes.items():
      relative = _relative_path(raw_path)
      if not isinstance(change, dict):
        raise ReplayError("malformed_trajectory", line=line_number)
      change_type = change.get("type")
      if change_type in {"add", "update"}:
        content = change.get("content")
        if not isinstance(content, str):
          raise ReplayError("malformed_trajectory", line=line_number)
        if change_type == "update" and not (root / relative).is_file():
          failed.append({"line": line_number, "operation": "update", "reason": "missing_file"})
          continue
        _write_file(root, relative, content)
      elif change_type == "delete":
        target = root / relative
        if target.is_file() and not target.is_symlink():
          target.unlink()
        else:
          failed.append({"line": line_number, "operation": "delete", "reason": "missing_file"})
      else:
        unsupported.append({"line": line_number, "operation": "patch:" + str(change_type)})
  if not events:
    raise ReplayError("malformed_trajectory", line=1)
  return unsupported, failed


def _diff(before: Dict[str, bytes], after: Dict[str, bytes]) -> str:
  sections = []
  for relative in sorted(set(before) | set(after)):
    if before.get(relative) == after.get(relative):
      continue
    old_lines = before[relative].decode("utf-8", errors="replace").splitlines() if relative in before else []
    new_lines = after[relative].decode("utf-8", errors="replace").splitlines() if relative in after else []
    old_name = "a/" + relative if relative in before else "/dev/null"
    new_name = "b/" + relative if relative in after else "/dev/null"
    body = "\n".join(difflib.unified_diff(old_lines, new_lines, fromfile=old_name, tofile=new_name, lineterm=""))
    sections.append("diff --git a/{0} b/{0}\n{1}".format(relative, body))
  return "\n".join(sections) + ("\n" if sections else "")


def _failure(error: ReplayError) -> Dict[str, Any]:
  value: Dict[str, Any] = {"category": error.category}
  if error.path is not None:
    value["path"] = redact_path(error.path)
  if error.line is not None:
    value["line"] = error.line
  return value


def _publish(output: Path, status: str, report: Dict[str, Any], workspace: Optional[Path], diff: str, diagnostic_override: bool) -> Dict[str, Any]:
  if contains_symlink_component(output):
    raise ReplayError("unsafe_output_path")
  output = output.resolve()
  output.parent.mkdir(parents=True, exist_ok=True)
  exported = status == "COMPLETE" or (status == "INCOMPLETE" and diagnostic_override)
  staging = Path(tempfile.mkdtemp(prefix=".repo-to-bench-replay-", dir=str(output.parent)))
  try:
    if exported and workspace is not None:
      shutil.copytree(workspace, staging / "workspace")
      (staging / "diff.patch").write_text(diff, encoding="utf-8")
    response: Dict[str, Any] = {
      "schema": SCHEMA,
      "status": status,
      "normal_review_eligible": status == "COMPLETE",
      "diagnostic_override": bool(diagnostic_override),
      "exported": exported,
      "unsupported_operations": report.get("unsupported_operations", []),
    }
    if "failure" in report:
      response["failure"] = report["failure"]
    (staging / "status.json").write_text(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    (staging / "replay-report.json").write_text(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    marker_status = status.casefold() + ("-diagnostic" if status == "INCOMPLETE" and diagnostic_override else "")
    (staging / OUTPUT_MARKER).write_text(SCHEMA + "\n" + marker_status + "\n", encoding="utf-8")
    if output.exists():
      marker = output / OUTPUT_MARKER
      if output.is_symlink() or not output.is_dir() or not is_owned_marker_file(marker):
        raise ReplayError("output_exists")
      try:
        marker_content = marker.read_text(encoding="utf-8")
      except (OSError, UnicodeError) as error:
        raise ReplayError("output_exists") from error
      if marker_content not in {
        SCHEMA + "\ncomplete\n",
        SCHEMA + "\nincomplete\n",
        SCHEMA + "\nincomplete-diagnostic\n",
        SCHEMA + "\nfailed\n",
      }:
        raise ReplayError("output_exists")
      shutil.rmtree(output)
    os.replace(staging, output)
    staging = None
    return response
  finally:
    if staging is not None and staging.exists():
      shutil.rmtree(staging, ignore_errors=True)


def replay_trajectory(workspace: str, trajectory: str, output_dir: str, agent: str, diagnostic_override: bool = False) -> Dict[str, Any]:
  output = Path(output_dir)
  try:
    before = _read_tree(Path(workspace))
    records = _read_records(Path(trajectory))
    if agent not in {"claude-code", "codex"}:
      raise ReplayError("unsupported_agent")
    with tempfile.TemporaryDirectory(prefix=".repo-to-bench-replay-work-") as directory:
      working = Path(directory) / "workspace"
      _materialize(before, working)
      if agent == "claude-code":
        unsupported, failed = _apply_claude(working, records)
      else:
        unsupported, failed = _apply_codex(working, records)
      after = _read_tree(working)
      status = "INCOMPLETE" if unsupported or failed else "COMPLETE"
      report = {
        "schema": REPORT_SCHEMA,
        "status": status,
        "agent": agent,
        "unsupported_operations": unsupported,
        "failed_operations": failed,
      }
      return _publish(output, status, report, working, _diff(before, after), diagnostic_override)
  except ReplayError as error:
    report = {
      "schema": REPORT_SCHEMA,
      "status": "FAILED",
      "agent": agent,
      "unsupported_operations": [],
      "failed_operations": [],
      "failure": _failure(error),
    }
    return _publish(output, "FAILED", report, None, "", False)


def is_normal_review_eligible(result: Dict[str, Any]) -> bool:
  return result.get("status") == "COMPLETE" and result.get("normal_review_eligible") is True
