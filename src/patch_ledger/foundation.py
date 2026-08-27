from pathlib import Path, PureWindowsPath
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from typing import Any, Dict, List, Optional, Union


RUN_MARKER = ".patch-ledger-run"
OWNED_MARKER = ".patch-ledger-owned"


_ASSIGNMENT_KEY = r"(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?(?:key|token)|token|secret(?:[_-]?key)?|password|passwd|private[_-]?key)(?:[_-][a-z0-9]+)*"


_SECRET_PATTERNS = (
  ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), 0),
  ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
  ("github_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), 0),
  ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), 0),
  ("shopify_token", re.compile(r"\bshp(?:at|ca|pa|oa)_[A-Za-z0-9]{20,}\b"), 0),
  ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"), 0),
  ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"), 0),
  ("url_credential", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@"), 0),
  ("credential_assignment", re.compile(r'(?i)\b' + _ASSIGNMENT_KEY + r'\b\s*[:=]\s*"([^"\r\n]*[^\s"\r\n][^"\r\n]*)"'), 1),
  ("credential_assignment", re.compile(r"(?i)\b" + _ASSIGNMENT_KEY + r"\b\s*[:=]\s*'([^'\r\n]*[^\s'\r\n][^'\r\n]*)'"), 1),
  ("credential_assignment", re.compile(r"(?i)\b" + _ASSIGNMENT_KEY + r"\b\s*[:=]\s*([^\s\"'\r\n]+)"), 1),
)


class FoundationError(Exception):
  def __init__(self, category: str, path: Optional[str], findings: Optional[List[Dict[str, Any]]] = None) -> None:
    super().__init__(category)
    self.category = category
    self.path = path
    self.findings = findings


def validate_source(source: object) -> str:
  safe_source = redact_path(str(source))
  if not isinstance(source, str) or not source or safe_source != source:
    raise FoundationError("unsafe_path", safe_source)
  normalized = source.replace("\\", "/")
  parts = normalized.split("/")
  if normalized in {".", ""} or Path(normalized).is_absolute() or PureWindowsPath(normalized).is_absolute() or any(part in {"", ".", ".."} or part.casefold().startswith(".patch-ledger-") for part in parts):
    raise FoundationError("unsafe_path", safe_source)
  return normalized


def resolve_source(input_directory: Path, source: str) -> Path:
  source_path = input_directory / source
  try:
    resolved_source = source_path.resolve(strict=True)
    resolved_source.relative_to(input_directory.resolve())
  except ValueError:
    raise FoundationError("symlink_escape", source)
  except RuntimeError:
    raise FoundationError("symlink_escape", source)
  except FileNotFoundError:
    raise FoundationError("missing_source", source)
  except OSError as error:
    if error.errno == errno.ELOOP:
      raise FoundationError("symlink_escape", source)
    raise FoundationError("source_unreadable", source)
  return source_path


def read_source_content(source_path: Path, source: str) -> bytes:
  try:
    metadata = source_path.stat()
  except FileNotFoundError:
    raise FoundationError("missing_source", source)
  except OSError:
    raise FoundationError("source_unreadable", source)
  if not stat.S_ISREG(metadata.st_mode):
    raise FoundationError("source_not_regular_file", source)
  flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
  try:
    descriptor = os.open(source_path, flags)
  except OSError as error:
    if error.errno == errno.ELOOP:
      raise FoundationError("symlink_escape", source)
    if error.errno == errno.ENOENT:
      raise FoundationError("missing_source", source)
    raise FoundationError("source_unreadable", source)
  try:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
      raise FoundationError("source_not_regular_file", source)
    with os.fdopen(descriptor, "rb") as source_file:
      descriptor = None
      return source_file.read()
  except FoundationError:
    raise
  except OSError:
    raise FoundationError("source_unreadable", source)
  finally:
    if descriptor is not None:
      os.close(descriptor)


def redact_path(path: str) -> str:
  redacted = path
  for _, pattern, _ in _SECRET_PATTERNS:
    redacted = pattern.sub("[redacted]", redacted)
  return redacted


def scan_credentials(source: str, content: bytes) -> List[Dict[str, Any]]:
  try:
    text = content.decode("utf-8")
  except UnicodeDecodeError:
    raise FoundationError("unsupported_encoding", redact_path(source))
  safe_source = redact_path(source)
  matches = []
  for category, pattern, value_group in _SECRET_PATTERNS:
    for match in pattern.finditer(text):
      start, end = match.span()
      if any(not (end <= existing[0] or start >= existing[1]) for existing in matches):
        continue
      value = match.group(value_group)
      line = text.count("\n", 0, start) + 1
      matches.append((start, end, {
        "category": category,
        "fingerprint": "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12],
        "line": line,
        "path": safe_source,
      }))
  return [item[2] for item in sorted(matches, key=lambda item: (item[2]["line"], item[2]["category"], item[2]["fingerprint"]))]


