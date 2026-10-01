# Codex GUI

Codex CLI (`codex exec --json`) を複数同時に動かして一画面で管理する、自分専用のローカル Web GUI。

- 1 タスク = 1 Git branch = 1 Git worktree = 1 Codex session（thread）。追加指示は `codex exec resume` で同じ session を続けるので、prompt cache（cached input）が効きやすい
- worktree は GUI 側が管理（Codex の `--worktree` は使わない）
- 複数 Codex プロセスの並列実行、ログのリアルタイム表示、Git status / diff / log の確認
- Stop、Commit / Push、Worktree / Branch の削除、SQLite による履歴保存

localhost 専用です。認証・マルチユーザー・クラウド対応はありません。

## 必要環境

- Linux / WSL2、Python 3.10+、git
- `codex` コマンドがインストール・ログイン済みであること（`codex exec --help` が動くこと）
  - 動作確認したバージョン: codex-cli 0.159.2

## 起動

```bash
./run.sh
```

初回は `.venv` を作って依存パッケージを入れます。起動後、Windows のブラウザから <http://127.0.0.1:8765> を開いてください。

環境変数:

| 変数 | 既定値 | 内容 |
| --- | --- | --- |
| `CODEX_GUI_HOME` | `~/.local/share/codex-gui` | データ保存先 |
| `CODEX_BIN` | `codex` | codex 実行ファイル |
| `CODEX_GUI_MAX_CONCURRENT` | `0` (無制限) | 同時実行数。超過分は `queued` で待つ |
| `CODEX_GUI_HOST` / `CODEX_GUI_PORT` | `127.0.0.1` / `8765` | 待ち受け先 |
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
| Codex の sandbox 内から push できない | sandbox が agent の socket やネットワークを遮ることがあります。GUI の **Commit / Push** ボタン（GUI プロセスから実行）を使ってください |
| 古い agent が残っている | `pkill -f 'ssh-agent -s'` は他の agent も止めるので、`kill $SSH_AGENT_PID`（`~/.ssh/agent.env` の pid）で止める |

手動確認: `. ~/.ssh/agent.env && ssh-add -l && ssh -T git@github.com`

## 使い方

1. `+ New Task` で入力して Run（すべて GUI で選べます）
   - **Repository:** `Browse…` でサーバー側のフォルダを辿って選択（git リポジトリは `git` バッジ付き）。最近使ったリポジトリはチップで 1 クリック
   - **Base ref:** 選んだリポジトリのブランチ / 既存 worktree / リモートブランチ / タグから選択（`Custom…` で任意の ref やコミットも可）。
     worktree を選んだ場合は **commit 済みの状態** から分岐します（未コミットの変更は含まれません）
   - **Model:** `codex debug models` のカタログから選択（`Custom…` で ID 直接入力）。Reasoning effort の選択肢もモデルごとの対応値に連動
   - **Run & add another:** フォームを開いたまま次のタスクを追加。**同じリポジトリで複数タスクを並列実行**できます（タスクごとに別 branch・別 worktree）
2. worktree と branch が作られ、その中で Codex が起動する（ダッシュボードは 2 秒ごとに自動更新）
3. ダッシュボードはリポジトリで絞り込み可能。行をクリックすると詳細画面。Codex ログ（種別名をクリックで生イベント JSON）と Git の Status / Diff / Log を確認
4. 追加指示は詳細画面の **Additional instruction** から（下記「追加指示と prompt cache」）
5. Stop / Commit / Push / Delete Worktree / Delete Branch は詳細画面から

Diff は **base commit との差分**（Codex が作った commit も含む）に、未追跡ファイルを加えたものです。

## 追加指示と prompt cache

1 タスクは 1 つの Codex session を使い続けます。

| 操作 | 実行されるコマンド | 備考 |
| --- | --- | --- |
| タスク作成 | `codex exec --json -C <worktree> [--approve-for-me] [--model …] [-c model_reasoning_effort=…] -` | 初回の `thread.started` の `thread_id` を `tasks.codex_thread_id` に保存 |
| **Send** | 上と同じオプション + `resume <thread_id> -` | 同じ session を継続。completed / failed / stopped / interrupted で、session id があるとき |
| **Start New Session** | 初回と同じ `codex exec …` | 同じ worktree で新しい session を作る（旧 session の会話と cache は引き継がない）。確認ダイアログあり |

