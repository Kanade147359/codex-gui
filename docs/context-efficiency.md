# Context Efficiency（実測メモ）

調査日: 2026-10-01 / **codex-cli 0.159.2** / ChatGPT ログイン（plan: `prolite`）/ Linux (WSL2) / モデル `gpt-6.1-sol`

GPT-6.1 Sol / Codex サブスクリプションを多数並列で使うときに、品質を落とさずに無駄な token / context 消費を減らすための機能と、
**それが実際に効くかどうかを実 Codex で測った結果**です。推測は書いていません。「測った」は実際の `codex`（モデルなし）または実モデルで確認したものです。

> **AGENTS.md は自動で編集・移動・削除・書き換えしません。** 問題を検出して警告するだけで、直すのは Edit ボタンから人が行います
> （`app/agents_audit.py` はファイルを読み取り専用で開くだけで、書き込み経路を持ちません。`tests/test_agents_audit.py` が全ファイルの内容・mtime・mode が不変であることを確認します）。

## 測定方法（モデルを呼ばない）

実 `codex` を、手元の偽 Responses API（`app/fake_responses.py`）に向けて動かし、**モデルに実際に送られるリクエスト**を記録します。

- `codex exec --ephemeral -c model_provider=…`: セッション履歴を残さず、設定ファイルも書かず、認証にも触れません。
- tool catalog は、リクエストの `additional_tools` 入力項目と、code-mode の `exec` ツールの `ALL_TOOLS`（モデルが到達できる nested tool 全部。declaration を省略された遅延 MCP tool も含む）から読みます。
- `codex debug prompt-input` は、モデルに見える静的 prefix（skills catalog、AGENTS.md chain）を出します。
- app-server の `config/read` で実効 config（`project_doc_max_bytes` など）、`turn/start` で per-turn の設定を確認しました。

どれも課金されません。`~/.codex/config.toml` は変更されません（`tests/test_ctx_integration.py::test_probes_and_audits_leave_the_users_codex_home_alone`）。

## 実測の結論（先に要点）

| 項目 | 実測結果 | GUI での扱い |
| --- | --- | --- |
| `tool_output_token_limit` | **下げる方向にしか効かない**。モデル自身の上限（`truncation_policy` = 10,000 tokens）が先に掛かるため、16,000 / 32,000 は **Codex default と完全に同じ**。8,000 は 1 回の巨大出力を約 4% しか減らさない（4,000 で約 50%、2,000 で約 75%）。モデルが `max_output_tokens=60000` を要求しても上限は超えない | preset（GUI 独自）を用意するが、**効果なしの preset はそう表示**。既定は Codex default |
| nested agents（sub-agent） | `features.multi_agent=false` は **gpt-6.1-sol では効かない**（`multi_agent_version: v2` がモデル側で決まっており、`spawn_agent` が成功する）。`agents.enabled` というキーは 0.159.2 に無い。**`features.multi_agent_v2.max_concurrent_threads_per_session=1` は効く**（`spawn_agent` が `agent thread limit reached` で失敗する）。tool 宣言（約 5.5KB）は catalog に残る | 新規 Task は既定 OFF（上記 3 キーを override）。Task ごとに ON 可 |
| MCP の無効化 | `mcp_servers.<name>.enabled=false` / `enabled_tools` / `disabled_tools` は、モデルが到達できる tool 集合に **実際に反映される**。**定義されていないサーバー名を `enabled=false` にすると Codex が config エラーで起動しない** | 無効化するサーバー名は必ず実効 config（`config/read`）から取る |
| ChatGPT apps / コネクタ | この環境では nested tool 187 個のうち **175 個が `mcp__codex_apps__*`**（GitHub / sites / chatgpt_space / pets …）。`features.apps=false` で **187 → 8**、catalog の宣言が約 4.6KB（約 1,150 tokens）減る。`features.plugins=false` だけだと 1 tool しか減らない | Tool profile の Development / Minimal で外す |
| skills catalog | `skills.max_context_tokens` は **catalog（名前 + description）の予算**。catalog は「予算 + 約 100 tokens」に収まるよう description が切り詰められ、予算が小さすぎると skill 自体が落ちる。**Codex の既定予算は約 5.4k tokens で、8,000 の preset はそれより大きい**（Large は catalog を増やす） | preset を用意。効かない場合・増える場合は画面に明記 |
| AGENTS.md の読み込み | global（`$CODEX_HOME/AGENTS.md`）は budget の **外**。project は root（`.git` など `project_root_markers`）から cwd までを 1 つの cumulative budget（`project_doc_max_bytes`、既定 32,768）で読み、超えたファイルはその位置で切られ、**以降（より深い）ファイルは丸ごと落ちる**。マーカーが無ければ cwd のみ | audit が同じ規則で予測し、実 Codex の prompt と一致することを検証済み |
| per-turn の speed | `thread/resume` の `serviceTier` は **無視される**。`turn/start.serviceTierForTurn` は **そのターンだけ**（thread の既定は変わらない）、`turn/start.serviceTier` は thread の既定を恒久的に変える | Send Standard / Send Fast は毎ターン `serviceTierForTurn` を明示 |
| cache write | 実 Codex は `cacheWriteInputTokens` を報告し、GUI は読み取れる（E2E で確認） | read / write / uncached を分けて保存・表示 |
| 1 request ごとの usage | `thread/tokenUsage/updated` の `last` は 1 回のモデル呼び出し。`total` は thread の累積 | `last` から request 単位で記録（long-context 判定はこちらだけを使う） |
| model window | `gpt-6.1-sol` の `context_window` は 272,000、Codex の実効 window は 258,400（95%）。`max_context_window` は 872,000 | 272K の long-context 料金帯は、`model_context_window` を上げない限り**到達しない**（GUI は絶対に上げない） |

