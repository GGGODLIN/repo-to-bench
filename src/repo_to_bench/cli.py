import argparse
import json
from typing import Optional, Sequence

from .foundation import FoundationError, clean_directory, redact_path, stage_input
from .task_builder import TaskBuilderError, build_task


def filesystem_error_payload(fallback_path: str) -> dict:
  return {"error": {"category": "filesystem_error", "path": redact_path(fallback_path)}}


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(prog="repo-to-bench")
  commands = parser.add_subparsers(dest="command", required=True)
  stage = commands.add_parser("stage")
  stage.add_argument("--input-root", required=True)
  stage.add_argument("--run-root", required=True)
  stage.add_argument("--manifest", required=True)
  clean = commands.add_parser("clean")
  clean.add_argument("--run-root", required=True)
  clean.add_argument("--path", required=True)
  clean.add_argument("--recursive", action="store_true")
  build = commands.add_parser("build-task")
  build.add_argument("--repo", required=True)
  build.add_argument("--base-revision", "--base", dest="base_revision", required=True)
  build.add_argument("--head-revision", "--head", dest="head_revision", required=True)
  build.add_argument("--test-command", "--test", dest="test_command", required=True)
  build.add_argument("--instruction")
  build.add_argument("--output-dir", "--output", "-o", dest="output_dir", required=True)
  build.add_argument("--output-input", "--artifact", dest="output_inputs", action="append", default=[])
  build.add_argument("--task-name", default="synthetic-task")
  build.add_argument("--timeout", type=float, default=30.0)
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
  if options.command == "build-task":
    try:
      result = build_task(
        repo=options.repo,
        base_revision=options.base_revision,
        head_revision=options.head_revision,
        test_command=options.test_command,
        output_dir=options.output_dir,
        instruction=options.instruction,
        output_inputs=options.output_inputs,
        task_name=options.task_name,
        timeout=options.timeout,
      )
    except TaskBuilderError as error:
      detail = {"category": error.category}
      if error.report is not None:
        detail["report"] = error.report
      print(json.dumps({"error": detail}, sort_keys=True, separators=(",", ":")))
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
