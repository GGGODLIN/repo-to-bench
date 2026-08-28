from pathlib import Path
import json
import stat
from typing import Any, Dict, List, Set

from .foundation import contains_symlink_component


VERDICT_SCHEMA = "repo-to-bench-verdict/v1"
PACKET_SCHEMA = "repo-to-bench-review-packet/v1"
OUTCOME_FLAGS = {
  "ACCEPT": (True, True, False),
  "ACCEPT_SHRINKAGE": (True, True, True),
  "REFUSAL": (False, False, False),
  "INVALID_TASK": (False, False, False),
  "EXTERNAL_LOOKUP": (False, False, False),
  "REWARD_HACKING": (False, False, False),
}


def _error(category: str, path: str) -> Dict[str, str]:
  return {"category": category, "path": path}


def _read_json(path: str, errors: List[Dict[str, str]], label: str) -> Dict[str, Any]:
  source = Path(path)
  if contains_symlink_component(source):
    errors.append(_error("malformed_field", label))
    return {}
  try:
    details = source.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
      raise ValueError
    value = json.loads(source.read_text(encoding="utf-8"))
  except (OSError, UnicodeError, ValueError, TypeError):
    errors.append(_error("malformed_field", label))
    return {}
  if not isinstance(value, dict):
    errors.append(_error("malformed_field", label))
    return {}
  return value


def _packet_ids(packet: Dict[str, Any], errors: List[Dict[str, str]]) -> List[str]:
  if set(packet) != {"schema", "packet_id", "metadata", "candidates"} or packet.get("schema") != PACKET_SCHEMA or not isinstance(packet.get("packet_id"), str):
    errors.append(_error("malformed_field", "packet"))
    return []
  candidates = packet.get("candidates")
  if not isinstance(candidates, list):
    errors.append(_error("malformed_field", "packet.candidates"))
    return []
  identifiers = []
  for index, candidate in enumerate(candidates):
    if not isinstance(candidate, dict) or set(candidate) != {"id", "diff"} or not isinstance(candidate.get("id"), str) or not isinstance(candidate.get("diff"), str):
      errors.append(_error("malformed_field", "packet.candidates[" + str(index) + "]"))
      continue
    identifiers.append(candidate["id"])
  if len(set(identifiers)) != len(identifiers):
    errors.append(_error("duplicate_id", "packet.candidates"))
  return identifiers


def _validate_ranking(value: Any, expected: Set[str], errors: List[Dict[str, str]]) -> None:
  if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
    errors.append(_error("malformed_field", "ranking"))
    return
  if len(set(value)) != len(value):
    errors.append(_error("duplicate_id", "ranking"))
  for index, identifier in enumerate(value):
    if identifier not in expected:
      errors.append(_error("unknown_id", "ranking[" + str(index) + "]"))
  if set(value) != expected:
    errors.append(_error("missing_id", "ranking"))


def _validate_items(value: Any, expected: Set[str], errors: List[Dict[str, str]]) -> None:
  if not isinstance(value, list):
    errors.append(_error("malformed_field", "items"))
    return
  seen = []
  required = {"id", "outcome", "accepted", "eligible", "shrinkage"}
  for index, item in enumerate(value):
    path = "items[" + str(index) + "]"
    if not isinstance(item, dict) or set(item) != required:
      errors.append(_error("malformed_field", path))
      continue
    identifier = item.get("id")
    if not isinstance(identifier, str):
      errors.append(_error("malformed_field", path + ".id"))
      continue
    seen.append(identifier)
    if identifier not in expected:
      errors.append(_error("unknown_id", path + ".id"))
    outcome = item.get("outcome")
    if outcome not in OUTCOME_FLAGS:
      errors.append(_error("unknown_enum", path + ".outcome"))
      continue
    flags = (item.get("accepted"), item.get("eligible"), item.get("shrinkage"))
    if any(not isinstance(value, bool) for value in flags):
      errors.append(_error("malformed_field", path))
    elif flags != OUTCOME_FLAGS[outcome]:
      errors.append(_error("inconsistent_derived_flag", path))
  if len(set(seen)) != len(seen):
    errors.append(_error("duplicate_id", "items"))
  if set(seen) != expected:
    errors.append(_error("missing_id", "items"))


def validate_verdict(packet_path: str, verdict_path: str) -> Dict[str, Any]:
  errors: List[Dict[str, str]] = []
  packet = _read_json(packet_path, errors, "packet")
  identifiers = _packet_ids(packet, errors) if packet else []
  verdict = _read_json(verdict_path, errors, "verdict")
  if verdict:
    if set(verdict) != {"schema", "packet_id", "ranking", "items"}:
      errors.append(_error("malformed_field", "verdict"))
    if verdict.get("schema") != VERDICT_SCHEMA:
      errors.append(_error("malformed_field", "schema"))
    if verdict.get("packet_id") != packet.get("packet_id"):
      errors.append(_error("packet_mismatch", "packet_id"))
    expected = set(identifiers)
    _validate_ranking(verdict.get("ranking"), expected, errors)
    _validate_items(verdict.get("items"), expected, errors)
  unique = {(item["category"], item["path"]): item for item in errors}
  ordered = [unique[key] for key in sorted(unique)]
  if ordered:
    return {"schema": VERDICT_SCHEMA, "status": "invalid", "errors": ordered}
  return {"schema": VERDICT_SCHEMA, "status": "valid", "errors": [], "outcomes": list(OUTCOME_FLAGS)}