- prompt は argv ではなく stdin（`-`）で渡し、**ユーザーが入力した文字列をそのまま**送ります。GUI が日時・ID・過去のやり取りを足すことはありません（会話履歴は Codex の session が持ちます。GUI のログには表示用に指示文を残しますが、再送はしません）。
- model / reasoning effort / approval はタスク作成時に決まり、そのタスクの全ターンで同じ引数を使います（途中変更の UI はありません。変えると cache が効きにくくなるため。変えたいときは新しいタスクを作ります）。
- `--no-daemon` などは付けません。Codex 側の既定（共有 app-server）に任せます。
- **実行中のタスクには送れません**（Send は無効、API は 409）。`codex queue` は使いません: codex-cli 0.159.2 では、実行中の `codex exec` に `codex queue` で送ると、メッセージはキューから取り出されて thread の履歴には入りますが、exec はその新しいターンを `turn_aborted` にして終了するため、**回答されないまま失われます**。終了を待ってから Send してください。
- Codex が `thread.started` を出さなかった（session id が取れない）場合はログに記録し、そのタスクでは Send できません（Start New Session は可能）。resume 時に Codex が別の thread id を報告した場合は、ログに `WARNING` を出します。

### Cache usage

`turn.completed` イベントの `usage`（`input_tokens` / `cached_input_tokens` / `output_tokens`、あれば `cache_write_input_tokens` / `reasoning_output_tokens`）を **ターンごと** に `turns` テーブルへ保存します。

- codex-cli 0.159.2 の `usage` は **thread の累積値**です（resume しても増え続けます）。GUI は同じ thread の前回累積値との差を「そのターンの usage」として保存・表示します。累積値が減った場合は、ターン単位の値とみなして生の値を使います（ログに記録）。
- Cache hit = `cached_input_tokens / input_tokens × 100`。`input_tokens == 0` のときは表示しません。
- 詳細画面に **Latest usage** と **ターンごとの表**、ダッシュボードの **CACHE** 列に最新ターンの hit 率を表示します。
- Prompt cache は best-effort です。直後に連続して送ると cache がまだ使えず hit 率が低いことがあります。

## 保存場所

```text
$CODEX_GUI_HOME/
├── codex-gui.db              タスク履歴・ターンごとの usage (SQLite)
├── logs/<task-id>.jsonl      タスクごとのログ（stdout の生イベント、JSON でない行、stderr、system）
└── worktrees/<repo>/<task-id>/
```

branch 名は `codex-gui/<task-id>-<slug>`。

## 自動承認について

Auto approval（既定 ON）は `codex exec --approve-for-me` を付けます。これは workspace-write sandbox 内で承認リクエストを自動レビューに回すモードです。
`--dangerously-bypass-approvals-and-sandbox` は一切使いません（テストでも検証しています）。

sandbox の都合で、Codex 自身は worktree の外にある `.git` に書き込めないことがあります。その場合は GUI の **Commit** ボタンで commit してください。

## Git worktree の扱い

- Stop しても worktree は消えません。途中の変更を確認できます。
- **Delete Worktree** は完了済み（completed / failed / stopped / interrupted）のタスクのみ。未コミットの変更や未追跡ファイルがあると確認ダイアログが出ます。
- worktree を消しても **branch は残ります**（commit も残る）。branch は **Delete Branch** で別途削除します（未マージなら再確認）。

## GUI の再起動

- 履歴は SQLite に残ります。
- GUI を **通常終了**（Ctrl+C）すると、実行中の Codex は止められ `interrupted` になります。Codex プロセスへの再接続は未対応のためです。
- GUI が強制終了された場合、次回起動時に active のまま残ったタスクを `interrupted` にします。

## テスト

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

実 Codex は使わず、`tests/fake_codex.py`（thread id と累積 usage を実 CLI と同じ形で出す）を `CodexRunner` の差し替えで起動します。
実際のサブプロセス・シグナル・git を使うので、並列実行や Stop → SIGKILL 昇格もテストされます。

実 Codex で同一 session の resume を確かめる（トークンを少し使います。cache hit は表示のみで、成否は同一 thread を resume できたかだけで判定）:

```bash
CODEX_GUI_REAL=1 .venv/bin/python -m pytest tests/test_real_codex.py -s
CODEX_GUI_REAL=1 CODEX_GUI_REAL_PAUSE=15 .venv/bin/python -m pytest tests/test_real_codex.py -s   # ターン間を空ける
```

## 構成

```text
app/
  main.py           アプリ生成・起動時の復旧・終了処理
  routes.py         HTML ページと JSON API
  task_manager.py   タスク作成・追加指示(resume)・並列実行・Stop・復旧・Git 操作・usage 記録
  codex_runner.py   codex コマンド組み立て・イベント解析・プロセス停止
  usage.py          turn.completed の usage 抽出・ターン差分・cache hit 率
  logstore.py       タスク別 JSONL ログの書き込み/増分読み出し
  git_manager.py    git CLI ラッパー
  database.py       SQLite
  models.py         ステータス遷移・命名規則
  config.py         設定
  ssh_agent.py      サブプロセスへの SSH_AUTH_SOCK 継承・起動時の SSH 診断
static/ templates/  UI（素の HTML/CSS/JS、ポーリング）
tests/
```
