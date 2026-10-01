# Codex GUI

Codex CLI を複数同時に動かして一画面で管理する、ローカル Web GUI。Codex は **`codex app-server`**（thread / turn）経由で動かします。

- 1 タスク = 1 Git branch = 1 Git worktree = 1 Codex thread。追加指示は同じ thread の次の turn（`thread/resume` + `turn/start`）なので、会話履歴は Codex が持ち続け、prompt cache（cached input）が効きます。GUI が履歴を貼り直すことはありません
- 定額枠をできるだけ有効に使うための既定値（GPT-6.1 Sol / Standard / Low / 低 verbosity / web search は Codex の既定どおり cached）、cache hit・context・利用枠の表示（[使用量の最適化](#使用量の最適化)）
- リポジトリと Task worktree の `AGENTS.md` をブラウザで編集（[AGENTS.md エディタ](#agentsmd-エディタ)）
- worktree は GUI 側が管理（Codex の `--worktree` は使わない）
- 複数 Codex プロセスの並列実行、ログのリアルタイム表示、Git status / diff / log の確認
- Stop、Commit / Push、Worktree / Branch の削除、SQLite による履歴保存

## セキュリティ

この GUI 自体にはログイン（認証）がありません。開ける人は誰でも、サーバー上で Codex（＝コマンド実行・ファイル編集・Git push）を操作できます。
既定では `127.0.0.1` だけで待ち受けます。ほかのマシンへ公開するなら、認証付きの reverse proxy か VPN（Tailscale / WireGuard など）の内側に置いてください。
（下の「Codex へのサインイン」は Codex（ChatGPT アカウント）へのサインインで、GUI のログインではありません。）

## 必要環境

- Linux / WSL2、Python 3.10+、git
- `codex` コマンドがインストール済みであること（`codex app-server --help` が動くこと）。ChatGPT アカウントへのサインインは GUI のダッシュボードからできます（[Codex へのサインイン](#codex-へのサインイン)）。`codex login` で済ませておいても構いません
  - 動作確認したバージョン: codex-cli 0.159.2（調査記録: [docs/codex-capabilities.md](docs/codex-capabilities.md)）
  - app-server が使えない古い CLI では `CODEX_GUI_BACKEND=exec`（従来の `codex exec` 方式。利用枠表示・実行中の追加指示・compact は使えません）

## 起動

```bash
git clone https://github.com/Kanade147359/codex-gui.git
cd codex-gui
./run.sh
```

初回は `.venv` を作って依存パッケージを入れます。起動後、ブラウザ（WSL2 なら Windows 側のブラウザ）で <http://127.0.0.1:8765> を開いてください。

## Codex へのサインイン

Codex が ChatGPT アカウントにサインインしていないとき（または API キーでサインインしているとき）、ダッシュボードの上部に **Codex sign-in** が出ます。`codex login` と同じことを GUI からできます。

| ボタン | 動き |
| --- | --- |
| **Sign in with ChatGPT (open browser)** | サインイン用のページをブラウザの新しいタブで開きます（開けなかったときは **Open sign-in page** を押す）。サインインが終わるとこの画面が自動で更新され、表示が消えます。サインイン後の戻り先は **Codex が動いているマシンの localhost** なので、Codex と同じマシンのブラウザで行ってください |
| **Use a device code** | 表示されたコードを、ページ（**Open sign-in page**）に入力します。別のマシンのブラウザからでも使えます（リモートで GUI を開いているときはこちら） |
| **Cancel** | 進行中のサインインを取り消します |

- サインイン済みなら、ダッシュボードに `Codex: signed in as <メール> (<プラン>)` と出ます。
- パスワードやトークンは GUI を通りません。ブラウザが OpenAI と直接やりとりし、結果は Codex 自身の保管場所（`~/.codex`）に保存されます。GUI は何も保存しません。
- 失敗したときは理由を表示します。API キーでのサインインはできません（`CODEX_GUI_SUBSCRIPTION_ONLY=1` の既定では、API キー認証だと Task も開始しません）。サインアウトの操作はありません（必要なら `codex logout`）。
- 実体は app-server の `account/read` / `account/login/start`（`chatgpt` / `chatgptDeviceCode`）/ `account/login/completed` / `account/login/cancel` です。API は `GET /api/codex/account`、`POST /api/codex/login`（`{"method": "browser" | "device"}`）、`POST /api/codex/login/cancel`。

環境変数:

| 変数 | 既定値 | 内容 |
| --- | --- | --- |
| `CODEX_GUI_HOME` | `~/.local/share/codex-gui` | データ保存先 |
| `CODEX_BIN` | `codex` | codex 実行ファイル |
| `CODEX_GUI_MAX_CONCURRENT` | `0` (無制限) | 同時実行数。超過分は `queued` で待つ（利用枠から自動で変えることはありません） |
| `CODEX_GUI_BACKEND` | `app-server` | `app-server`（thread / turn）または `exec`（従来の `codex exec`） |
| `CODEX_GUI_SUBSCRIPTION_ONLY` | `1` | `1`: ChatGPT ログイン以外では実行せず、codex の環境から API キー変数を除く |
| `CODEX_GUI_CONTEXT_WARN_PERCENT` | `80` | Context Guard が警告する context 使用率（%） |
| `CODEX_GUI_PREFERRED_MODEL` | `gpt-6.1-sol` | 推奨モデル。`codex debug models` に無ければ「Codex default」 |
| `CODEX_GUI_HOST` / `CODEX_GUI_PORT` | `127.0.0.1` / `8765` | 待ち受け先 |
| `CODEX_GUI_AUTO_RETRY` / `CODEX_GUI_MAX_RETRIES` | `1` / `3` | 予期しない停止の自動リトライの既定（Task ごとに変更可） |
| `CODEX_GUI_RETRY_BACKOFF` | `10,30,60` | リトライまでの待ち秒数（カンマ区切り。最後の値を以降も使う） |
| `CODEX_GUI_SCHEDULER_INTERVAL` | `2` | 待機中・リトライ待ちタスクを見に行く間隔（秒） |
| `CODEX_GUI_SSH_KEY` | `~/.ssh/id_ed25519` | agent が空のときに `ssh-add` する鍵 |
| `CODEX_GUI_SSH_AGENT_ENV` | `~/.ssh/agent.env` | agent の環境変数の保存先 |
| `CODEX_GUI_SSH_DIAGNOSTICS` | `1` | `0` で起動時の SSH 診断ログを無効化 |
| `CODEX_GUI_SSH_GITHUB_TEST` | `1` | `0` で起動時の `ssh -T git@github.com` を無効化 |

## SSH 認証

Push（GUI の Push ボタン）や Codex 内の git 操作が SSH リモート（`git@github.com:...`）に届くよう、`run.sh` が WSL 上の `ssh-agent` を管理します。

### 仕組み

`./run.sh` は起動時に次の順で agent を決めます。

1. 環境にある `SSH_AUTH_SOCK` の agent が応答すればそれを使う
2. `~/.ssh/agent.env`（前回起動した agent の `SSH_AUTH_SOCK` / `SSH_AGENT_PID`）を読み、応答すれば再利用する
3. どちらも使えないときだけ `ssh-agent` を新規起動し、`~/.ssh/agent.env`（権限 600）に保存する

その後 `ssh-add -l` で鍵を確認し、**1 つも登録されていなければ** `~/.ssh/id_ed25519` を `ssh-add` します。
パスフレーズがある鍵は通常の `ssh-add` プロンプトで入力します（`run.sh` を端末から起動してください）。

`SSH_AUTH_SOCK` は `export` されたまま uvicorn → FastAPI → Codex / git のサブプロセスに継承されます。
Codex task を開始するたびに agent を起こすことはありません（agent は `run.sh` 起動時の 1 回だけ）。
`run.sh` を経由せずに uvicorn を直接起動した場合も、`~/.ssh/agent.env` の socket が生きていればサブプロセスにはそれを渡します。

他のシェルでも同じ agent を使いたいときは `. ~/.ssh/agent.env` を実行してください。

### 起動時の診断ログ

GUI 起動時に、次の結果をログ（`run.sh` を実行した端末）に出します。

- `ssh-add -l`（終了コード 0 = 鍵あり / 1 = agent はあるが鍵なし / 2 = agent に接続できない）
- `git config --get remote.origin.url`（GUI の起動ディレクトリと、最近使ったリポジトリ）
- リモートが GitHub の SSH URL なら `ssh -T git@github.com`

GitHub は `ssh -T` 成功時でも **終了コード 1** を返します。そのため終了コードでは判定せず、出力に
`successfully authenticated` が含まれるかで成否を判断します。診断は非同期に実行され、起動を待たせません。

### トラブルシューティング

| 症状 | 確認・対処 |
| --- | --- |
| ログに `ssh-add -l (exit 2)` | agent に繋がっていません。`./run.sh` で起動し直す。`~/.ssh/agent.env` を消して再起動しても可 |
| `ssh-add -l (exit 1)` / `no keys` | `ssh-add ~/.ssh/id_ed25519`。別の鍵なら `CODEX_GUI_SSH_KEY` を指定 |
| `ssh-add` がパスフレーズを聞けず失敗 | 端末から `./run.sh` を実行するか、先に手動で `ssh-add` しておく |
| `ssh -T` が `Permission denied (publickey)` | agent の鍵が GitHub に登録されていない。`ssh-add -l` の公開鍵と <https://github.com/settings/keys> を比較 |
| `Host key verification failed` | 初回接続。端末で一度 `ssh -T git@github.com` を実行して host key を承認 |
| `Could not resolve hostname` / timeout | ネットワーク・DNS・プロキシの問題（WSL の DNS を確認） |
| remote が `https://` | SSH 認証は使われません。`git remote set-url origin git@github.com:OWNER/REPO.git` |
| Codex の sandbox 内から push できない | **Network access**（既定 ON）が OFF の Task、または古い Task（この項目より前に作ったもの）はネットワークが遮られます。ON でも sandbox が SSH agent の socket を遮ることがあります。GUI の **Commit / Push** ボタン（GUI プロセスから実行）を使ってください |
| 古い agent が残っている | `pkill -f 'ssh-agent -s'` は他の agent も止めるので、`kill $SSH_AGENT_PID`（`~/.ssh/agent.env` の pid）で止める |

手動確認: `. ~/.ssh/agent.env && ssh-add -l && ssh -T git@github.com`

## 使い方

1. `+ New Task` で入力して Run（すべて GUI で選べます）
   - **Repository:** `Browse…` でサーバー側のフォルダを辿って選択（git リポジトリは `git` バッジ付き）。最近使ったリポジトリはチップで 1 クリック。選ぶと `Git: clean` / `AGENTS.md: Found | Not found` が出ます
   - **Base ref:** 選んだリポジトリのブランチ / 既存 worktree / リモートブランチ / タグから選択（`Custom…` で任意の ref やコミットも可）。
     worktree を選んだ場合は **commit 済みの状態** から分岐します（未コミットの変更は含まれません）
   - **Model / Reasoning / Speed:** 既定は GPT-6.1 Sol（この codex が認識しているとき）/ Low / Standard。選択肢は `codex debug models` の内容（モデルごとの対応 effort・tier）に連動
   - **Auto approve / Web search / Network access / Adaptive reasoning / Context guard**、**Advanced settings**（Output verbosity・Sandbox・追加の書き込み可能ディレクトリ・feature flags）
   - **Run & add another:** フォームを開いたまま次のタスクを追加。**同じリポジトリで複数タスクを並列実行**できます（タスクごとに別 branch・別 worktree）
2. worktree と branch が作られ、その中で Codex が起動する（ダッシュボードは 2 秒ごとに自動更新）
3. ダッシュボードはリポジトリで絞り込み可能。行をクリックすると詳細画面。Codex ログ（種別名をクリックで生イベント JSON）と Git の Status / Diff / Log を確認
4. 追加指示は詳細画面の **Additional instruction** から（下記「追加指示」）
5. Stop / Commit / Push / Delete Worktree / Delete Branch は詳細画面から

Diff は **base commit との差分**（Codex が作った commit も含む）に、未追跡ファイルを加えたものです。

## 追加指示

1 タスクは 1 つの Codex thread を使い続けます。

| 状況 | 操作 | 実行されること |
| --- | --- | --- |
| タスク作成 | Run | `thread/start`（設定を固定）→ `turn/start`。thread id を `tasks.codex_thread_id` に保存 |
| 停止中・完了後 | **Send** | `thread/resume`（作成時と同じ設定）→ `turn/start`。同じ thread の次の turn |
| **実行中** | **Send to running turn** | `turn/steer`（実行中の turn に追加）。app-server バックエンドのみ |
| 完了後 | **Compact Thread** | `thread/compact/start`（確認あり。Task / worktree / branch / thread はそのまま） |
| いつでも（停止後） | **Start New Session** | 同じ worktree で新しい thread。旧 thread の会話と cache は引き継がない。確認あり |

- 入力した文字列を**そのまま**送ります。日時・ID・過去のやり取りを GUI が足すことはありません（履歴は Codex の thread が持ちます。GUI のログには表示用に指示文を残しますが、再送はしません）。
- model / reasoning effort / Speed / sandbox / approval / web search は**タスク作成時に決まり、全ターンで同じ値**を送ります（途中変更の UI はありません。変えると prompt prefix が変わって cache が効きにくくなるため）。
  例外は **Retry with Medium / High** を押したときの reasoning effort だけです（押したときだけ。ログに記録されます）。
- `codex queue` は使いません: codex-cli 0.159.2 では、実行中の `codex exec` に `codex queue` で送ると、メッセージは thread の履歴には入りますが
  exec がその turn を `turn_aborted` にして終了するため**回答されずに失われます**。app-server の `turn/steer` を使います。
  `exec` バックエンドでは実行中の追加指示は不可です（Send は無効、API は 409）。
- Codex が thread id を報告しなかった場合はログに記録し、そのタスクでは Send できません（Start New Session は可能）。resume 時に別の thread id が返った場合は、ログに `WARNING` を出します。
- GUI を再起動しても thread は Codex 側に残っているので、DB の `codex_thread_id` から再開できます（実測済み）。

## 使用量の最適化

目的は節約ではなく、**無駄な context 送信・session 再作成・不要な高 reasoning・非 cache の入力を減らす**ことです。
利用枠の回避・API キーへの自動 fallback・複数アカウント・「枠を使い切るペース」の自動制御・利用枠からの並列数計算はありません。

### 既定値

| 設定 | 既定 | 備考 |
| --- | --- | --- |
| Model | GPT-6.1 Sol（この codex が一覧に出しているとき）、無ければ Codex default | `Use Codex default` / `Other…` も選べる |
| Reasoning | **Low** | `Auto` = Codex / モデルの既定。選べるのはそのモデルが対応する値だけ（Low〜Ultra） |
| Speed | **Standard** | Fast を選ぶと小さく *Fast mode consumes included usage more quickly.* |
| Output verbosity | **Low**（Advanced） | `model_verbosity` |
| Auto approve | **ON** | `--approve-for-me` 相当（下記） |
| Sandbox | **workspace-write**（Advanced） | `read-only` も選べる。`danger-full-access` は選べない |
| Web search | **Cached** | Codex 自身の既定（0.159.2 は web search が既定で有効）に合わせます。**Cached** = OpenAI の検索インデックスの結果、**Live** = 実際の Web、**Off** = 無効（Codex がローカルのファイルを読むことは妨げません）。選べる値は Task 作成時に固定。以前の Task は、ON だったものは Live、OFF は Off のままです |
| Network access | **ON** | workspace-write sandbox 内のネットワーク（`git fetch` / `git push`、`gh`、パッケージのインストールなど）。OFF にすると sandbox 内の通信はすべて失敗します（ローカルの `git status` / `diff` / `log` は使えます）。Web search（モデルの検索ツール）とは別です。`read-only` では無関係。この項目より前に作った Task は OFF のままです |
| Adaptive reasoning | ON | 下記 |
| Context guard | ON | 下記 |
| Use subscription authentication only | ON（変更不可の表示） | 下記 |

- **Ultra**（"Maximum reasoning with automatic task delegation"）は**明示的に選んだときだけ**使います。選ぶと *Ultra may use substantially more compute.* と出ます。自動選択・自動昇格の対象外です。
- **Adaptive reasoning** は、Codex 自身が turn を `failed` と報告した（かつ quota 停止ではない）ときに、詳細画面で **Retry with Medium / High** を強調して勧めるだけです。
  **自動では上げません**（process の exit code だけで上げることもしません）。提案は High まで。XHigh / Max / Ultra は自動では出ません。
- 共通の作業方針（巨大なファイル・ログを丸ごと読まない、`rg` / 範囲指定の `sed` / `head`・`tail` / 絞ったテスト / 短い出力）は、**新しい thread の開始時に一度だけ** `developerInstructions` として渡します（毎 turn の再送なし）。
  既定の文面は [app/instructions.py](app/instructions.py)。`$CODEX_GUI_HOME/instructions.md` を置くと差し替わり、空にすると無効です。リポジトリの `AGENTS.md` は自動では変更しません。
- **prompt cache を壊しにくくする**: GUI が加える固定 instruction は定数で、日時・PID・Task ID・UUID・worktree 作成時刻・quota などの可変値を含みません（テストあり）。
  同一タスクでは上の設定を変更しません。

### cache と usage

Task 詳細に最新ターンの Input / Cached / Uncached / Cache hit / Output（あれば Reasoning）、**ターンごとの表**、ダッシュボードの **CACHE** 列に最新ターンの hit 率を表示します。

- app-server の `thread/tokenUsage/updated`（exec では `turn.completed.usage`）は **thread の累積値**です。GUI は前回累積値との差を「そのターンの usage」として `turns` テーブルに保存します。
  累積値が減った場合は、ターン単位の値とみなして生の値を使います（ログに記録）。
- Cache hit = `cached_input_tokens / input_tokens × 100`。`input_tokens == 0` のときは表示しません。
- compaction も token を使うため、`turns` に `kind = compact` の行として残ります（表では `(compact)`）。
- Prompt cache は best-effort です。直後に連続して送ると hit 率が低いことがあります（実測では 76% → 76% → 99%）。

### Context と Context Guard

Task 詳細の **Context**（Current / Model window / バー）と、ダッシュボードの **CTX** 列。context サイズは Codex が報告する `last.totalTokens`、窓は `modelContextWindow`（モデルのメタデータ由来。GUI に値は決め打ちしていません）です。

- **Context Guard**（既定 ON）: 窓の 80%（`CODEX_GUI_CONTEXT_WARN_PERCENT`）を超えると、*This thread has become large. Compaction may reduce repeated context processing.* と **[Compact]** を出します（警告のみ。自動 compact はしません）。
- **Compact Thread**: 確認ダイアログの後に `thread/compact/start`。compact 後の context サイズは次のターンで分かります（それまで unknown）。

### Codex Usage（利用枠）

ダッシュボード上部に `account/rateLimits/read` の内容を表示します（15 秒ごと。サーバー側 30 秒 cache）。

```text
Codex Usage (prolite)
Weekly   ██░░░░░░░░   11%   Reset: Oct 8 18:20
Running: 4
```

- ウィンドウは `windowDurationMins` で **5 hour / Weekly** に分類します。**プランによっては週次の 1 本だけで 5 時間枠は返ってきません**（確認した環境では週次のみ。2 本返るプランもあります）。無い枠は表示しません。
- `Available resets: N`（利用可能な reset 数）も表示のみ。**reset を GUI が使うことはありません。**
- 利用枠の履歴を `rate_limit_history` テーブルに保存します（Task の各ターンの開始時・終了時は必ず、Codex からの更新通知は 5 分に 1 回まで）。後から Task ごとの消費を分析するためで、**自動制御には使いません**。
  `GET /api/limits/history?task_id=...` で取れます。
- Task 詳細の **Observed quota change**: 最新ターンの開始前後の使用率（`5 hour 31% → 33%` / `Weekly 12% → 13%`）。
  整数 % の粗い値なので *Observed only; not an exact per-task cost.* と表示し、**他の Task が同時に動いていたときは個別 Task の消費として断定しません**（その旨を表示）。

### 利用枠が尽きたとき

Codex が「枠が使えない」と言ったとき（`ordinaryUsageAllowed: false`、`rateLimitReachedType`、`usageLimitExceeded` / `rateLimitExceeded` エラー）、Task は **`waiting-for-quota`**（ダッシュボードでは *Waiting for Codex quota*）になります。

- **自動で再試行しません。** 別の課金経路にも移りません。枠が戻ったら詳細画面の **Retry last instruction** で、同じ thread に最後の指示を送り直せます。
- 開始前に枠が無いと分かった場合は、turn を開始しません。
- 単に Codex がエラーで `failed` になった場合は quota 扱いにしません。

### サブスクリプション認証のみ

- ターン開始前に `account/read` で認証が ChatGPT（`type: "chatgpt"`）であることを確認します。API キー認証・未ログインなら**開始せず `failed`**（理由を表示）。API キー課金に切り替えることはありません。
- codex の子プロセスには `OPENAI_API_KEY` / `CODEX_API_KEY` / `OPENAI_BASE_URL` などを渡しません。
- 解除は `CODEX_GUI_SUBSCRIPTION_ONLY=0`（既定は ON。GUI からは変更できません）。

## Context Efficiency

多数の Task を並列に動かすとき、品質を落とさずに無駄な token / context を減らすための機能です。**何が実際に効くか（実 Codex での測定結果）は
[docs/context-efficiency.md](docs/context-efficiency.md)** にまとめています。要点:

- **AGENTS.md は自動で編集しません。** Health Check（New Task の preview と Task Detail）が chain・サイズ・budget 使用率・重複・「毎回読め」系の指示を
  警告するだけで、直すのは Edit ボタンから人が行います。
- New Task の **Context efficiency**: Tool output limit / Tool profile（Full・Development・Minimal）/ Allow subagents（既定 OFF）、Advanced に Skills catalog budget と
  Working directory。設定は Task 作成時に決まり、同じ thread では凍結されます。tool profile が「最適化済み」と表示されるのは、実 Codex で tool 数が減ったことを測れたときだけです。
- Task Detail の **Context efficiency**: Context と Compactions、Cache age（HOT/WARM/COLD、参考表示のみ。cache を温めるための prompt は送りません）、
  ターンごとの cache read / write / uncached と cache miss の原因候補、大きな tool output、long-context の警告
  （`≥272K` で **Continue / Compact / Start New Session in Same Worktree** を提示、自動 compact はしません）。
- 送信は **Send Standard**（通常）と **Send Fast**（明示操作）。要求した speed は各ターンに保存されます。
- quota 枯渇・context 超過・認証エラー・同じ tool 失敗の繰り返しは無限に retry せず、状態を表示して止まります。
- 閾値はダッシュボードの **Efficiency settings** で変更できます。

```bash
.venv/bin/python -m pytest                                              # モデルは呼ばない（実 codex は使う）
CODEX_GUI_REAL=1 .venv/bin/python -m pytest tests/test_real_ab.py -s   # 実モデルでの A/B（少量消費）
```

## AGENTS.md エディタ

プロジェクト共通のルールを毎回 prompt で送る代わりに `AGENTS.md` に置けるよう、GUI から確認・編集できます。GUI が `AGENTS.md` の中身を prompt に足すことはありません（Codex 自身が読み込みます。prompt の重複・context の重複・cache prefix の変動を避けるため）。

| 種類 | 対象 | 開き方 |
| --- | --- | --- |
| **Repository AGENTS.md** | リポジトリ**本体（main checkout）**の `AGENTS.md` | ダッシュボードでリポジトリを選ぶと出る `[AGENTS.md]`（`/agents?repository=…`）。New Task のフォームからも |
| **Task Worktree AGENTS.md** | その Task の **worktree 内のコピー** | Task 詳細の `[Edit Worktree AGENTS.md]`（`/tasks/<id>/agents`） |

この 2 つは**別のファイル**です。画面にも種別が表示され、Task worktree の編集は main 側に影響しません（逆も同じ）。リポジトリ用エディタに Task の worktree を渡すと拒否します。
Task の worktree には作成時に base ref の `AGENTS.md` が入ります。以降 main 側を編集しても既存 worktree のコピーは変わりません。

- **存在しない**ときは `AGENTS.md does not exist` と `Create AGENTS.md`。Save で作成します。
- textarea ベース（monospace、行番号、Tab 入力、**Ctrl+S** で保存、未保存の印）。**Reload / Save**。保存成功で `Saved AGENTS.md`、失敗はエラー内容を表示。内容が同じなら書き込みません。
- 読み込み後にファイルが外で変更されていたら、上書きせず **409**（Reload を促す）。CRLF のファイルは CRLF のまま保存します。UTF-8 以外・1 MiB 超は拒否します。
- **Git status**（`M AGENTS.md` / Untracked など）と **View Diff**（`git diff HEAD -- AGENTS.md` 相当。未追跡ファイルは全行追加として表示）。**commit はしません**（Commit は別途）。
- **Nested AGENTS.md**: リポジトリ内のサブディレクトリにある `AGENTS.md`（`.gitignore` されたものを除く）を **File** セレクタで選んで編集できます。
- ダッシュボードのリポジトリ行に `Git: clean` / `AGENTS.md: found` を表示します。
- 書けるのは「リポジトリ（または worktree）内の、名前が `AGENTS.md` のファイル」だけです（`..`・絶対パス・リポジトリ外へ出る symlink は拒否）。

## 保存場所

```text
$CODEX_GUI_HOME/
├── codex-gui.db              タスク履歴・ターンごとの usage・利用枠の履歴 (SQLite)
├── instructions.md           （任意）共通の作業方針。無ければ既定の文面
├── logs/<task-id>.jsonl      タスクごとのログ（stdout の生イベント、JSON でない行、stderr、system）
└── worktrees/<repo>/<task-id>/
```

branch 名は `codex-gui/<task-id>-<slug>`。

## 自動承認について

Network access（既定 ON）は `sandbox_workspace_write.network_access=true` を thread の config として全ターンに渡します（app-server の `thread/start` が `networkAccess: true` を返すことを確認済み）。
ON の間、Codex は sandbox の中から任意の外部へ通信できます（プロンプトに紛れた指示でコードや秘密が外に出るリスクが増えます）。信頼できないリポジトリ・指示では OFF にしてください。

Auto approval（既定 ON）は、app-server では `approvalPolicy: "on-request"` + `approvalsReviewer: "auto_review"` + `sandbox: "workspace-write"`、
`exec` バックエンドでは `codex exec --approve-for-me`（どちらも workspace-write sandbox 内で承認リクエストを自動レビューに回す）です。
OFF のときは `approvalPolicy: "never"`（sandbox 内で完結。昇格が要る操作は失敗。GUI に承認ダイアログは無いため）。
`--dangerously-bypass-approvals-and-sandbox` と `danger-full-access` は一切使いません（テストでも検証しています）。

sandbox の都合で、Codex 自身は worktree の外にある `.git` に書き込めないことがあります。その場合は GUI の **Commit** ボタンで commit してください。

## Task の依存関係（After other tasks complete）

New Task の **Run** で **After other tasks complete** を選び、**Depends on** で待つ Task を選ぶと、それらがすべて `completed` になってから自動で始まります
（A・B・C → D。ポリシーは `all_success`。将来 `all_terminal` などを足せる作りです）。

- **worktree は実行直前に作ります。** 待機中の Task は branch 名と worktree のパスだけを持ち、worktree はありません（無駄な worktree を作りません）。
  base ref は作成時に存在確認し、**開始時点の ref** から分岐します。
- 状態: `waiting_dependencies`（待機中。Dashboard は `Waiting (2/3 complete)`）→ `queued`（準備完了、まだ runner に claim されていない）→ `starting` → `running`。
- 依存先が `failed` / `stopped` / `blocked` になると、この Task は **`blocked`**（`Dependency B failed`）になり、勝手には実行されません。
  詳細画面の **Run Anyway**（依存を無視して開始）か **Retry Failed Dependency**（失敗した依存先を同じ worktree・同じ thread で再実行し、もう一度待つ）で進めます。
- 依存先が **リトライ中（`retry_wait`）の間は待ちます**。一時的な失敗だけでは `blocked` になりません。最終的に `failed` になった時点で `blocked` です。
  `waiting-for-quota` / `interrupted` の依存先も「再開できる一時停止」なので待ち続けます。
- 依存先を Stop すると `stopped` なので、依存する Task は `blocked` になります。
- 依存は DAG です。**自己依存・重複・循環は拒否**します（`PUT /api/tasks/{id}/dependencies` で未開始の Task の依存先を差し替えるときも同じ検査）。
- 二重起動しない仕組み: `waiting_dependencies → queued` は「全依存先が completed」の判定を含む **1 本の条件付き UPDATE**、`queued → 実行` は `claimed_by IS NULL` を条件にした
  **1 本の UPDATE（claim）** です。親が同時に終わっても、リスナー・スケジューラ・API が何度評価しても、起動できるのは 1 回だけです。

## 自動復旧（リトライ）

予期しない停止（Codex の子プロセスが落ちた、app-server が落ちた、一時的な接続 / I/O エラー、原因不明の失敗）は、既定で **最大 3 回** まで自動で再開します（New Task の
**Auto recovery**、Task ごとに変更可）。再開は **同じ Task・同じ worktree・同じ branch・同じ Codex thread** です。

| 分類 | 例 | 動作 |
| --- | --- | --- |
| retryable | プロセスが signal で終了 / app-server の終了・タイムアウト / `httpConnectionFailed` / `responseStreamDisconnected` / `serverOverloaded` / `internalServerError` / EAGAIN 等 | リトライ |
| unknown | 分類できない失敗 | **リトライ**（ただし上限は必ず守る） |
| quota | `usageLimitExceeded` / `rateLimitExceeded` / rate limit | リトライせず `waiting-for-quota`（回数を消費しない。手動で再開。API 課金・別モデルへの fallback なし） |
| non-retryable | **ユーザーの Stop** / 認証 / 設定 / 不正な model / 不正な引数 / 不正な Git リポジトリ / worktree 作成の恒久エラー / 依存先の失敗 / worktree が消えている / thread が存在しない / context window 超過 / 上限到達 | `stopped` / `failed`（リトライしない） |

- **間隔:** 10 秒 → 30 秒 → 60 秒（`CODEX_GUI_RETRY_BACKOFF`）。連続リトライはしません。待機中は `Retry 1/3 in 18s`。
- **再開の方法:** 元の指示を送り直さず、固定の短い recovery instruction を同じ thread に送ります。Codex が現在の状態（`git status` / `git log`・会話）を確認し、終わっている作業はやり直しません
  （同じ commit / push を重複しない）。
  Codex 0.159.2 には「中断した turn を自動で続ける」機能が **ない**ことを実機で確認しています（`codex exec resume <id>` はプロンプト必須）。
- **turn が始まる前に落ちた場合**（Codex が `turn/started` を返す前）は、指示が thread に届いていない可能性があるため、recovery instruction ではなく **元の指示をもう一度**、同じ thread に送ります
  （まだ何も実行されていないので二重実行になりません）。thread id がまだ無い場合は、同じ worktree で新しい thread を作り、ログに
  `Retry started a new Codex thread because no previous thread ID existed.` と残します。
- **Git の安全:** 再開前に worktree の存在を確認します（**消えていても作り直さず `failed`**）。`git status` / HEAD / push 状況を **記録するだけ**で、`reset` / `clean` / `checkout` はしません。
  途中の変更は Codex が現在の状態を見て続けます。GUI が履歴を書き換えることはありません。
- **Stop** は明示的な操作なので `stopped`。リトライしません（依存する Task は `blocked`）。
- **手動 Retry**（`failed` / `stopped`）: 同じ worktree・同じ thread で再実行。自動リトライの上限を超えているときは確認ダイアログが出ます。別の thread にしたいときは **Start New Session**。
  `retry_wait` 中は **Retry Now**、**Disable Auto Retry** で待機中のリトライを取り消せます。
- **履歴:** `task_attempts` テーブルに 1 回の実行ごと（初回 / 追加指示 / 自動・手動リトライ / GUI 再起動後の復旧）の開始・終了・exit code・結果・失敗の種類・thread id・resume か否か・service tier・
  reasoning effort・再開前の git 状態を記録します（token usage の `turns` とは別）。詳細画面の Recovery に一覧が出ます。

### GUI 自体が落ちたとき

起動時に `running` / `starting` / `retry_wait` / `queued` の Task を見直します。

- `running` / `starting`: 記録した **プロセスがまだ生きていれば**（pid・**起動時刻**・コマンドラインがすべて一致。pid の使い回しは別プロセス扱い）そのままにして監視し、終了したらリトライします。
  いなければ、自動リトライが有効で上限内なら `retry_wait`、そうでなければ `failed`。**GUI の再起動だけが原因で同じ Task を二重に実行することはありません。**
- `retry_wait`: タイマーは DB にあるので続きます（worktree が消えていたら `failed`）。
- `queued`（claim 済みで未開始）: claim を解放し、スケジューラが開始します。
- 通常終了（Ctrl+C）で止めた Task は、これまで通り `interrupted`（Send / Resume interrupted で続行）。
- 前提: **1 つの DB に GUI プロセスは 1 つ**です（起動時の復旧は前のプロセスの claim を引き継ぎます）。

## Git worktree の扱い

- Stop しても worktree は消えません。途中の変更を確認できます。
- **Delete Worktree** は完了済み（completed / failed / stopped / interrupted）のタスクのみ。未コミットの変更や未追跡ファイルがあると確認ダイアログが出ます。
- worktree を消しても **branch は残ります**（commit も残る）。branch は **Delete Branch** で別途削除します（未マージなら再確認）。

## GUI の再起動

- 履歴は SQLite に残ります。
- GUI を **通常終了**（Ctrl+C）すると、実行中の turn は止められ `interrupted` になり、app-server も終了します。**thread は Codex 側に残る**ので、再起動後に Send で同じ thread を続けられます。
- GUI が強制終了された場合の復旧は「自動復旧（リトライ）」の「GUI 自体が落ちたとき」を参照（プロセスが残っていれば監視、いなければ自動リトライ）。

## テスト

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

実 Codex は使わず、`tests/fake_app_server.py`（app-server の JSON-RPC。thread / turn / 累積 usage / rate limit / steer / interrupt / compact / quota エラー）と
`tests/fake_codex.py`（`codex exec` の従来方式）を、実際の JSON-RPC クライアント・サブプロセス・シグナル・git を通して動かします。
カバー範囲: Codex へのサインイン（ブラウザ / デバイスコード / 失敗 / 取り消し）、Task と thread id の永続化、同一 thread の再利用（resume）、token / cached の parse と cache hit 計算、rate limit の parse（週次のみ / 2 本）、
quota 時の状態遷移（再試行しない）、model・reasoning・Standard・auto approval・web search cached の既定値と送信内容、API キーに fallback しないこと、
context guard の判定、steer / stop / compact、AGENTS.md の読み書き・競合・path 検証・Repository と Task worktree の分離、
依存関係（DAG・循環 / 自己 / 重複の拒否・複数接続からの同時評価でも 1 回だけ起動・依存先のリトライ中は待機・blocked / Run Anyway）、
自動復旧（失敗の分類・上限・間隔・同じ thread / worktree の再利用・Stop / quota / 認証はリトライしない・作業ツリーに触れない・worktree 削除時は再作成しない・
GUI を SIGKILL した後の復旧・pid 使い回しの判別）。

実 Codex に接続する統合テストは分離してあり、`CODEX_GUI_REAL=1` のときだけ動きます（トークンを少し使います。cache hit は表示のみで、成否は同一 thread・usage 取得・context window・利用枠の取得で判定）:

```bash
CODEX_GUI_REAL=1 .venv/bin/python -m pytest tests/test_real_codex.py -s
CODEX_GUI_REAL=1 .venv/bin/python -m pytest tests/test_real_codex.py -s -k killed_mid_turn   # 実 Codex を turn の途中で SIGKILL → 同じ thread・worktree で復旧
CODEX_GUI_REAL=1 CODEX_GUI_REAL_MODEL=gpt-6.1-sol CODEX_GUI_REAL_PAUSE=15 .venv/bin/python -m pytest tests/test_real_codex.py -s -k app_server
```

## 制約

- `Profile`（codex の config profile）は GUI から選べません。
- app-server のプロトコルは experimental です（0.159.2 で確認）。`CODEX_GUI_BACKEND=exec` で従来方式に戻せます。
- compact の効果（context がどれだけ縮むか）は、小さい thread でしか実機確認していません（約 13k の thread では縮みませんでした）。
- `exec` バックエンドには、利用枠・context 取得・quota 検出・steer・compact がありません。
- Adaptive reasoning は提案のみで、自動昇格はしません。
- compact 直後の context サイズは次のターンまで不明です。
- 利用枠の % は整数で粗く、Task ごとの消費は並列実行時に分離できません。
- GUI 自体のログイン（認証）はありません。Codex へのサインインは ChatGPT アカウントのみ（API キー・サインアウトの操作なし）。

## 構成

```text
app/
  main.py           アプリ生成・起動時の復旧・終了処理
  codex_login.py    Codex（ChatGPT）へのサインイン（app-server の account/login/*）
  routes.py         HTML ページと JSON API
  task_manager.py   タスク作成・追加指示(resume / steer)・並列実行・Stop・compact・復旧・Git 操作・usage / 利用枠の記録
  appserver.py      codex app-server の JSON-RPC クライアント（共有プロセス・通知の振り分け・API キーを渡さない環境）
  notifications.py  app-server 通知 → タスクログ
  codex_runner.py   `codex exec` 方式のコマンド組み立て・イベント解析・設定（task_config / approval_params）
  usage.py          token usage・ターン差分・cache hit 率・context 判定・rate limit の parse
  instructions.py   共通の作業方針（thread 開始時に一度だけ渡す定数）
  agents_md.py      AGENTS.md の読み書き・Git 状態・diff・ネスト探索
  logstore.py       タスク別 JSONL ログの書き込み/増分読み出し
  git_manager.py    git CLI ラッパー
  database.py       SQLite
  models.py         ステータス遷移・命名規則
  config.py         設定
  ssh_agent.py      サブプロセスへの SSH_AUTH_SOCK 継承・起動時の SSH 診断
  scheduler.py      待機中 / キュー / リトライ待ちタスクを定期的に進めるバックグラウンド処理
  recovery.py       予期しない停止の分類とリトライの間隔
  efficiency.py     cache・モデル選択による節約量の見積もり（クレジット / API 換算の推定。実際の節約額ではありません）
  cache_health.py   ターンごとの cache hit の記録・miss の原因候補・compact の監視
  ctx_config.py / ctx_guard.py / ctx_manager.py / turn_observer.py   Context Efficiency（設定・ガード・ターンの観測）
  agents_audit.py   AGENTS.md の健全性チェック（読み取り専用）
  tool_probe.py / fake_responses.py   実 codex に偽の Responses API を向けてツール一覧などを測る
  procinfo.py / tokens.py             pid の同一性確認（/proc）・token の概算
docs/               Codex CLI / app-server の調査記録
static/ templates/  UI（素の HTML/CSS/JS、ポーリング）
tests/
```