## 機能

### 1. AGENTS.md Health Check

- 対象: global + repository root から**実際の cwd まで**の chain（Task の cwd が custom subdirectory ならその chain）。
- 表示: 各ファイルの path / bytes / token 概算（bytes÷4）/ cumulative bytes / 状態（全文・途中で切断・丸ごと除外）/ budget 使用率。
  budget は `config/read` の実効 `project_doc_max_bytes`（読めなければ既定 32,768 と明記）。
- 75% 以上で WARNING、100% 以上で CRITICAL（どのファイルが切られ、どれが落ちるかを表示）。
- heuristic warning: "always read" / "before every task" / "read all" / "read first" / 日本語の同等表現 / 参照 doc が 8 本以上 / root と nested の重複 / `~/.codex/AGENTS.md` と project の重複。
- Edit ボタンは GUI の AGENTS.md エディタ（`AGENTS.md` のみ。`AGENTS.override.md` や global は「GUI の外で編集」と表示）。

### 2. Tool Output Guard

- Task 設定: Codex default / Conservative 8,000 / Balanced 16,000 / Large 32,000（**GUI 独自の preset**）。上の実測のとおり 16,000 / 32,000 は
  この Codex では効果がないため、Task 作成画面と詳細に `no effect: this model already caps one tool output at 10,000 tokens` と出します。
  **新規 Task の既定は Codex default**（Balanced は何も減らさず「最適化済み」に見せるだけになるため）。
- 各 tool item の出力を推定 token（UTF-8 bytes ÷ 4）で記録: raw（ツールが出した量）とモデルが受け取った量（cap 適用後）。
  既定で 8k を超えるものを Task Detail に一覧し、16k 以上は large と表示。閾値は設定可能。
- 大きな出力の直後に request の input が 8k 以上増えたら `context_jump` を警告。

### 3. Long Context Guard

- 判定に使うのは app-server が報告する **最新 request の context サイズ**（`tokenUsage.last`）だけ。thread の累積 usage は使いません。
  サイズが分からなければ `unknown`（推測しない）。
