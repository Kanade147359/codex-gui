# Codex CLI の機能調査（Phase A）

調査日: 2026-10-01 / **codex-cli 0.159.2** / ChatGPT ログイン（plan: `prolite`）/ Linux (WSL2)

推測ではなく、`--help`、`codex app-server generate-json-schema`、実際の app-server への JSON-RPC 呼び出し（実測）で確認した内容だけを書いています。
「実測」は実際の `gpt-6.1-sol` で動かして確認したもの、「スキーマ」は生成した JSON Schema にあることだけを確認したものです。

## 結論

| 項目 | 結果 |
| --- | --- |
| app-server | **利用可**。`codex app-server`（stdio / unix / ws。GUI は stdio）。thread / turn を持つ JSON-RPC。 |
| session の再開 | app-server: `thread/resume` → `turn/start`（実測: **別プロセスからでも**同じ thread を再開できる）。exec: `codex exec resume <thread_id> -`。 |
| 実行中の追加指示 | app-server の **`turn/steer`** が使える（実測: 受理される）。`codex queue` は exec 相手には**使えない**（下記）。 |
| token usage | app-server: `thread/tokenUsage/updated`。exec: `turn.completed.usage`。どちらも thread の**累積値**。 |
| cache usage | `cachedInputTokens`（exec は `cached_input_tokens`）。`cacheWriteInputTokens`、`reasoningOutputTokens` も来る。 |
| rate limit | `account/rateLimits/read` と `account/rateLimits/updated`（push）。 |
| context size / window | `thread/tokenUsage/updated` の `tokenUsage.last.totalTokens`（現在の context）と `modelContextWindow`。 |
| compact | **`thread/compact/start`**（実測: 動く。`contextCompaction` アイテムを持つ 1 ターンとして走る）。 |
| モデル | `model/list`（app-server）/ `codex debug models`（CLI）。`gpt-6.1-sol` を認識、`isDefault: true`。 |

## GPT-6.1 Sol の認識

`codex debug models` と `model/list` の両方に出ます。主なメタデータ（`codex debug models`）:

- `slug: gpt-6.1-sol`、`default_reasoning_level: low`、`priority: 1`、`visibility: list`
- 対応 effort: `low / medium / high / xhigh / max / ultra`（`ultra` の説明は "Maximum reasoning with automatic task delegation"）
- `service_tiers: [{id: "priority", name: "Fast", description: "2x speed, increased usage"}]`、`defaultServiceTier: null`（= Standard）
- `support_verbosity: true`、`default_verbosity: low`
- `context_window: 272000`（app-server が実際に報告する `modelContextWindow` は **258400** = 95%）

注意: このマシンの `~/.codex/config.toml` は `model = "gpt-6-sol"`、`model_reasoning_effort = "high"` です。
そのため「Codex default」を選ぶと **high** になります。GUI は reasoning を既定で `Low` と明示的に指定します（`Auto` を選ぶと Codex 側の既定）。
GUI は `gpt-6.1-sol` を **この codex が一覧に出しているときだけ** 推奨モデルにします（`CODEX_GUI_PREFERRED_MODEL` で変更可）。

## app-server の使い方（GUI が実際に使っているもの）

```text
initialize {clientInfo}  →  initialized (notification)
account/read             →  {account: {type: "chatgpt" | "apiKey"}}   サブスクリプション認証の確認
account/rateLimits/read  →  rate limit（下記）
thread/start  {model, cwd, serviceTier, approvalPolicy, approvalsReviewer, sandbox, config, developerInstructions}
thread/resume {threadId, excludeTurns, <thread/start と同じ設定>}
turn/start    {threadId, input:[{type:"text",text}], effort}   →  {turn:{id}}
turn/steer    {threadId, expectedTurnId, input}                →  {turnId}
turn/interrupt{threadId, turnId}                               →  turn/completed(status: "interrupted")
thread/compact/start {threadId}                                →  {}  （その後 turn/started … turn/completed）
```

通知: `turn/started`、`item/completed`、`thread/tokenUsage/updated`、`turn/completed`、`error`、`account/rateLimits/updated` など。

- **thread の設定**: `config` に `model_verbosity`、`web_search`（`disabled` / `live`）、`model_reasoning_effort`、
  `sandbox_workspace_write.writable_roots`、`features.*` を渡せます。`serviceTier: "default"` が Standard、`"priority"` が Fast。
- **auto approval**: `codex exec --approve-for-me` は `approvalPolicy: "on-request"` + `approvalsReviewer: "auto_review"` + `sandbox: "workspace-write"` に相当します。
  GUI はこの 3 つを渡します（`--dangerously-bypass-approvals-and-sandbox` / `danger-full-access` は一切使いません）。
- **common instruction**: `thread/start` の `developerInstructions` に一度だけ渡します（resume では渡し直しません）。
- 承認などの server request（`item/commandExecution/requestApproval` など）が来た場合は decline します。GUI に承認ダイアログは無いためです。
  auto approval OFF のときは `approvalPolicy: "never"`（sandbox 内で完結、昇格が要る操作は失敗）にして、人の承認待ちで止まらないようにしています。

## token usage

実測（gpt-6.1-sol、3 ターン、同一 thread）:

```text
thread/tokenUsage/updated.tokenUsage = {
  total: {inputTokens, cachedInputTokens, cacheWriteInputTokens, outputTokens, reasoningOutputTokens, totalTokens},  // thread の累積
  last:  {…同じ形…},                                                                                              // 直近 1 回のモデル呼び出し
  modelContextWindow: 258400
}
```

