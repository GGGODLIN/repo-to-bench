from pathlib import Path
import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Sequence

from .foundation import FoundationError, contains_symlink_component, prepare_run_directory, redact_path, scan_credentials_bytes


PACKET_SCHEMA = "repo-to-bench-review-packet/v1"
REVEAL_SCHEMA = "repo-to-bench-review-reveal/v1"
ISOLATION_SCHEMA = "repo-to-bench-isolation-probe/v1"
DOCKER_IMAGE = "python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"


class ReviewError(Exception):
  def __init__(self, category: str, path: Optional[str] = None) -> None:
    super().__init__(category)
    self.category = category
    self.path = path


def _json_bytes(value: Any) -> bytes:
  return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".repo-to-bench-review-", suffix=".tmp")
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


def _read_regular(path: Path, category: str) -> bytes:
  if contains_symlink_component(path):
    raise ReviewError(category, redact_path(path.name))
  try:
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
      raise ReviewError(category, redact_path(path.name))
    return path.read_bytes()
  except ReviewError:
    raise
  except OSError as error:
    raise ReviewError(category, redact_path(path.name)) from error


def _read_json(path: Path, category: str) -> Dict[str, Any]:
  raw = _read_regular(path, category)
  try:
    value = json.loads(raw)
  except (TypeError, ValueError) as error:
    raise ReviewError(category, redact_path(path.name)) from error
  if not isinstance(value, dict):
    raise ReviewError(category, redact_path(path.name))
  return value


def _read_diff(path: Path) -> str:
  raw = _read_regular(path, "invalid_diff")
  if scan_credentials_bytes(path.name, raw):
    raise ReviewError("credential_detected", redact_path(path.name))
  try:
    return raw.decode("utf-8")
  except UnicodeError as error:
    raise ReviewError("invalid_diff", redact_path(path.name)) from error


def _keyed(seed: str, value: str) -> str:
  return hmac.new(seed.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _candidate(root_value: str, index: int) -> Dict[str, Any]:
  root = Path(root_value)
  status = _read_json(root / "status.json", "invalid_replay")
  if status.get("schema") != "repo-to-bench-replay/v1" or status.get("status") != "COMPLETE" or status.get("normal_review_eligible") is not True:
    raise ReviewError("incomplete_replay", str(index))
  return {
    "index": index,
    "kind": "candidate",
    "source": str(root.resolve()),
    "diff": _read_diff(root / "diff.patch"),
  }


def _prepare_output(output_dir: str) -> Path:
  try:
    output = prepare_run_directory(output_dir)
  except FoundationError as error:
    raise ReviewError(error.category, error.path) from error
  for name, allowed in {
    "agent": {"mount-contract.json"},
    "judge": {"packet.json"},
    "controller": {"reveal.json", "isolation-probe.json"},
  }.items():
    directory = output / name
    if directory.is_symlink() or directory.exists() and not directory.is_dir():
      raise ReviewError("unsafe_review_root", name)
    directory.mkdir(exist_ok=True)
    extras = {path.name for path in directory.iterdir()} - allowed
    if extras:
      raise ReviewError("unsafe_review_root", name)
  return output


def _docker_executable() -> str:
  configured = os.environ.get("REPO_TO_BENCH_DOCKER")
  executable = configured if configured else shutil.which("docker")
  if not executable:
    raise ReviewError("isolation_unavailable")
  return executable


def _run_probe(mount: Path, destination: str, command: str) -> bool:
  mount_spec = "type=bind,src=" + str(mount) + ",dst=" + destination + ",readonly"
  try:
    result = subprocess.run(
      [_docker_executable(), "run", "--rm", "--network", "none", "--mount", mount_spec, DOCKER_IMAGE, "sh", "-c", command],
      capture_output=True,
      text=True,
      timeout=120,
      env=dict(os.environ),
    )
  except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
    raise ReviewError("isolation_probe_failed") from error
  return result.returncode == 0


def _probe_isolation(output: Path) -> Dict[str, Any]:
  agent_ok = _run_probe(
    output / "agent",
    "/agent",
    "test -r /agent/mount-contract.json && test ! -e /controller && test ! -e /judge",
  )
  judge_ok = _run_probe(
    output / "judge",
    "/judge",
    "test -r /judge/packet.json && test ! -e /controller && test ! -e /agent && test ! -e /judge/reveal.json",
  )
  result = {"schema": ISOLATION_SCHEMA, "agent_isolated": agent_ok, "judge_isolated": judge_ok, "passed": agent_ok and judge_ok}
  _atomic_write(output / "controller" / "isolation-probe.json", _json_bytes(result))
  if not result["passed"]:
    raise ReviewError("isolation_probe_failed")
  return result


def build_review(
  execution_report: str,
  candidates: Sequence[str],
  seed: str,
  output_dir: str,
  human_oracle: Optional[str] = None,
  allow_partial: bool = False,
  probe_isolation: bool = False,
) -> Dict[str, Any]:
  if not seed or len(candidates) < 2:
    raise ReviewError("invalid_review_input")
  execution = _read_json(Path(execution_report), "invalid_execution_report")
  if execution.get("schema") != "repo-to-bench-execution/v1":
    raise ReviewError("invalid_execution_report")
  complete = execution.get("complete") is True and execution.get("normal_review_eligible") is True
  missing = execution.get("missing_cells")
  if not isinstance(missing, list):
    raise ReviewError("invalid_execution_report")
  if not complete and not allow_partial:
    raise ReviewError("incomplete_matrix")
  records = [_candidate(value, index) for index, value in enumerate(candidates)]
  if human_oracle is not None:
    records.append({
      "index": len(records),
      "kind": "human_oracle",
      "source": str(Path(human_oracle).resolve()),
      "diff": _read_diff(Path(human_oracle)),
    })
  ordered = sorted(records, key=lambda item: _keyed(seed, "order:" + str(item["index"])))
  packet_candidates = []
  mapping = {}
  for item in ordered:
    identifier = "candidate-" + _keyed(seed, "id:" + str(item["index"]))[:12]
    packet_candidates.append({"id": identifier, "diff": item["diff"]})
    mapping[identifier] = {"kind": item["kind"], "source": item["source"], "input_index": item["index"]}
  identity = {
    "candidate_digests": [hashlib.sha256(item["diff"].encode("utf-8")).hexdigest() for item in packet_candidates],
    "missing_cells": missing if not complete else [],
  }
  packet = {
    "schema": PACKET_SCHEMA,
    "packet_id": "packet-" + _keyed(seed, json.dumps(identity, sort_keys=True, separators=(",", ":")))[:16],
    "metadata": {"complete_comparison": complete, "missing_cells": missing if not complete else []},
    "candidates": packet_candidates,
  }
  reveal = {
    "schema": REVEAL_SCHEMA,
    "packet_id": packet["packet_id"],
    "seed_sha256": hashlib.sha256(seed.encode("utf-8")).hexdigest(),
    "mapping": mapping,
  }
  output = _prepare_output(output_dir)
  _atomic_write(output / "judge" / "packet.json", _json_bytes(packet))
  _atomic_write(output / "controller" / "reveal.json", _json_bytes(reveal))
  _atomic_write(output / "agent" / "mount-contract.json", _json_bytes({"schema": "repo-to-bench-agent-mount/v1", "visible": ["workspace"]}))
  isolation = _probe_isolation(output) if probe_isolation else None
  return {
    "status": "ready",
    "packet": "judge/packet.json",
    "reveal": "controller/reveal.json",
    "normal_review_eligible": complete,
    "isolation": "passed" if isolation is not None else "not_run",
  }
