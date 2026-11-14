# FILE-01 / ARTIFACT-01

## ブラウザでの操作

New Task / Additional Instruction / Scheduled Instruction の **Attach files** で任意形式のファイルを選択、または入力欄へドロップする。PNG・JPEG・WebPの画像入力とクリップボード貼り付けは従来どおり。一般ファイルはアップロード進捗、失敗理由、再試行を表示し、保存完了したIDだけを送信する。本文と保存済み添付の下書きはブラウザのlocalStorageに保持する。

Task詳細のGit領域に **Files** タブがある。base commitからの差分（commit済みを含む）、未追跡ファイル、completion contractのrequired_paths、直近256 KiBのassistantログでリンク／backtick指定されたパスを優先する。JSON・Markdown・ログ等の変更ファイルはartifact候補、それ以外はsourceと表示する。この区分は候補の表示だけに使用し、自動転送の許可には使用しない。相対パスによるフォルダ／単体ファイル検索、ファイル名の絞り込み、単体ダウンロード、選択ZIP、UTF-8テキストのプレビューが可能。実行中も利用できる。プレビューはHTML/Markdownとして実行・描画しない。

Files内の **Send to Task** または **Send selected to Task** で転送対象を明示し、転送先と追加指示を選んで **Confirm transfer** を押す。新規Taskを選んだ場合は添付済みのNew Task画面へ移動し、実行前に内容を確認できる。既存の待機中Taskでは未開始のpending_turnに関連付け、実行開始と競合した場合は409で再操作を求める。実行中Taskへの追加指示は既存のturn/steerを使用する。

New TaskおよびAdditional / Scheduled Instructionの **Attach from Task** からも選択できる。既定ではartifact候補と保存済みスナップショットを表示し、**Show source files too** で通常のソースも明示選択できる。BLOCKED/FAILEDのTaskからも手動選択できる。

新規Taskでは **Wait for source success** を選ぶと、選択パスを将来のArtifact参照として保存し、転送元Taskへの依存関係を同じDB transactionで設定する。まだ存在しない成果物は相対ファイルパスを指定できる。転送元の `completed + task_outcome=success + completion_pending=0` が成立した後、実行準備時にスナップショット化する。通常の依存関係の循環検査・atomic claim・retry・thread再利用を変更しない。成功前の手動Run Anywayでも自動スナップショットは作らない。参照先欠損は意味的な失敗として扱い、再試行や下流解放をしない。

## 実体と保持期間

| 種別 | 実体 | worktree変更・削除後 |
| --- | --- | --- |
| Worktree file | 対象Task自身のworktree | 変更を反映。削除後は410で取得不可 |
| Saved input | `$CODEX_GUI_HOME/attachments/<server-ID>.bin` | 不変入力として保持し、タスクに紐付いたAPIから取得可能 |
| Artifact snapshot | `$CODEX_GUI_HOME/artifacts/<SHA-256>.bin` とDBメタデータ | 元worktreeから独立。FilesのArtifact snapshotsから取得・再転送可能 |
| Deferred Artifact reference | DBのartifact_requests | 待機／再起動後も保持。成功後の実行準備時に実体を作成 |

一般入力IDは1つのTaskだけに所有させる。別Taskに渡す場合はArtifact経由で新しい入力IDを発行する。画像IDの既存の共有仕様は維持する。ArtifactはSHA-256によるCASで重複排除し、転送先の入力は別inodeのコピーとする。共有CASパスや転送元worktreeへのアクセスを転送先に要求しない。保存ファイルはread-only、入力はworktree外に置くため、通常の `git add` / commitには混入しない。

Artifact ID、source task、元相対パス、SHA-256、サイズ、登録時刻、取得時のHEAD revisionをSQLiteに保存する。HEADは参考情報であり、未commit内容の正確な識別子はSHA-256。アップロード／CASは一時ファイル・fsync・不変名による公開後にDB登録し、失敗時に自分の一時ファイルを削除する。起動時に未登録の一般アップロード／CASと途中ファイルを回収する。複数Artifactの入力登録はDB transactionで一括確定し、失敗時にステージしたコピーを削除する。自動転送の入力ID、requestとpending_turnの関連付けは一緒にcommitする。プロセス強制終了のタイミングによって未参照の入力行が残ることはあるが、確定済み参照を失わず、二重実行は既存のscheduler claimで防止する。