- GPT-6.1 Sol: `< 220K normal` / `220–250K warning` / `250–272K strong warning` / `≥ 272K LONG CONTEXT（GPT-6.1 Sol long-context pricing zone）`。
  他のモデルは pricing table に threshold がある場合のみ（同じ比率）。無ければ判定しません。
- 警告時は **[Continue] [Compact] [Start New Session in Same Worktree]** を提示。自動 compact はしません。`model_context_window` /
  `model_auto_compact_token_limit` は一切設定しません（テストで確認）。

### 4. Nested agents / 5. Tool profile / 6. Skills budget

- 新規 Task は nested agents **OFF**（Task ごとに "Allow subagents"）。
- Tool profile: **Full**（Codex のまま）/ **Development**（ChatGPT apps と plugins を外し、ユーザーの MCP は残す）/ **Minimal**（組み込み tool のみ。MCP も全部無効）。
- 設定は Task 作成時に決まり、**同じ thread では凍結**されます（毎回同じ config を `thread/start|resume` に渡す）。変更は
  `POST /api/tasks/{id}/tool-profile` で `confirm: true` が必要で、`Changing tool configuration may reduce prompt cache reuse` と警告し、イベントに記録します。
- **「最適化済み」と表示するのは、実測で tool 数が減ったときだけ**です（`Verify profile` / Task 作成後のバックグラウンド検証）。減らなければ
  `No reduction was measured, so this profile is NOT marked optimized.`。測定に失敗したら「not verified」。
- Skills catalog budget: Codex default / Economy 2,000 / Balanced 4,000 / Large 8,000。catalog の skill 数と推定 token、予算が効くかどうか
  （効かない / かえって増える場合はそう表示）を New Task の preview に出します。skill 本体の予算ではないことも明記。
- グローバルの `~/.codex/config.toml` は編集しません。すべて per-thread の `config` override です。

### 7–9. Cache Health / Cache Age / Compaction Monitor

- ターンごとに input / cache read / cache write / uncached / output / hit rate / 要求した speed / request 数 / tool call 数を保存・表示。
- 大きな cache miss を event 化: `uncached > 10,000` **または** 直近平均より 30 ポイント以上低下（閾値は Efficiency settings で変更可）。
  考えられる原因（断定しない）: model / service tier / reasoning effort / verbosity / tool profile の変更、compaction、30 分以上の idle、新しい session。
  GUI が知っている変化がなければ「Codex 内部で prompt prefix が変わったか cache が失効した可能性」とだけ書きます。
- Cache age: HOT（10 分未満）/ WARM（30 分未満）/ COLD。参考表示で、**cache を温めるための prompt は絶対に送りません**（テスト + 静的チェック）。
- Compaction: 回数（手動 + Codex の自動）を記録し `Context 78% / Compactions 3` を表示。60 分以内に 2 回以上なら
  `Frequent compaction can reduce cache reuse and cause files to be re-read`。自動で compact はしません。

### 10. CWD

新規 Task の cwd は worktree の repository root が既定。custom subdirectory は Advanced（作成時にその commit に実在する相対パスだけ許可）。
custom cwd のときは worktree 全体を writable root に追加し、適用される AGENTS.md chain（root → subdir）を表示します。

### 11–12. Web/Search/Browser と Standard / Fast

- Web search は Codex の既定（cached）のまま。browser / MCP / apps は tool profile で外せます（thread 途中では変えない）。
- 通常の送信は **Send Standard**、明示操作で **Send Fast**（確認ダイアログ付き）。同じ thread 内で毎ターン切替でき、要求した tier を各 turn に保存します。
  Fast / Standard 変更後に cache hit が落ちた場合は、Cache Health の原因に `service tier change` が出ます。

### 13. Retry Guard

次は**無限に retry しません**: quota 枯渇 / context window 超過 / 認証エラー / 同一 tool 失敗の繰り返し。