def create_marker(path: Path, content: str, category: str, path_name: str) -> None:
  flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
  try:
    descriptor = os.open(path, flags, 0o600)
  except FileExistsError:
    if path.is_symlink():
      raise FoundationError("symlink_escape", path_name)
    raise FoundationError(category, path_name)
  try:
    marker = os.fdopen(descriptor, "wb")
    descriptor = None
    with marker:
      marker.write(content.encode("utf-8"))
      marker.flush()
      os.fsync(marker.fileno())
    path.chmod(0o600)
  except BaseException:
    try:
      path.unlink()
    except OSError:
      pass
    raise
  finally:
    if descriptor is not None:
      try:
        os.close(descriptor)
      except OSError:
        pass


def is_owned_marker_file(path: Path) -> bool:
  try:
    details = path.lstat()
  except OSError:
    return False
  if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
    return False
  owner_getter = getattr(os, "getuid", None)
  if owner_getter is None:
    return False
  return details.st_uid == owner_getter()


def verify_marker(path: Path, expected: str, category: str, path_name: str) -> None:
  if not is_owned_marker_file(path):
    raise FoundationError(category, path_name)
  try:
    content = path.read_text(encoding="utf-8")
  except (OSError, UnicodeError):
    raise FoundationError(category, path_name)
  if content != expected:
    raise FoundationError(category, path_name)


def is_allowed_system_alias(path: Path) -> bool:
  aliases = {
    Path("/var"): Path("/private/var"),
    Path("/tmp"): Path("/private/tmp"),
  }
  target = aliases.get(path)
  if sys.platform != "darwin" or target is None or not path.is_symlink():
    return False
  try:
    return path.resolve() == target and path.lstat().st_uid == 0
  except OSError:
    return False


def contains_symlink_component(path: Union[str, Path]) -> bool:
  requested = Path(path)
  current = Path(requested.anchor) if requested.anchor else Path(".")
  for component in requested.parts:
    if component == requested.anchor:
      continue
    current = current / component
    if current.is_symlink() and not is_allowed_system_alias(current):
      return True
  return False


def prepare_run_directory(run_root: Union[str, Path]) -> Path:
  requested = Path(run_root)
  if contains_symlink_component(requested):
    raise FoundationError("unowned_run_root", ".")
  run_directory = requested.resolve()
  if run_directory.exists():
    if not run_directory.is_dir():
      raise FoundationError("unowned_run_root", ".")
    marker = run_directory / RUN_MARKER
    if marker.is_symlink():
      raise FoundationError("unowned_run_root", ".")
    if not marker.exists():
      if any(run_directory.iterdir()):
        raise FoundationError("unowned_run_root", ".")
      create_marker(marker, "patch-ledger-run/v1\n", "unowned_run_root", ".")
    else:
      verify_marker(marker, "patch-ledger-run/v1\n", "unowned_run_root", ".")
    run_directory.chmod(0o700)
    marker.chmod(0o600)
    return run_directory
  run_directory.mkdir(parents=True, exist_ok=False)
  run_directory.chmod(0o700)
  marker = run_directory / RUN_MARKER
  create_marker(marker, "patch-ledger-run/v1\n", "unowned_run_root", ".")
  return run_directory


def prepare_output_paths(run_directory: Path, source: str) -> Path:
  source_path = Path(source)
  current = run_directory
  components = ("staged",) + source_path.parent.parts
  for component in components:
    current = current / component
    if current.is_symlink():
      raise FoundationError("symlink_escape", current.relative_to(run_directory).as_posix())
    if current.exists() and not current.is_dir():
      raise FoundationError("unsafe_output_path", current.relative_to(run_directory).as_posix())
  destination = run_directory / "staged" / source
  destination_name = destination.relative_to(run_directory).as_posix()
  if destination.is_symlink():
    raise FoundationError("symlink_escape", destination_name)
  if destination.exists():
    raise FoundationError("destination_exists", destination_name)
  return destination