保存済み入力／スナップショットは履歴・予約・再実行を保つため自動削除しない。下書きから外す操作はサーバ保存物を削除しない。quota到達時はアップロード・新規スナップショットを拒否する。DB・保存ディレクトリをバックアップする際は両方を一緒に保管する。

## Codexへの受け渡し

インストール済み `codex-cli 0.159.2` の `codex app-server generate-json-schema` で確認したUserInputを使用する。一般ファイル用のlocalFile/inputFile型を仮定しない。既存のtext入力にサーバの絶対パス、名前、サイズ、SHA-256、必要な場合はArtifactの転送元情報をJSON manifestとして付加する。ファイル本文は展開しない。画像は従来のlocalImage、exec backendでは従来の--imageのまま。既存threadの会話履歴を再送しない。

確認した公式資料: [Codex App Server](https://learn.chatgpt.com/docs/app-server)。入力の可否は一般的なResponses APIの仕様ではなく、実際のインストール済みapp-server schemaで判断した。

## 上限

環境変数の単位はbytes。ファイル種類／MIMEには依存せず、空の一般ファイルも扱える。

| 設定 | 初期値 |
| --- | --- |
| `CODEX_GUI_FILE_MAX_BYTES` | 100 MiB / 一般入力・Artifact単体 |
| `CODEX_GUI_FILE_MESSAGE_MAX_BYTES` | 400 MiB / メッセージの一般入力合計 |
| `CODEX_GUI_FILE_STORE_MAX_BYTES` | 一般入力とArtifact CASの各保存領域につき2 GiB |
| `CODEX_GUI_FILE_STORE_MAX_COUNT` | 各保存領域につき1000件（CAS側はArtifactメタデータ件数） |
| 一般入力のメッセージ件数 | 16件。deferred参照との合計も16件 |
| 既存画像 | 8枚、10 MiB / 枚、40 MiB / メッセージ |
| 単体成果物取得 / ZIPの非圧縮合計 | 512 MiB |
| ZIP選択数 | 100ファイル、重複不可 |
| プレビュー | 256 KiB。超過分は省略表示 |
| 一覧 | 最大20,000 entry走査・深さ32、1000結果ずつ。上限到達時はパスを絞って検索 |

同時アップロードは直列化し、DBへの入力登録時も件数・総容量をatomicに再検査する。中断したストリームや超過したアップロードは一時ファイルを残さない。転送準備やZIPには一時ディスク容量も必要。ブラウザのダウンロード処理は失敗を画面に表示するためBlobを使用し、選択した容量に応じたメモリを使用する。

## API

既存の `/api/attachments`、task作成、messages、scheduledの仕様を維持し、`attachment_ids`に一般入力のサーバ発行IDも受け付ける。New Taskに任意の `artifact_refs: [{source_task_id, path}]` を追加した。

| 操作 | API |
| --- | --- |
| 上限 | `GET /api/uploads/limits` |
| 一般ファイルupload | `POST /api/uploads?filename=...` raw bytes |
| 保存済み入力の関連付け | `POST /api/tasks/{id}/inputs` `{attachment_ids: [...]}` |
| 入力一覧・取得 | `GET /api/tasks/{id}/inputs[/{input_id}]` |
| worktree一覧 | `GET /api/tasks/{id}/files?path=&q=&offset=` |
| 単体取得 | `GET /api/tasks/{id}/files/download?path=` |
| プレビュー | `GET /api/tasks/{id}/files/preview?path=` |
| ZIP取得 | `POST /api/tasks/{id}/files/zip` `{paths: [...]}` |
| 候補・保存snapshot | `GET /api/tasks/{id}/artifacts?path=&offset=` |
| 明示選択のsnapshot登録 | `POST /api/tasks/{id}/artifacts` `{paths: [...]}` |
| snapshot取得 | `GET /api/tasks/{id}/artifacts/{artifact_id}/download` |
| snapshotを入力に変換 | `POST /api/artifacts/inputs` `{artifact_ids: [...], task_id?: ...}` |

`task_id`を指定しないsnapshot入力はNew Task向けの未所有uploadとなり、task作成時に所有権を確定する。所有済み一般入力のIDを別Taskへ流用すると拒否する。移行は追加テーブル・追加列のみで既存画像行はkind=imageとして保持し、繰り返し適用できる。

待機中の指示へ渡す `POST /api/tasks/{id}/inputs` では `pending_only: true` を指定する。開始と競合してpending turnへ関連付けできなかった場合は409を返し、追加指示からの再操作を求める。省略時の保存済み入力の関連付けAPIは互換維持する。

## 安全性と対応範囲

成果物はDBのTaskパスとサーバ計算のworktreeパスの一致を検査し、trustedなworktree rootからdir_fd / O_NOFOLLOWで各要素を開く。検証後に文字列パスから開き直さず、同じFDで配信する。symlink・hardlink・FIFO等は取得／ZIP／snapshotすべてで拒否する。snapshotとZIPではサイズ・inode・mtime/ctimeを前後比較し、snapshotでは元パスの置換も検査する。

絶対パス、`..`、dotfile/dotdirectory、既知の認証／秘密鍵ファイル名は取得・転送対象外。node_modules、venv、__pycache__は一覧走査を省略する。秘密情報の内容を網羅的に検出するDLP機能ではないため、通常名のファイルに含まれる秘密情報は利用者が選択前に確認する。フォルダ全体の暗黙的な転送は行わない。

新APIはcross-siteと異なるOriginからの要求を拒否する。配信はattachment / nosniff / no-store / CSP sandboxとし、元のGUIの認証・bind・ネットワーク公開設定は変更しない。ローカル単一利用者、1 DBにつき1 GUIプロセスという既存運用を前提とする。

この実装はLinuxのopenat/O_NOFOLLOWと `/proc/self/fd` を使用し、実測もLinux/Chromium。Safari・Firefox・実機スマートフォン・Windowsネイティブサーバは未検証。スマートフォンについては390×844のChromium viewportで実モデルと模擬モデルの両方を検証した。

## 検証

[実装時の機械可読の結果](verification.json)、[commit前の検証・統合メモ](commit-review.md)、[今回の検証記録](commit-validation.json)。画面画像は利用量・thread ID等の一時情報を含むためcommit対象外とした。すべて一時DB・リポジトリ・worktree・CODEX_HOME、localhostランダムポートを使用。本番GUI・本番DBは変更せず、mainへのmerge・push・本番再起動はしていない。

- `pytest tests/test_files.py tests/test_artifacts.py`：入力、所有権、SHA-256、ZIP一致、再起動、改ざん／欠損、Path Traversal、symlink/hardlink/FIFO、FD置換、snapshot中の変更、quota、中断cleanup、依存成功、二重claim、競合、元worktree削除後の利用。
- `pytest tests/test_files_codex.py`：実app-server・workspace sandbox・ローカル模擬Responsesで、ファイルの実読み取りと既存thread継続を検証。外部モデル／API課金は使用しない。
- `CODEX_GUI_BROWSER=1 pytest tests/test_ui_files_browser.py tests/test_ui_attachments_browser.py tests/test_ui_mobile_browser.py`：PC／モバイルUI、従来画像操作、添付失敗と再試行、プレビュー、DL失敗、単体／ZIP取得、A→新規B、既存Bへの追加指示。
- `CODEX_GUI_REAL_FILES=1 pytest tests/test_real_files.py`：隔離したsubscription実モデルでupload読込→成果物作成→成功後の自動依存転送→元worktree削除→同一threadの再読込。
- `CODEX_GUI_BROWSER=1 CODEX_GUI_REAL_FILES_BROWSER=1 pytest tests/test_ui_files_browser.py::test_real_browser_upload_model_read_transfer_and_download`：PC／モバイルの実ブラウザ操作→subscription実モデル→Aから新規B→DL結果のSHA-256一致。

実モデルの試験はopt-in。認証ファイルだけを一時CODEX_HOMEへコピーし、モデルメタデータ以外の既存設定・thread・DBを使用しない。試験終了時にコピーした認証を削除する。

開発用の外側sandboxでは `/tmp/codex-daemon-1000` がmode 000に隔離され、実CLIのtool実行が `app-server socket directory must be a user-owned directory with mode 0700` で失敗した。通常の外側sandboxを外した検証環境で再実行し、子Codexのworkspace sandboxは維持して通過した。製品コードにはsandboxのバイパスやsocket設定変更を追加していない。
