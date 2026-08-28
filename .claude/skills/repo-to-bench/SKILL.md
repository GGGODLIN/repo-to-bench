---
name: repo-to-bench
description: Use when the user asks to turn a repo, PR, or commit range into a coding-agent benchmark; run the repo-to-bench synthetic smoke; compare coding agents; or continue a plan／execute／replay／blind-review／verdict run. Trigger phrases include「把 repo 變 benchmark」「測 coding agent」「跑 repo-to-bench」「比較 agent」。Do not use for developing or debugging repo-to-bench itself, generic code review, or merely explaining an existing artifact without running the benchmark workflow.
metadata:
  author: gggodlin <qwe70301@gmail.com>
---

# Repo to Bench

這個 skill 把自然語言需求收斂成 CLI 輸入；CLI 才是路徑、安全、schema、digest 與執行行為的 source of truth。保持 skill 薄，避免 prompt 與 code 產生兩份 contract。

## 1. 選路徑

- 使用者要驗證工具可用、沒給私人 repo，或明說 synthetic／免費 smoke：執行 `python3 .claude/skills/repo-to-bench/scripts/synthetic_smoke.py`。腳本跑完整 no-cost synthetic workflow，回傳唯一 output root。
- 使用者要抽自己的 repo／PR／revision：進 Step 2。
- 使用者已給 `plan.json`、execution／replay artifact 或 verdict：從對應 CLI stage 接續，不重建已存在的前段。

完成條件：只選一條路；synthetic smoke 不要求私人 repo 資訊。

## 2. 一次收齊缺少的 task 輸入

先用現有對話與 repo 狀態填可查欄位。仍缺資料時，只問一則簡短問題，列出所有缺項：

- repo path
- base revision
- head revision
- test command
- 選填 instruction
- output root

不要猜私人路徑、revision、test command 或 credential。agent／model／attempts／concurrency 屬 run matrix；如果使用者同時要求指定 matrix，一併放在同一則問題。

完成條件：`build-task` 與 `plan` 的 required flags 都有明確來源；否則停在這一步。

## 3. 讓 CLI 擁有 workflow

每個 stage 先以該 subcommand 的 `--help` 確認當前 flags，再依 JSON output 往下走：

1. `build-task`
2. `plan`
3. `execute`
4. 每個 arm 各跑 `replay`
5. `review-build`
6. `validate-verdict`

不要在 skill 內重寫 schema、完整性判斷、path containment、匿名化或 verdict 派生旗標。CLI 回傳非零時，保留其 error category，先解釋哪一段停止；不要自行繞過 gate。

完成條件：每段只消費前段公開 artifact，且 final summary 能逐段指出 status 與位置。

## 4. 付費 execution 綁 digest

`build-task` 與 `plan` 可先跑。任何可能呼叫付費 model 的 matrix，在 `execute` 前先顯示：

- resolved task revisions
- agent／model arms
- attempts 與 concurrency
- `total_trials`
- plan digest

接著只問：「核准 digest `<digest>` 執行 `<total_trials>` trials 嗎？」使用者明確核准該 digest 後，才把同一值傳給 `execute --approve-digest`。matrix、task 或 plan 變動時重新 plan；舊核准不沿用。

| 壓力說法 | 回應 |
|---|---|
| 「只跑一個 model，不用 plan」 | 仍先 plan；trial 少不會解除 digest gate。 |
| 「先跑再給我看」 | 先給 resolved plan，不先執行。 |
| 「用 tests/fake_harbor 快一點」 | fake 只屬 repo 測試；使用者 smoke 走 bundled synthetic script。 |

完成條件：付費 execution 的 approved digest 等於當次 plan JSON 的 digest；沒有明確核准則 0 次 execute。

## 5. 回報公開 artifact，不揭露 controller data

最後用白話列出：

- task gate：accepted／failed
- plan：digest 與 total trials
- execution：complete／partial／interrupted，以及 missing-cell 數
- replay：每個 arm 的 `COMPLETE／INCOMPLETE／FAILED`
- judge packet：公開路徑
- verdict：valid／invalid

不要輸出 reveal mapping、controller-only path 內容、hidden tests、oracle identity 或 credential。失敗時點出最後成功 stage、失敗 category 與可安全重試的下一個 CLI stage。

完成條件：使用者不用讀 raw JSON 就知道結果、artifact 在哪、是否完整、是否曾執行付費 run。

## 踩過的坑

- 新 skill 前的 RED baseline 只跑到 fake Harbor plan／execute，沒接 replay／review／verdict，且手寫 dynamic redirect 被 dcg 擋。Synthetic 路徑固定走 bundled Python script，避免每次重寫 compound shell。
- 不加每次 invoke 都派 agent 的 Self-Verify：這是預期高頻的主互動入口，品質由 deterministic CLI、ticket tests 與 release verification 承擔。