- Codex が `willRetry: true` で再試行しようとする上記エラーは、turn を interrupt して止め、理由を表示します。
- 同一コマンドが同一出力で 3 回連続失敗したら turn を止めます（回数は設定可）。成功・別の失敗が挟まればカウントは戻ります。
- context window 超過の後は、同じ巨大 context を再送しないよう通常の Send / Resume を **409 `context_overflow`** で止め、
  **Compact** か **Start New Session in Same Worktree** を選ばせます。
- 別機能の自動リトライ（`recovery.py`）とは、これらの失敗を non-retryable として扱うことで整合しています。

### 14. Savings（API 換算）

cache read / write / uncached を分離して計算します（`app/efficiency.py`）。レートは公式 pricing page（2026-10-01）の GPT-6.1-Sol:

| | uncached input | cache write | cache read | output |
| --- | --- | --- | --- | --- |
| Standard | $2.00 | $2.50 | $0.10 | $10.00 |
| Fast（Standard の 2 倍） | $4.00 | $5.00 | $0.20 | $20.00 |
| 272K 超の request（Standard） | $4.00 | $5.00 | $0.20 | $15.00 |

- long-context 倍率（入力・cache ×2、出力 ×1.5）は、**その request の入力サイズが報告されている場合のみ・request 単位で**適用します。
  サイズが分からない turn には適用せず、その旨を note に出します（累積 usage を request サイズと誤認しません）。
- cache write の rate が無い単位（credits、Astra）では、書き込まれた token は通常の uncached input として扱います（rate を作りません）。
- 表示名は「API-equivalent savings」「same-token estimate」などで、**サブスクリプションで実際に支払わなかった金額とは表現しません**。

## 実モデルでの A/B（default 設定 vs Efficiency 設定）

`tests/test_real_ab.py`（`CODEX_GUI_REAL=1`）。同じ小タスク（6,000 行の CSV を集計して 3 行で答える → 続けて 1 行を答える、2 ターン、
`gpt-6.1-sol` / effort low / Standard / 同一 thread）を、A と B で 2 回ずつ **A, B, A, B の順**に実行しました（ウォームアップや cache 共有が片側に有利にならないように）。

- **A default**: Codex の既定（nested agents 許可、tool output default、skills default、tool profile Full）
- **B efficiency**: nested agents OFF、tool output Conservative(8,000)、skills Economy(2,000)、tool profile Development

| run | 設定 | input | cached | uncached | output | request | tool call | 大きな tool output | API 換算 | 正解 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | A | 52,951 | 48,768 | 4,183 | 327 | 4 | 2 | 0 | $0.0165 | 4/4 |
| 2 | B | 45,318 | 42,112 | 3,206 | 332 | 4 | 2 | 0 | $0.0139 | 4/4 |
| 3 | A | 52,915 | 37,376 | 15,539 | 325 | 4 | 2 | 0 | $0.0381 | 4/4 |
| 4 | B | 45,285 | 30,848 | 14,437 | 329 | 4 | 2 | 0 | $0.0352 | 4/4 |

（run 3 / 4 は cache が冷えていた。cache write は実 API が 0 を返した。API 換算は same-token estimate で、実際に支払った額ではない。）

- **品質**: 4 回とも全問正解。tool call 数も request 数も同じで、増えていない。
- **削減**: input は約 **−14%**（約 7.6k tokens / 2 ターン）、API 換算は約 −16%（warm）/ −8%（cold）。小さなタスクなので絶対額は小さい。
- **何が効いたか（モデルを呼ばずに分解、初回 request のサイズ）**:

  | 設定 | 初回 request の変化 |
  | --- | ---: |
  | tool profile = Development（apps / plugins off） | **−5,494 bytes（約 −1.4k tokens 以上 / request）** |
  | nested agents OFF | 0 |
  | tool output = Conservative | 0（このタスクには巨大出力が無い） |
  | skills = Economy | 0（この環境の catalog は約 490 tokens で予算より小さい） |

  つまり、このタスクで**実際に token を減らしたのは tool profile だけ**です。他の 3 つは「効く条件になれば効く / 暴走を防ぐ」設定で、この A/B では差が出ていません。
  tool 宣言は cache される prefix なので、cache が温かいときの料金への効きは小さく、cache が冷えているときと included usage の消費に効きます。