- 1 ターンの中で複数回（モデル呼び出しごとに）更新されます。**ターンの使用量 = そのターンの最後の `total` − 前ターンの `total`**。GUI はこの差分を保存します。
- 現在の context サイズ = `last.totalTokens`（直近リクエストのサイズ）。
- compaction 中の更新は `total` が変わらないことがありました。compact 直後の context サイズは次のターンで分かります（それまで「unknown」と表示）。
- `codex exec --json` の `turn.completed.usage` も thread の累積値（resume しても増え続ける）。

## rate limit

`account/rateLimits/read`（実測）:

```json
{"ordinaryUsageAllowed": true,
 "rateLimits": {"limitId": "codex", "planType": "prolite",
                "primary": {"usedPercent": 7, "windowDurationMins": 10080, "resetsAt": 1791453983},
                "secondary": null, "rateLimitReachedType": null, "credits": {…}},
 "rateLimitResetCredits": {"availableCount": 0, "credits": []}}
```

- **このアカウントのプランには 5 時間枠が無く、週次（10080 分）の `primary` だけ**です。
  `primary = 5 hour / secondary = weekly` と決め打ちすると間違えるため、GUI は `windowDurationMins` で分類します（≤360 分 = "5 hour"、≥6 日 = "Weekly"、それ以外は時間/日数で表示）。
  5 時間枠があるプランでは `primary: 300 分 / secondary: 10080 分` の 2 本として表示されます。
- `usedPercent` は整数。粗い値です。
- 枯渇の判定材料（Codex が言うことだけを使う）: `ordinaryUsageAllowed == false`、`rateLimitReachedType != null`、
  turn の `error.codexErrorInfo` が `usageLimitExceeded` / `rateLimitExceeded`。
- `rateLimitResetCredits.availableCount` は**表示のみ**。`account/rateLimitResetCredit/consume`（reset の消費）は GUI のどこからも呼びません。

## `codex queue` と `turn/steer`

- `codex queue --thread <id> --message <text>` は存在します。ただし README に記録したとおり、実行中の **`codex exec`** に送ると
  メッセージは queue から取り出されて thread の履歴には入るものの、exec が新しい turn を `turn_aborted` にして終了するため**回答されずに失われます**（0.159.2 で確認）。
- app-server の `turn/steer`（`expectedTurnId` 必須）は、実測で受理されました（`{turnId}` が返る）。GUI の「実行中 Task への追加指示」はこれを使います。
  **GUI の実装経由で実機確認済み**: `sleep 12` を走らせている最中に「返信に STEERED-OK を含めて」と送ると、最終返信は `done STEERED-OK` になりました（steer は同じ turn の中で実モデルに反映される）。
- exec バックエンドでは実行中の追加指示は不可（409）のままです。

## 認証

`account/read` が `{"type": "chatgpt", "planType": ...}` を返します。API キー認証だと `"apiKey"`。
GUI は（既定で）`chatgpt` 以外のときターンを開始せず、`failed` にして理由を表示します。API キーに自動で切り替えることはありません。
さらに codex の子プロセスの環境から `OPENAI_API_KEY` / `CODEX_API_KEY` などを取り除きます。

## 未確認・制約

- `Profile`（config の profile 選択）は GUI から設定できません。
- app-server の schema は experimental です。`thread/start` の `serviceName` などは今後変わる可能性があります。
- compaction は小さい thread（context 約 13k）では token をほぼ使わず、context も縮みませんでした（`total` が変わらない）。大きい thread での縮み方は未確認です。
- prompt cache は best-effort です。実測では 1・2 ターン目が約 76%、3 ターン目が約 99% でした（変動します）。

## 中断した turn の復旧（実測: 0.159.2）

自動復旧（README「自動復旧（リトライ）」）の設計は、次の実測に基づいています。

- `codex exec resume <thread_id>` は **プロンプトが必須**（stdin が空だと `No prompt provided via stdin.` で終了）。「中断した turn をそのまま続ける」ネイティブ機能は **ない**。
- turn の途中で `kill -9` した後の rollout（`~/.codex/sessions/…/rollout-…-<thread_id>.jsonl`）は、`task_started` と tool 呼び出しで終わり `task_complete` が無い。
  子プロセス（`sleep` を実行していた shell）は親の死亡で消え、**コマンドの結果は残らない**。
- 同じ thread に固定の recovery instruction（`codex exec … resume <id> -` / app-server の `thread/resume` + `turn/start`）を送ると、**同じ thread_id** のまま、モデルは中断された作業を把握し、
  `git status` 相当の確認をしてから **残りだけ** を実行した（実測: 再開 turn の cached input は約 90〜98%）。
- 反例（実測で発見）: app-server を `turn/start` の応答直後（`turn/started` の前）に落とすと、**再開した thread に元の指示が入っていなかった**
  （モデルは "The interrupted task isn't visible in this conversation" と答えて何もせず完了した）。そのため GUI は「Codex が turn の開始を確認したか」を記録し、
  確認前に落ちた turn は recovery instruction ではなく **元の指示を同じ thread に再送**します（まだ何も実行されていない）。
- app-server の `codexErrorInfo` は `httpConnectionFailed` / `responseStreamConnectionFailed` / `responseStreamDisconnected` / `responseTooManyFailedAttempts` / `serverOverloaded` /
  `internalServerError`（一時的）、`usageLimitExceeded` / `rateLimitExceeded`（利用枠）、`unauthorized` / `badRequest` / `contextWindowExceeded` / `sandboxError`（リトライしても直らない）などに分かれる。

## 再現方法

```bash
codex --version
codex app-server --help
codex app-server generate-json-schema --out /tmp/schema     # ClientRequest.json / ServerNotification.json など
codex exec resume --help
codex queue --help
codex debug models | python3 -m json.tool | less
CODEX_GUI_REAL=1 .venv/bin/python -m pytest tests/test_real_codex.py -s   # 実機での 3 ターン検証
```
