from pathlib import Path
import argparse
import errno
import json
import os
import re
import stat
import subprocess
from typing import Any, Dict, List, Tuple

from patch_ledger.foundation import FoundationError, redact_path, scan_credentials


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PATTERNS = (
  re.compile(b"/" + b"Users/"),
  re.compile(b"/" + b"home/"),
  re.compile(b"harbor-" + b"3arm"),
  re.compile(b"real-" + b"pr-bench"),
  re.compile(b"ako" + b"hub"),
  re.compile(b"Key" + b"chain"),
  re.compile(b"private-" + b"proxy"),
)
FORBIDDEN_NAME = re.compile(
  r"(?:^|/)(?:" + "harbor-" + "3arm|" + "real-" + "pr-bench|" + "ako" + "hub|transcript)(?:/|$)",
  re.IGNORECASE,
)


def git(*arguments: Any) -> bytes:
  return subprocess.run(
    ["git", *arguments],
    cwd=ROOT,
    check=True,
    capture_output=True,
  ).stdout


def tracked_paths() -> List[Path]:
  return [Path(item.decode()) for item in git("ls-files", "-z").split(b"\0") if item]


def path_name_issues(relative: Path) -> List[Dict[str, Any]]:
  issues = []
  name = relative.as_posix()
  safe_name = redact_path(name)
  if safe_name != name:
    issues.append({"category": "credential_path", "path": safe_name})
  if FORBIDDEN_NAME.search(name):
    issues.append({"category": "forbidden_name", "path": safe_name})
  return issues


def path_issues(relative: Path, data: bytes) -> List[Dict[str, Any]]:
  issues = path_name_issues(relative)
  name = relative.as_posix()
  safe_name = redact_path(name)
  if any(pattern.search(data) for pattern in PRIVATE_PATTERNS):
    issues.append({"category": "private_path", "path": safe_name})
  try:
    findings = scan_credentials(name, data)
  except FoundationError as error:
    issues.append({"category": error.category, "path": error.path or safe_name})
    return issues
  issues.extend(findings)
  return issues


def has_symlink_component(relative: Path) -> bool:
  current = ROOT
  for component in relative.parts:
    current = current / component
    if current.is_symlink():
      return True
  return False


def read_current_file(relative: Path) -> bytes:
  path = ROOT / relative
  safe_name = redact_path(relative.as_posix())
  try:
    metadata = os.lstat(path)
  except OSError:
    raise FoundationError("scanner_error", safe_name)
  if not stat.S_ISREG(metadata.st_mode):
    raise FoundationError("non_regular_file", safe_name)
  flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
  try:
    descriptor = os.open(path, flags)
  except OSError as error:
    if error.errno == errno.ELOOP:
      raise FoundationError("tracked_symlink", safe_name)
    raise FoundationError("scanner_error", safe_name)
  try:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
      raise FoundationError("non_regular_file", safe_name)
    with os.fdopen(descriptor, "rb") as source_file:
      descriptor = None
      return source_file.read()
  except FoundationError:
    raise
  except OSError:
    raise FoundationError("scanner_error", safe_name)
  finally:
    if descriptor is not None:
      os.close(descriptor)


def current_issues(paths: List[Path]) -> List[Dict[str, Any]]:
  issues = []
  for relative in paths:
    if has_symlink_component(relative):
      issues.append({"category": "tracked_symlink", "path": redact_path(relative.as_posix())})
      continue
    try:
      data = read_current_file(relative)
    except FoundationError as error:
      issues.append({"category": error.category, "path": error.path or redact_path(relative.as_posix())})
      continue
    issues.extend(path_issues(relative, data))
  return issues


def history_issues() -> Tuple[List[Dict[str, Any]], int]:
  issues = []
  commits = git("rev-list", "--all").splitlines()
  seen_pairs = set()
  blobs = {}
  for commit in commits:
    records = git("ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0")
    for record in records:
      if not record:
        continue
      metadata, _, raw_path = record.partition(b"\t")
      fields = metadata.split()
      if len(fields) != 3 or fields[1] != b"blob":
        continue
      relative = Path(raw_path.decode())
      if fields[0] == b"120000":
        for finding in path_name_issues(relative):
          finding["path"] = "history/" + commit.decode() + ":" + finding["path"]
          issues.append(finding)
        continue
      object_id = fields[2]
      pair = (object_id, raw_path)
      if pair in seen_pairs:
        continue
      seen_pairs.add(pair)
      if object_id not in blobs:
        blobs[object_id] = git("cat-file", "-p", object_id)
      for finding in path_issues(relative, blobs[object_id]):
        finding["path"] = "history/" + commit.decode() + ":" + finding["path"]
        issues.append(finding)
  return issues, len(blobs)


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(prog="release_scan")
  parser.add_argument("--root", default=str(ROOT))
  return parser.parse_args()


def main() -> int:
  paths = tracked_paths()
  issues = current_issues(paths)
  history_findings, history_objects_count = history_issues()
  issues.extend(history_findings)
  unique = {json.dumps(issue, sort_keys=True): issue for issue in issues}
  findings = [unique[key] for key in sorted(unique)]
  if findings:
    for finding in findings:
      print(json.dumps(finding, sort_keys=True, separators=(",", ":")))
    return 1
  print(json.dumps({
    "current_files": len(paths),
    "history_objects": history_objects_count,
    "status": "clean",
  }, sort_keys=True, separators=(",", ":")))
  return 0


if __name__ == "__main__":
  try:
    ROOT = Path(parse_args().root).resolve()
    raise SystemExit(main())
  except (OSError, subprocess.SubprocessError, UnicodeError):
    print(json.dumps({"category": "scanner_error", "path": "repository"}, sort_keys=True, separators=(",", ":")))
    raise SystemExit(2)
