# repo-to-bench

[English](README.md) | **繁體中文**

**把 Git history 裡的一次真實修正，轉成可以重複執行的 coding-agent benchmark。**

https://github.com/user-attachments/assets/712d93c7-4a24-46c8-9324-0c63515a05e9

影片裡的排名、agent 和他們的答案都是虛構示範；那一行修正取自 repo 自帶的合成範例。

你剛修好一個真實 repo 裡的 bug。手上有修正前的 commit、修正後的 commit，以及能證明兩者差異的 tests。現在你想知道：coding agent 能不能在看不到原始 patch 的情況下解出同一個問題？如果同時測幾個 agents，結果是否完整，解答有沒有外洩，reviewer 又會不會因為知道作者身分而產生偏見？

`repo-to-bench` 會把這次修改包成 benchmark task。你提供 repo、base revision、已知正確的 revision 與 test command。任何 agent 結果被採用之前，它會先確認 base 會失敗，而且已知正確的解法會通過。

Agent 從仍有問題的 revision 開始。Hidden tests 與 oracle patch 留在 agent workspace 之外。執行後，toolkit 會確認每一個宣告的 run 都有結果，重建支援的 Claude Code 或 Codex 修改，並把候選解答匿名化後交給 reviewer。只有 candidate IDs、排名、outcomes 與衍生 flags 都符合原始 review packet，verdict 才會通過驗證。

你得到的不是通用排行榜，也不是跟自己 codebase 無關的玩具題目。它是一份來自你真正處理過、而且有已知答案的工程問題所建立的 benchmark。

## 從一次修正變成 benchmark

```text
repo + 有問題的 revision + 已知正確的 revision + test command
  → 驗證題目
  → 在隔離環境中執行 coding agents
  → 拒絕缺漏或不完整的結果
  → 重建候選修改
  → 建立匿名 review packet
  → 驗證 verdict
```

如果已知正確的解法沒有通過，或 base 沒有失敗，題目就會被拒絕。不完整的 execution 仍是不完整。無法完整重建的 replay 仍是 `INCOMPLETE`。這些狀態都不能靜默進入正常 review 流程。

這套工具適合用來比較 agents 處理你自己 repo 問題的表現、確認另一個 agent 能否重現已知修正，或從真實工程工作建立私人 benchmark。

## Harbor 在哪裡

`repo-to-bench` 在 task、replay、review、verdict 與完整性 contract 上不綁 runner。v0.1.0 唯一支援的真實 agent execution backend 是 Harbor 0.22.0。

| repo-to-bench | Harbor v0.22.0 |
|---|---|
| 從 repo revisions 建立 task | 執行 agent lifecycle 與 adapters |
| 驗證 known-good-pass／base-fail | 管理 Docker environments |
| 用已核准的 plan digest 綁定 execution | 安排 trials 與 concurrency |
| 檢查 matrix 與 replay 是否完整 | retry 與 resume 原生 jobs |
| 建立匿名 review packets | 保存 trajectories 與 results |
| 驗證 verdicts | 執行 verifier primitives |

Harbor 是執行引擎，不是產品名稱。這個專案不是 Harbor fork、官方 extension、replacement framework 或 runner marketplace。

## 已驗證基準

| 範圍 | 已驗證基準 |
|---|---|
| Core CLI | GitHub Actions 中的 Python 3.9、3.11、3.14 |
| 完整 workflow | Python 3.13.2 |
| Execution backend | Harbor 0.22.0 |
| Release verification 使用的 container runtime | Docker 29.4.3 |
| Synthetic image | `python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a` |
| Harbor task schema | 1.4 |

Harbor 0.22.0 需要 Python 3.12 以上。完整 workflow 請使用已驗證的 Python 3.13 路徑。只使用 core commands 時，仍可在 Python 3.9 以上安裝。

## 安裝

完整 Harbor workflow：

```bash
git clone https://github.com/GGGODLIN/repo-to-bench.git
cd repo-to-bench
python3.13 -m venv .venv
.venv/bin/python -m pip install ".[harbor]"
.venv/bin/repo-to-bench --help
```

不含 Harbor extra 的 Core CLI：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/repo-to-bench --help
```

Task verification、execution 與 process-isolation probes 都需要 Docker 正在執行。

## Skill-first 快速開始

從 repo 根目錄啟動 Claude Code。專案 skill 位於 [`.claude/skills/repo-to-bench/SKILL.md`](/.claude/skills/repo-to-bench/SKILL.md)。

輸入：

```text
Run the free repo-to-bench synthetic smoke. Do not run paid models.
```

Skill 會把 workflow 委派給 CLI。也可以直接執行同一套 deterministic smoke：

```bash
.venv/bin/python .claude/skills/repo-to-bench/scripts/synthetic_smoke.py
```

Release verification 實際觀察到：

```json
{"execution":"complete","output_root":"<temporary-directory>/repo-to-bench-smoke-…","replays":["COMPLETE","COMPLETE"],"review":"ready","status":"passed","task":"accepted","total_trials":2,"verdict":"valid"}
```

這個 smoke 只使用 Harbor `oracle` 與 `nop`，不會呼叫付費模型。

## 直接使用 CLI

CLI 是穩定 contract。每個 subcommand 的現行 flags 以自己的 `--help` 為準。

```text
build-task → plan → execute → replay → review-build → validate-verdict
```

從明確的 revisions 建立 task：

```bash
repo-to-bench build-task \
  --repo <repo-path> \
  --base-revision <base> \
  --head-revision <head> \
  --test-command <command> \
  --output-dir <task-root>
