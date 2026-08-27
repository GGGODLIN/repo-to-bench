import argparse
import json
from typing import Optional, Sequence

from .foundation import FoundationError, clean_directory, redact_path, stage_input


def filesystem_error_payload(fallback_path: str) -> dict:
  return {"error": {"category": "filesystem_error", "path": redact_path(fallback_path)}}


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(prog="patch-ledger")
  commands = parser.add_subparsers(dest="command", required=True)
  stage = commands.add_parser("stage")
  stage.add_argument("--input-root", required=True)
  stage.add_argument("--run-root", required=True)
  stage.add_argument("--manifest", required=True)
  clean = commands.add_parser("clean")
  clean.add_argument("--run-root", required=True)
  clean.add_argument("--path", required=True)
  clean.add_argument("--recursive", action="store_true")
  return parser


def main(arguments: Optional[Sequence[str]] = None) -> int:
  parser = build_parser()
  options = parser.parse_args(arguments)
  if options.command == "stage":
    try:
      result = stage_input(options.input_root, options.run_root, options.manifest)
    except FoundationError as error:
      detail = {"category": error.category}
      if error.findings is not None:
        detail["findings"] = error.findings
      else:
        detail["path"] = error.path
      print(json.dumps({"error": detail}, sort_keys=True, separators=(",", ":")))
      return 1
    except OSError:
      print(json.dumps(filesystem_error_payload(options.run_root), sort_keys=True, separators=(",", ":")))
      return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0
  if options.command == "clean":
    if not options.recursive:
      print(json.dumps({"error": {"category": "recursive_confirmation_required", "path": redact_path(options.path)}}, sort_keys=True, separators=(",", ":")))
      return 1
    try:
      result = clean_directory(options.run_root, options.path)
    except FoundationError as error:
      detail = {"category": error.category, "path": error.path}
      if error.category == "cleanup_failed":
        detail["limitation"] = "recursive cleanup is not atomic; partial deletion may have occurred"
      print(json.dumps({"error": detail}, sort_keys=True, separators=(",", ":")))
      return 1
    except OSError:
      print(json.dumps(filesystem_error_payload(options.path), sort_keys=True, separators=(",", ":")))
      return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0
  return 2
