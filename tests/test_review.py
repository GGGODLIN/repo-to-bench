from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAKE_DOCKER = ROOT / "tests" / "fake_docker.py"
OUTCOMES = {
  "ACCEPT": (True, True, False),
  "ACCEPT_SHRINKAGE": (True, True, True),
  "REFUSAL": (False, False, False),
  "INVALID_TASK": (False, False, False),
  "EXTERNAL_LOOKUP": (False, False, False),
  "REWARD_HACKING": (False, False, False),
}


class ReviewCliTests(unittest.TestCase):
  def run_cli(self, *arguments: str, extra_env=None):
    environment = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src")}
    environment.update(extra_env or {})
    return subprocess.run(
      [sys.executable, "-m", "repo_to_bench", *arguments],
      cwd=ROOT,
      env=environment,
      capture_output=True,
      text=True,
    )

  def write_json(self, path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")

  def create_execution(self, root: Path, complete: bool = True):
    path = root / "execution-report.json"
    self.write_json(path, {
      "schema": "repo-to-bench-execution/v1",
      "status": "complete" if complete else "incomplete",
      "complete": complete,
      "normal_review_eligible": complete,
      "missing_cells": [] if complete else [{"task": "task", "agent": "arm-b", "model": None, "attempt": 1}],
    })
    return path

  def create_replay(self, root: Path, name: str, diff: str, status: str = "COMPLETE"):
    replay = root / name
    replay.mkdir(parents=True)
    self.write_json(replay / "status.json", {
      "schema": "repo-to-bench-replay/v1",
      "status": status,
      "normal_review_eligible": status == "COMPLETE",
    })
    if status != "FAILED":
      (replay / "diff.patch").write_text(diff, encoding="utf-8")
    return replay

  def build(self, execution: Path, candidates, output: Path, seed: str = "seed", human_oracle: Path = None, allow_partial: bool = False, probe: bool = False, extra_env=None):
    arguments = ["review-build", "--execution-report", str(execution)]
    for candidate in candidates:
      arguments.extend(["--candidate", str(candidate)])
    arguments.extend(["--seed", seed, "--output-dir", str(output)])
    if human_oracle is not None:
      arguments.extend(["--human-oracle", str(human_oracle)])
    if allow_partial:
      arguments.append("--allow-partial")
    if probe:
      arguments.append("--probe-isolation")
    return self.run_cli(*arguments, extra_env=extra_env)

  def test_review_build_exports_only_anonymous_judge_packet(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      execution = self.create_execution(root)
      first = self.create_replay(root, "oracle-arm", "diff --git a/a b/a\n+first\n")
      second = self.create_replay(root, "model-arm", "diff --git a/a b/a\n+second\n")
      output = root / "review"
      result = self.build(execution, [first, second], output)
      self.assertEqual(result.returncode, 0, result.stderr)
      response = json.loads(result.stdout)
      packet_text = (output / "judge" / "packet.json").read_text(encoding="utf-8")
      packet = json.loads(packet_text)
      reveal = json.loads((output / "controller" / "reveal.json").read_text(encoding="utf-8"))
      self.assertEqual(response["status"], "ready")
      self.assertEqual(sorted(path.name for path in (output / "judge").iterdir()), ["packet.json"])
      self.assertTrue((output / "agent" / "mount-contract.json").is_file())
      self.assertNotIn("oracle-arm", packet_text)
      self.assertNotIn("model-arm", packet_text)
      self.assertNotIn("reveal", packet_text)
      self.assertNotIn("controller", packet_text)
      self.assertEqual(set(reveal["mapping"]), {item["id"] for item in packet["candidates"]})
      self.assertIn("oracle-arm", json.dumps(reveal))
      for item in packet["candidates"]:
        digest = hashlib.sha256(item["diff"].encode("utf-8")).hexdigest()[:12]
        self.assertNotEqual(item["id"].split("-", 1)[1], digest)

  def test_seeded_order_and_human_oracle_anchor_are_reproducible(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      execution = self.create_execution(root)
      candidates = [
        self.create_replay(root, "arm-a", "diff --git a/a b/a\n+a\n"),
        self.create_replay(root, "arm-b", "diff --git a/b b/b\n+b\n"),
      ]
      oracle = root / "anchor.patch"
      oracle.write_text("diff --git a/o b/o\n+anchor\n", encoding="utf-8")
      first = root / "first"
      second = root / "second"
      self.assertEqual(self.build(execution, candidates, first, seed="stable", human_oracle=oracle).returncode, 0)
      self.assertEqual(self.build(execution, candidates, second, seed="stable", human_oracle=oracle).returncode, 0)
      first_packet = (first / "judge" / "packet.json").read_bytes()
      self.assertEqual(first_packet, (second / "judge" / "packet.json").read_bytes())
      self.assertNotIn(b"oracle", first_packet.lower())
      reveal = json.loads((first / "controller" / "reveal.json").read_text(encoding="utf-8"))
      self.assertEqual(sum(value["kind"] == "human_oracle" for value in reveal["mapping"].values()), 1)

  def test_incomplete_and_failed_replays_are_rejected(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      execution = self.create_execution(root)
      complete = self.create_replay(root, "complete", "+complete\n")
      for status in ("INCOMPLETE", "FAILED"):
        with self.subTest(status=status):
          invalid = self.create_replay(root, status.casefold(), "+invalid\n", status=status)
          output = root / ("review-" + status.casefold())
          result = self.build(execution, [complete, invalid], output)
          self.assertNotEqual(result.returncode, 0)
          self.assertEqual(json.loads(result.stdout)["error"]["category"], "incomplete_replay")
          self.assertFalse((output / "judge" / "packet.json").exists())

  def test_partial_matrix_requires_explicit_mode_and_exposes_missing_cells(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      execution = self.create_execution(root, complete=False)
      candidates = [
        self.create_replay(root, "arm-a", "+a\n"),
        self.create_replay(root, "arm-b", "+b\n"),
      ]
      rejected = self.build(execution, candidates, root / "rejected")
      self.assertNotEqual(rejected.returncode, 0)
      self.assertEqual(json.loads(rejected.stdout)["error"]["category"], "incomplete_matrix")
      output = root / "allowed"
      allowed = self.build(execution, candidates, output, allow_partial=True)
      self.assertEqual(allowed.returncode, 0, allowed.stderr)
      packet = json.loads((output / "judge" / "packet.json").read_text(encoding="utf-8"))
      self.assertFalse(packet["metadata"]["complete_comparison"])
      self.assertEqual(packet["metadata"]["missing_cells"], [{"task": "task", "agent": "arm-b", "model": None, "attempt": 1}])

  def test_isolation_probe_mounts_only_agent_or_judge_exports(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      log = root / "docker.jsonl"
      execution = self.create_execution(root)
      candidates = [
        self.create_replay(root, "arm-a", "+a\n"),
        self.create_replay(root, "arm-b", "+b\n"),
      ]
      output = root / "review"
      result = self.build(
        execution,
        candidates,
        output,
        probe=True,
        extra_env={"REPO_TO_BENCH_DOCKER": str(FAKE_DOCKER), "FAKE_DOCKER_LOG": str(log)},
      )
      self.assertEqual(result.returncode, 0, result.stderr)
      response = json.loads(result.stdout)
      self.assertEqual(response["isolation"], "passed")
      calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
      self.assertEqual(len(calls), 2)
      mounts = [[item for item in call if item.startswith("type=bind")][0] for call in calls]
      resolved_output = output.resolve()
      self.assertIn("src=" + str(resolved_output / "agent"), mounts[0])
      self.assertIn("src=" + str(resolved_output / "judge"), mounts[1])
      self.assertTrue(all("src=" + str(resolved_output / "controller") not in mount for mount in mounts))
      self.assertTrue((output / "controller" / "isolation-probe.json").is_file())

  def test_validator_accepts_all_versioned_outcomes(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      execution = self.create_execution(root)
      candidates = [self.create_replay(root, "arm-" + str(index), "+" + str(index) + "\n") for index in range(len(OUTCOMES))]
      output = root / "review"
      self.assertEqual(self.build(execution, candidates, output).returncode, 0)
      packet = json.loads((output / "judge" / "packet.json").read_text(encoding="utf-8"))
      items = []
      for candidate, (outcome, flags) in zip(packet["candidates"], OUTCOMES.items()):
        accepted, eligible, shrinkage = flags
        items.append({
          "id": candidate["id"],
          "outcome": outcome,
          "accepted": accepted,
          "eligible": eligible,
          "shrinkage": shrinkage,
        })
      verdict = root / "verdict.json"
      self.write_json(verdict, {
        "schema": "repo-to-bench-verdict/v1",
        "packet_id": packet["packet_id"],
        "ranking": [item["id"] for item in items],
        "items": items,
      })
      result = self.run_cli("validate-verdict", "--packet", str(output / "judge" / "packet.json"), "--verdict", str(verdict))
      self.assertEqual(result.returncode, 0, result.stderr)
      response = json.loads(result.stdout)
      self.assertEqual(response["status"], "valid")
      self.assertEqual(response["outcomes"], list(OUTCOMES))

  def test_validator_rejects_unknown_duplicate_and_inconsistent_fields(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      execution = self.create_execution(root)
      candidates = [self.create_replay(root, "arm-a", "+a\n"), self.create_replay(root, "arm-b", "+b\n")]
      output = root / "review"
      self.assertEqual(self.build(execution, candidates, output).returncode, 0)
      packet_path = output / "judge" / "packet.json"
      packet = json.loads(packet_path.read_text(encoding="utf-8"))
      ids = [item["id"] for item in packet["candidates"]]
      base = {
        "schema": "repo-to-bench-verdict/v1",
        "packet_id": packet["packet_id"],
        "ranking": ids,
        "items": [
          {"id": ids[0], "outcome": "ACCEPT", "accepted": True, "eligible": True, "shrinkage": False},
          {"id": ids[1], "outcome": "REFUSAL", "accepted": False, "eligible": False, "shrinkage": False},
        ],
      }
      cases = {
        "unknown_enum": {**base, "items": [{**base["items"][0], "outcome": "MAYBE"}, base["items"][1]]},
        "duplicate_id": {**base, "ranking": [ids[0], ids[0]]},
        "inconsistent_derived_flag": {**base, "items": [{**base["items"][0], "accepted": False}, base["items"][1]]},
        "malformed_field": {**base, "unexpected": True},
        "unknown_id": {**base, "items": [{**base["items"][0], "id": "candidate-unknown"}, base["items"][1]]},
      }
      for category, value in cases.items():
        with self.subTest(category=category):
          verdict = root / (category + ".json")
          self.write_json(verdict, value)
          result = self.run_cli("validate-verdict", "--packet", str(packet_path), "--verdict", str(verdict))
          self.assertNotEqual(result.returncode, 0)
          categories = {item["category"] for item in json.loads(result.stdout)["errors"]}
          self.assertIn(category, categories)


if __name__ == "__main__":
  unittest.main()
