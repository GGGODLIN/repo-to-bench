#!/usr/bin/env python3
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))


def environment():
  value = dict(os.environ)
  existing = value.get("PYTHONPATH")
  value["PYTHONPATH"] = str(ROOT / "src") + (os.pathsep + existing if existing else "")
  return value


def command(*arguments):
  result = subprocess.run(
    [sys.executable, "-m", "repo_to_bench", *arguments],
    cwd=ROOT,
    env=environment(),
    capture_output=True,
    text=True,
  )
  if result.returncode != 0:
    try:
      detail = json.loads(result.stdout)
    except ValueError:
      detail = {"category": "command_failed", "returncode": result.returncode}
    raise RuntimeError(json.dumps(detail, sort_keys=True))
  return json.loads(result.stdout)


def git(repo, *arguments):
  return subprocess.run(
    ["git", "-C", str(repo), *arguments],
    check=True,
    capture_output=True,
    text=True,
  ).stdout.strip()


def create_output(value):
  if value is None:
    return Path(tempfile.mkdtemp(prefix="repo-to-bench-smoke-", dir=tempfile.gettempdir()))
  output = Path(value).resolve()
  if output.exists() and (not output.is_dir() or any(output.iterdir())):
    raise RuntimeError("output directory must be empty")
  output.mkdir(parents=True, exist_ok=True)
  return output


def write_verdict(packet_path, verdict_path):
  from repo_to_bench.verdict import OUTCOME_FLAGS, VERDICT_SCHEMA

  packet = json.loads(packet_path.read_text(encoding="utf-8"))
  accepted, eligible, shrinkage = OUTCOME_FLAGS["ACCEPT"]
  items = [
    {
      "id": candidate["id"],
      "outcome": "ACCEPT",
      "accepted": accepted,
      "eligible": eligible,
      "shrinkage": shrinkage,
    }
    for candidate in packet["candidates"]
  ]
  verdict = {
    "schema": VERDICT_SCHEMA,
    "packet_id": packet["packet_id"],
    "ranking": [item["id"] for item in items],
    "items": items,
  }
  verdict_path.write_text(json.dumps(verdict, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def main():
  parser = argparse.ArgumentParser(prog="synthetic_smoke")
  parser.add_argument("--output-dir")
  options = parser.parse_args()
  output = create_output(options.output_dir)
  repo = output / "synthetic-repo"
  subprocess.run([str(ROOT / "examples" / "synthetic-project" / "create-repo.sh"), str(repo)], cwd=ROOT, check=True)
  base = git(repo, "rev-parse", "HEAD^")
  head = git(repo, "rev-parse", "HEAD")
  test_command = (ROOT / "examples" / "synthetic-project" / "test-command.txt").read_text(encoding="utf-8").strip()
  task = command(
    "build-task",
    "--repo",
    str(repo),
    "--base-revision",
    base,
    "--head-revision",
    head,
    "--test-command",
    test_command,
    "--output-dir",
    str(output / "task"),
  )
  plan = command(
    "plan",
    "--task-root",
    str(output / "task"),
    "--agent",
    "oracle",
    "--agent",
    "nop",
    "--attempts",
    "1",
    "--concurrency",
    "2",
    "--output-dir",
    str(output / "run"),
  )
  execution = command(
    "execute",
    "--plan",
    str(output / "run" / "plan.json"),
    "--approve-digest",
    plan["digest"],
  )
  replay_claude = command(
    "replay",
    "--workspace",
    str(ROOT / "examples" / "replay-fixtures" / "base"),
    "--trajectory",
    str(ROOT / "examples" / "replay-fixtures" / "claude-code.jsonl"),
    "--agent",
    "claude-code",
    "--output-dir",
    str(output / "replay-claude"),
  )
  replay_codex = command(
    "replay",
    "--workspace",
    str(ROOT / "examples" / "replay-fixtures" / "base"),
    "--trajectory",
    str(ROOT / "examples" / "replay-fixtures" / "codex.jsonl"),
    "--agent",
    "codex",
    "--output-dir",
    str(output / "replay-codex"),
  )
  review = command(
    "review-build",
    "--execution-report",
    str(output / "run" / "execution-report.json"),
    "--candidate",
    str(output / "replay-claude"),
    "--candidate",
    str(output / "replay-codex"),
    "--seed",
    "repo-to-bench-synthetic-smoke-v1",
    "--output-dir",
    str(output / "review"),
    "--probe-isolation",
  )
  packet = output / "review" / "judge" / "packet.json"
  verdict_path = output / "synthetic-verdict.json"
  write_verdict(packet, verdict_path)
  verdict = command("validate-verdict", "--packet", str(packet), "--verdict", str(verdict_path))
  print(json.dumps({
    "status": "passed",
    "output_root": str(output),
    "task": task["status"],
    "plan_digest": plan["digest"],
    "total_trials": plan["matrix"]["total_trials"],
    "execution": execution["status"],
    "replays": [replay_claude["status"], replay_codex["status"]],
    "review": review["status"],
    "verdict": verdict["status"],
  }, sort_keys=True, separators=(",", ":")))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