```

建立 execution plan：

```bash
repo-to-bench plan \
  --task-root <task-root> \
  --agent <agent> \
  --model <model> \
  --attempts 1 \
  --concurrency 1 \
  --output-dir <run-root>
```

`plan` 會解析 task revisions、matrix、attempts、concurrency、trial 總數、Harbor configuration 與不可變的 digest。如果 matrix 可能呼叫付費模型，請在 execution 前先檢查這些欄位。

只執行你核准的 plan：

```bash
repo-to-bench execute \
  --plan <run-root>/plan.json \
  --approve-digest <exact-plan-digest>
```

Task 或 matrix 變更後會產生不同 digest 與 Harbor job。舊 approval 不能核准新的內容。相同 plan 的 resume 會委派給 `harbor job resume`；`repo-to-bench` 不會另外實作 scheduler、retry queue、concurrency engine 或 resume state。

## 完整性語意

| Stage | 完整狀態 | 不完整時的預設行為 |
|---|---|---|
| Execution | 每個 task／agent／model／attempt cell 都有結果 | 回傳 `incomplete`、列出所有 missing cells，並以 nonzero 結束 |
| 使用 `--allow-partial` 的 execution | Missing cells 仍會明確列出 | 回傳 `partial`，並設定 `normal_review_eligible=false` |
| Replay | 所有支援的 mutation 都已重建 | 不支援的 mutation 回傳 `INCOMPLETE`；無效輸入回傳 `FAILED` |
| Review | Matrix 完整，而且所有 replay candidates 都是 `COMPLETE` | 除非明確允許 partial review，否則拒絕不完整輸入 |
| Verdict | Packet IDs、enums、ranking 與 derived flags 完全符合 | 回傳有版本的 error taxonomy，並以 nonzero 結束 |

Diagnostic replay 可以匯出 `INCOMPLETE` workspace，但不能進入正常 review 路徑。

## Replay formats

v0.1.0 每次 replay 接受一個 JSONL trajectory file：

- Claude Code：成功的 `Write` 與 single-match `Edit` tool records。Shell mutations 只會被辨識，不會執行。
- Codex：成功的 `patch_apply_end` records，內容必須明確包含 `add`、`update` 或 `delete` changes。

v0.1.0 不實作 shell-command replay、trajectory auto-detection、directory trajectory discovery、executable-mode preservation 或 hardlink-specific replay defenses。

## 安全邊界

Task construction 會把 hidden tests、oracle patch 與 controller metadata 留在 agent workspace 外。Blind review 只匯出匿名 candidate diffs，reveal mapping 留在 controller。Docker probes 會確認 agent 與 judge processes 只收到各自宣告的 mounts。

Run roots 必須由 toolkit 建立，並以 marker 限制 cleanup。偵測到 credential-shaped content 時會 fail closed；finding 只會暴露 category、safe path、line 與 masked fingerprint。完整的信任邊界、operator 責任與已知限制請看 [SECURITY.md](/SECURITY.md)。

不要把 private repositories、generated task material、hidden tests、真實 trajectories、provider credentials 或 controller reveal mappings commit 到這個 public repo。

## 如果要取代 Harbor

v0.1.0 沒有 alternative backend，也沒有 generic runner plugin interface。替代品必須提供：

1. Task 與 agent configuration
2. 隔離 environment lifecycle
3. Verifier injection
4. 原生 trajectory capture
5. Reward 與 result persistence
6. Concurrency
7. Retry
8. Resume

它也必須輸出能夠正規化成現有 execution、replay、review 與 verdict contracts 的 artifacts。這些由 toolkit 定義的 contracts 不會 import Harbor internals。

## 已知限制

- Pathname operations 在 hostile local filesystem 上仍有 TOCTOU exposure。
- Source hardlinks、JSON-escaped credentials 與部分 Unicode token-boundary cases 不在目前 credential scanner 的範圍內。
- Release scanning 涵蓋 tracked tree 與 reachable Git blobs，不涵蓋任意 Git metadata。
- Operating-system failure 後，recursive cleanup 可能只完成一部分。
- Provider model availability、credentials、pricing 與 budget enforcement 仍由 operator 負責。Plan digest 可以阻止未核准的 configuration 變更，但不會估算 provider cost。
- 沒有 hosted service、database、judge model invocation、automatic verdict generation、non-Harbor backend 或 generic runner adapter。

## License 與 provenance

原創 toolkit code 使用 [MIT License](/LICENSE)。Harbor 是外部 Apache-2.0 dependency；這個 repo 沒有 vendored Harbor package source。

[`provenance.json`](/provenance.json) 涵蓋每一個 tracked public file。唯一的 migrated-modified source 是 generic replay implementation，並綁定已核准 private source commit 與 SHA-256。所有 synthetic fixtures 都是新寫的，不含 private task、公司資料、transcript 或 credential material。

最終 [`v0.1.0` GitHub Actions run](https://github.com/GGGODLIN/repo-to-bench/actions/runs/33145413683) 通過 Python 3.9／3.11／3.14 core matrix、129 個 no-cost tests、8 個 Harbor end-to-end tests，以及 41 files／74 history objects 的乾淨 release scan。緊接在 release commit 之前、code 完全相同的 head 也通過隔離 `HOME` 的 fresh clone、完整 no-cost skill smoke 與 Docker isolation probes。
