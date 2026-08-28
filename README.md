# repo-to-bench

Turn repository revisions into reproducible coding-agent benchmark tasks, Harbor runs, cross-agent replays, blind review packets, and validated verdicts.

`repo-to-bench` is a runner-neutral evaluation toolkit. Harbor is the only supported real-agent execution backend in v0.1.0, but Harbor is not the product identity and does not own the task, replay, review, verdict, or completeness contracts.

## What the toolkit owns

| repo-to-bench | Harbor v0.22.0 |
|---|---|
| Revision-to-task construction | Agent lifecycle and adapters |
| Oracle-pass／base-fail validity gate | Docker environment lifecycle |
| Immutable plan digest and approval boundary | Trial scheduling and concurrency |
| Claude Code／Codex replay completeness | Retry and native resume |
| Anonymous review packet and reveal mapping | Native trajectory and result persistence |
| Verdict schema and validation | Verifier execution primitives |

The project is not a Harbor fork, official Harbor extension, replacement framework, or runner marketplace.

## Verified baseline

| Surface | Verified baseline |
|---|---|
| Core CLI | Python 3.9, 3.11, and 3.14 in GitHub Actions |
| Full workflow | Python 3.13.2 |
| Execution backend | Harbor 0.22.0 |
| Container runtime used for release verification | Docker 29.4.3 |
| Synthetic image | `python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a` |
| Harbor task schema | 1.4 |

Harbor 0.22.0 requires Python 3.12 or newer. Use Python 3.13 for the verified full-workflow path. Core-only commands remain installable on Python 3.9+.

## Install

Full Harbor workflow:

```bash
git clone https://github.com/GGGODLIN/repo-to-bench.git
cd repo-to-bench
python3.13 -m venv .venv
.venv/bin/python -m pip install ".[harbor]"
.venv/bin/repo-to-bench --help
```

Core CLI without the Harbor extra:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/repo-to-bench --help
```

Docker must be running for task verification, execution, and process-isolation probes.

## Skill-first quickstart

Open Claude Code from the repository root. The project skill is discovered from [`.claude/skills/repo-to-bench/SKILL.md`](/.claude/skills/repo-to-bench/SKILL.md).

Ask:

```text
Run the free repo-to-bench synthetic smoke. Do not run paid models.
```

The skill delegates the workflow to the CLI. The deterministic smoke can also run directly:

```bash
.venv/bin/python .claude/skills/repo-to-bench/scripts/synthetic_smoke.py
```

Release verification observed:

```json
{"execution":"complete","output_root":"<temporary-directory>/repo-to-bench-smoke-…","replays":["COMPLETE","COMPLETE"],"review":"ready","status":"passed","task":"accepted","total_trials":2,"verdict":"valid"}
```

The smoke uses only Harbor `oracle` and `nop`. It does not call a paid model.

## Direct CLI workflow

The CLI is the stable contract. Use each subcommand's `--help` for its current flags.

```text
build-task → plan → execute → replay → review-build → validate-verdict
```

Build a task from explicit revisions:

```bash
repo-to-bench build-task \
  --repo <repo-path> \
  --base-revision <base> \
  --head-revision <head> \
  --test-command <command> \
  --output-dir <task-root>
```

Create an execution plan:

```bash
repo-to-bench plan \
  --task-root <task-root> \
  --agent <agent> \
  --model <model> \
  --attempts 1 \
  --concurrency 1 \
  --output-dir <run-root>
```

`plan` resolves the task revisions, matrix, attempts, concurrency, total trial count, Harbor configuration, and immutable digest. For any matrix that can call paid models, review those fields before execution.

Execute only the approved plan:

```bash
repo-to-bench execute \
  --plan <run-root>/plan.json \
  --approve-digest <exact-plan-digest>
```

A changed task or matrix produces a different digest and Harbor job. An old approval cannot authorize it. Same-plan resume delegates to `harbor job resume`; repo-to-bench does not implement its own scheduler, retry queue, concurrency engine, or resume state.

## Completeness semantics

| Stage | Complete state | Default behavior when incomplete |
|---|---|---|
| Execution | Every task／agent／model／attempt cell has a result | Returns `incomplete`, lists every missing cell, and exits nonzero |
| Execution with `--allow-partial` | Missing cells remain explicit | Returns `partial`; `normal_review_eligible=false` |
| Replay | Every supported mutation reconstructed | Unsupported mutations return `INCOMPLETE`; invalid input returns `FAILED` |
| Review | Complete matrix and `COMPLETE` replay candidates | Rejects incomplete input unless partial review is explicit |
| Verdict | Exact packet IDs, enums, ranking, and derived flags | Returns a versioned error taxonomy and exits nonzero |

A diagnostic replay may export an `INCOMPLETE` workspace, but it cannot enter the normal review path.

## Replay formats

v0.1.0 accepts one JSONL trajectory file per replay:

- Claude Code: successful `Write` and single-match `Edit` tool records. Shell mutations are identified but never executed.
- Codex: successful `patch_apply_end` records containing explicit `add`, `update`, or `delete` changes.

v0.1.0 does not implement shell-command replay, trajectory auto-detection, directory trajectory discovery, executable-mode preservation, or hardlink-specific replay defenses.

## Security boundary

Task construction keeps hidden tests, the oracle patch, and controller metadata outside the agent workspace. Blind review exports only anonymous candidate diffs; the reveal mapping stays controller-only. Docker probes verify that agent and judge processes receive only their declared mounts.

Run roots are tool-owned and marker-gated. Credential-shaped content fails closed, and findings expose only category, safe path, line, and masked fingerprint. See [SECURITY.md](/SECURITY.md) for the trust boundary, operator responsibilities, and known limitations.

Do not commit private repositories, generated task material, hidden tests, real trajectories, provider credentials, or controller reveal mappings into this public repository.

## Replacing Harbor

v0.1.0 ships no alternative backend and no generic runner plugin interface. A replacement must supply all of these responsibilities:

1. Task and agent configuration
2. Isolated environment lifecycle
3. Verifier injection
4. Native trajectory capture
5. Reward and result persistence
6. Concurrency
7. Retry
8. Resume

It must also emit artifacts that can be normalized into the existing execution, replay, review, and verdict contracts. Those toolkit-owned contracts do not import Harbor internals.

## Known limitations

- Pathname operations retain TOCTOU exposure on hostile local filesystems.
- Source hardlinks, JSON-escaped credentials, and some Unicode token-boundary cases are outside the current credential scanner.
- Release scanning covers the tracked tree and reachable Git blobs, not arbitrary Git metadata.
- Recursive cleanup may be partial after an operating-system failure.
- Provider model availability, credentials, pricing, and budget enforcement remain operator responsibilities. The plan digest prevents unapproved configuration changes; it does not estimate provider cost.
- There is no hosted service, database, judge model invocation, automatic verdict generation, non-Harbor backend, or generic runner adapter.

## License and provenance

Original toolkit code is licensed under the [MIT License](/LICENSE). Harbor is an external dependency licensed under Apache-2.0; no Harbor package source is vendored into this repository.

[`provenance.json`](/provenance.json) covers every tracked public file. The only migrated-modified source is the generic replay implementation, bound to its approved private source commit and SHA-256. All synthetic fixtures are newly authored and contain no private task, company, transcript, or credential material.

Release verification for public head `bffe2a67226e9edf2547abdebde68c1341789228` passed a fresh clone with isolated `HOME`, 129 tests, a clean 39-file／70-history-object scan, the full no-cost skill smoke, Docker isolation probes, and [GitHub Actions](https://github.com/GGGODLIN/repo-to-bench/actions/runs/33144285266).
