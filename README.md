# Codex GUI

Codex CLI (`codex exec --json`) を複数同時に動かして一画面で管理する、自分専用のローカル Web GUI。

- 1 タスク = 1 Git branch = 1 Git worktree（worktree は GUI 側が管理。Codex の `--worktree` は使わない）
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

## 使い方

1. `+ New Task` で入力して Run（すべて GUI で選べます）
   - **Repository:** `Browse…` でサーバー側のフォルダを辿って選択（git リポジトリは `git` バッジ付き）。最近使ったリポジトリはチップで 1 クリック
   - **Base ref:** 選んだリポジトリのブランチ / 既存 worktree / リモートブランチ / タグから選択（`Custom…` で任意の ref やコミットも可）。
     worktree を選んだ場合は **commit 済みの状態** から分岐します（未コミットの変更は含まれません）
   - **Model:** `codex debug models` のカタログから選択（`Custom…` で ID 直接入力）。Reasoning effort の選択肢もモデルごとの対応値に連動
   - **Run & add another:** フォームを開いたまま次のタスクを追加。**同じリポジトリで複数タスクを並列実行**できます（タスクごとに別 branch・別 worktree）
2. worktree と branch が作られ、その中で Codex が起動する（ダッシュボードは 2 秒ごとに自動更新）
3. ダッシュボードはリポジトリで絞り込み可能。行をクリックすると詳細画面。Codex ログ（種別名をクリックで生イベント JSON）と Git の Status / Diff / Log を確認
4. Stop / Commit / Push / Delete Worktree / Delete Branch は詳細画面から

Diff は **base commit との差分**（Codex が作った commit も含む）に、未追跡ファイルを加えたものです。

## 保存場所

```text
$CODEX_GUI_HOME/
├── codex-gui.db              タスク履歴 (SQLite)
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

実 Codex は使わず、`tests/fake_codex.py` を `CodexRunner` の差し替えで起動します。
実際のサブプロセス・シグナル・git を使うので、並列実行や Stop → SIGKILL 昇格もテストされます。

## 構成

```text
app/
  main.py           アプリ生成・起動時の復旧・終了処理
  routes.py         HTML ページと JSON API
  task_manager.py   タスク作成・並列実行・Stop・復旧・Git 操作
  codex_runner.py   codex コマンド組み立て・イベント解析・プロセス停止
  logstore.py       タスク別 JSONL ログの書き込み/増分読み出し
  git_manager.py    git CLI ラッパー
  database.py       SQLite
  models.py         ステータス遷移・命名規則
  config.py         設定
static/ templates/  UI（素の HTML/CSS/JS、ポーリング）
tests/
```