def load_manifest(manifest_path: Union[str, Path]) -> Dict[str, str]:
  manifest_file = Path(manifest_path)
  safe_manifest_path = redact_path(str(manifest_path))
  try:
    if manifest_file.is_symlink() or not manifest_file.is_file():
      raise FoundationError("invalid_manifest", safe_manifest_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
  except FoundationError:
    raise
  except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
    raise FoundationError("invalid_manifest", safe_manifest_path)
  if not isinstance(manifest, dict) or set(manifest) != {"source"} or not isinstance(manifest["source"], str):
    raise FoundationError("invalid_manifest", safe_manifest_path)
  return manifest


def prepare_staged_directory(run_directory: Path) -> Path:
  staged_directory = run_directory / "staged"
  if staged_directory.is_symlink():
    raise FoundationError("symlink_escape", "staged")
  if staged_directory.exists() and not staged_directory.is_dir():
    raise FoundationError("unsafe_output_path", "staged")
  staged_directory.mkdir(parents=True, exist_ok=True)
  owned_marker = staged_directory / OWNED_MARKER
  if owned_marker.is_symlink():
    raise FoundationError("symlink_escape", "staged/" + OWNED_MARKER)
  if not owned_marker.exists():
    if any(staged_directory.iterdir()):
      raise FoundationError("unowned_staged_directory", "staged")
    create_marker(owned_marker, "patch-ledger-owned/v1\n", "unowned_staged_directory", "staged")
  else:
    verify_marker(owned_marker, "patch-ledger-owned/v1\n", "unowned_staged_directory", "staged")
  staged_directory.chmod(0o700)
  owned_marker.chmod(0o600)
  return staged_directory


def prepare_destination_parent(run_directory: Path, source: str) -> Path:
  current = run_directory / "staged"
  for component in Path(source).parent.parts:
    current = current / component
    relative_path = current.relative_to(run_directory).as_posix()
    if current.is_symlink():
      raise FoundationError("symlink_escape", relative_path)
    if current.exists() and not current.is_dir():
      raise FoundationError("unsafe_output_path", relative_path)
    current.mkdir(exist_ok=True)
    current.chmod(0o700)
  return current


def publish_file(destination: Path, content: bytes, destination_name: str) -> None:
  descriptor, temporary_name = tempfile.mkstemp(
    dir=destination.parent,
    prefix=".patch-ledger-",
    suffix=".tmp",
  )
  try:
    output = os.fdopen(descriptor, "wb")
    descriptor = None
    with output:
      output.write(content)
      output.flush()
      os.fsync(output.fileno())
    os.chmod(temporary_name, 0o600)
    try:
      os.link(temporary_name, destination)
    except FileExistsError:
      raise FoundationError("destination_exists", destination_name)
  finally:
    if descriptor is not None:
      try:
        os.close(descriptor)
      except OSError:
        pass
    try:
      os.unlink(temporary_name)
    except OSError:
      pass


def stage_input(input_root: Union[str, Path], run_root: Union[str, Path], manifest_path: Union[str, Path]) -> Dict[str, Any]:
  input_directory = Path(input_root).resolve()
  manifest = load_manifest(manifest_path)
  source = validate_source(manifest["source"])
  source_path = resolve_source(input_directory, source)
  content = read_source_content(source_path, source)
  findings = scan_credentials(source, content)
  if findings:
    raise FoundationError("credential_detected", None, findings)
  run_directory = prepare_run_directory(run_root)
  destination = prepare_output_paths(run_directory, source)
  prepare_staged_directory(run_directory)
  prepare_destination_parent(run_directory, source)
  publish_file(destination, content, destination.relative_to(run_directory).as_posix())
  return {
    "destination": destination.relative_to(run_directory).as_posix(),
    "sha256": hashlib.sha256(content).hexdigest(),
    "source": source,
    "status": "staged",
  }


def verify_run_directory(run_root: Union[str, Path]) -> Path:
  requested = Path(run_root)
  if contains_symlink_component(requested):
    raise FoundationError("unowned_run_root", ".")
  run_directory = requested.resolve()
  marker = run_directory / RUN_MARKER
  verify_marker(marker, "patch-ledger-run/v1\n", "unowned_run_root", ".")
  return run_directory


def clean_directory(run_root: Union[str, Path], path: str) -> Dict[str, str]:
  run_directory = verify_run_directory(run_root)
  relative_path = validate_source(path)
  target = run_directory / relative_path
  if target.is_symlink():
    raise FoundationError("symlink_escape", relative_path)
  if not target.is_dir():
    raise FoundationError("missing_directory", relative_path)
  try:
    target.resolve(strict=True).relative_to(run_directory)
  except ValueError:
    raise FoundationError("symlink_escape", relative_path)
  owned_marker = target / OWNED_MARKER
  if owned_marker.is_symlink():
    raise FoundationError("symlink_escape", relative_path + "/" + OWNED_MARKER)
  verify_marker(owned_marker, "patch-ledger-owned/v1\n", "unmarked_recursive_delete", relative_path)
  try:
    shutil.rmtree(target)
  except OSError:
    raise FoundationError("cleanup_failed", relative_path)
  return {"path": relative_path, "status": "cleaned"}
