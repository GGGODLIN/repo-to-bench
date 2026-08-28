import argparse
import json
from typing import Optional, Sequence

from .execution import ExecutionError, execute_plan, plan_execution
from .foundation import FoundationError, clean_directory, redact_path, stage_input
from .replay import ReplayError, replay_trajectory
from .review import ReviewError, build_review
from .task_builder import TaskBuilderError, build_task
from .verdict import validate_verdict


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
  replay = commands.add_parser("replay")
  replay.add_argument("--workspace", required=True)
  replay.add_argument("--trajectory", required=True)
  replay.add_argument("--agent", choices=("claude-code", "codex"), required=True)
  replay.add_argument("--output-dir", required=True)
  replay.add_argument("--diagnostic-override", action="store_true")
  plan = commands.add_parser("plan")
  plan.add_argument("--task-root", required=True)
  plan.add_argument("--agent", action="append", required=True)
  plan.add_argument("--model", action="append", default=[])
  plan.add_argument("--attempts", type=int, default=1)
  plan.add_argument("--concurrency", type=int, default=1)
  plan.add_argument("--output-dir", required=True)
  execute = commands.add_parser("execute")
  execute.add_argument("--plan", required=True)
  execute.add_argument("--approve-digest", required=True)
  execute.add_argument("--allow-partial", action="store_true")
  review = commands.add_parser("review-build")
  review.add_argument("--execution-report", required=True)
  review.add_argument("--candidate", action="append", required=True)
  review.add_argument("--seed", required=True)
  review.add_argument("--output-dir", required=True)
  review.add_argument("--human-oracle")
  review.add_argument("--allow-partial", action="store_true")
  review.add_argument("--probe-isolation", action="store_true")
  verdict = commands.add_parser("validate-verdict")
  verdict.add_argument("--packet", required=True)
  verdict.add_argument("--verdict", required=True)
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
  if options.command == "replay":
    try:
      result = replay_trajectory(
        workspace=options.workspace,
        trajectory=options.trajectory,
        output_dir=options.output_dir,
        agent=options.agent,
        diagnostic_override=options.diagnostic_override,
      )
    except ReplayError as error:
      print(json.dumps({"error": {"category": error.category}}, sort_keys=True, separators=(",", ":")))
      return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "COMPLETE" or options.diagnostic_override and result["status"] == "INCOMPLETE" else 1
  if options.command == "plan":
    try:
      result = plan_execution(
        task_root=options.task_root,
        agents=options.agent,
        models=options.model,
        attempts=options.attempts,
        concurrency=options.concurrency,
        output_dir=options.output_dir,
      )
    except ExecutionError as error:
      detail = {"category": error.category}
      if error.path is not None:
        detail["path"] = error.path
      print(json.dumps({"error": detail}, sort_keys=True, separators=(",", ":")))
      return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0
  if options.command == "execute":
    try:
      result = execute_plan(options.plan, options.approve_digest, options.allow_partial)
    except ExecutionError as error:
      detail = {"category": error.category}
      if error.path is not None:
        detail["path"] = error.path
      print(json.dumps({"error": detail}, sort_keys=True, separators=(",", ":")))
      return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] in {"complete", "partial"} else 1
  if options.command == "review-build":
    try:
      result = build_review(
        execution_report=options.execution_report,
        candidates=options.candidate,
        seed=options.seed,
        output_dir=options.output_dir,
        human_oracle=options.human_oracle,
        allow_partial=options.allow_partial,
        probe_isolation=options.probe_isolation,
      )
    except ReviewError as error:
      detail = {"category": error.category}
      if error.path is not None:
        detail["path"] = error.path
      print(json.dumps({"error": detail}, sort_keys=True, separators=(",", ":")))
      return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0
  if options.command == "validate-verdict":
    result = validate_verdict(options.packet, options.verdict)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "valid" else 1
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