- 4 回・1 種類のタスクなので、傾向の確認であって統計的な結論ではありません。品質低下や tool call 増加は見られませんでしたが、自動 default にはしていません
  （tool profile の既定は Full のまま。Development に変える場合は `app/routes.py` の `context_options()` の `default_tool_profile`）。

## 「実際に token を減らせた設定」と「警告・観測だけの設定」

**実測で token を減らせた**

| 設定 | 実測 |
| --- | --- |
| Tool profile Development / Minimal | 実モデル A/B で input −14%。nested tool 187 → 8、tool 宣言 −4.6KB + plugin/apps 関連の developer 指示。**検証で減ったときだけ「最適化済み」と表示** |

**条件が合うときだけ減らす（仕組みは実測で確認、この A/B では差なし）**

| 設定 | 実測 |
| --- | --- |
| Tool output Conservative(8,000)、さらに小さい custom 値 | 巨大出力 1 回あたり 8,000 で −4%、4,000 で −52%、2,000 で −76%。**16,000 / 32,000 は効果なし**（モデルの cap が 10,000） |
| Skills budget Economy / Balanced | catalog が Codex 既定（約 5.4k）より大きいときに減らす（40 skill の合成 catalog で 2,000 → −3.5k tokens）。**8,000 は逆に増やす**。この環境（4 skill, 約 490 tokens）では効果なし |
| Nested agents OFF | spawn を実際に失敗させる（実測）。token の「削減」ではなく暴走防止。tool 宣言は残る |

**警告・観測だけ（token は減らさない）**

AGENTS.md Health Check、Long Context Guard、Cache Health（miss event / 原因）、Cache Age、Compaction Monitor、大きな tool output / context jump の警告、
Retry Guard（無限 retry の停止。再送の浪費を止めるが通常の消費は減らさない）、Savings 表示。
これらは自動で何かを変更・削除・compact・retry しません。

## 設定（閾値）

ダッシュボードの **Efficiency settings** で変更できます（`GET/PUT/DELETE /api/context/settings`）。範囲外・矛盾する値は 400 で拒否します。

## テスト

```bash
.venv/bin/python -m pytest                      # 通常（実 codex を使うテストはモデルを呼ばない）
CODEX_GUI_REAL=1 .venv/bin/python -m pytest tests/test_real_ab.py -s   # 実モデルでの A/B（サブスクリプションを少量消費）
```

- `tests/test_ctx_integration.py`: 実 codex でツール / skills / AGENTS.md / sub-agent の実効性を測る。
- `tests/test_ctx_e2e.py`: GUI → 実 `codex app-server` → 偽モデル の E2E（cache write の取り込み、per-turn tier、nested agents、tool output limit、cwd、profile 検証）。
- `tests/test_ctx_manager.py` / `test_ctx_api.py`: スクリプト可能な偽 app-server で、イベント・guard・API を検証。
- `tests/test_ui_context.py`: UI の描画（Node + stub DOM）。

## 未確認・制約

- 実測は codex-cli 0.159.2 / gpt-6.1-sol / この config の結果です。モデルや Codex が変わると変わり得ます（検証はいつでも再実行できます）。
- 推定 token は UTF-8 bytes ÷ 4（Codex の `original_token_count` と同じ規則）です。
- `aggregatedOutput` は Codex が最大 1 MiB に切って渡すため、それを超える raw 出力の推定は下限です。
- tool profile の検証は `codex exec`（と app-server で同じ値になることを確認済み）の catalog です。ブラウザ拡張のホストなど別経路の追加 tool は測っていません。
- sub-agent の抑止は「spawn を失敗させる」方式で、tool 宣言（約 5.5KB、cache される prefix）自体は残ります。
