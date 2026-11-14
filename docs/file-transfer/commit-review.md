# FILE-01 / ARTIFACT-01 統合メモ

## 中心要件と実装範囲

Task Aの成果物を選び、**Send to Task** / **Attach from Task** から新規または既存Task Bの入力へ直接登録できる。操作は成果物のサーバ側スナップショット登録と入力ID関連付けで完結し、ブラウザでのダウンロード・再アップロードを転送経路に使用しない。一般ファイルupload/downloadだけで本要件を満たしたとは判定していない。

不変CAS、source task/path/revision/SHA-256のDB記録、転送先の独立した入力コピー、既存threadでの追加指示、成功後の依存転送、再起動・source worktree削除後の参照保持を実装済み。一般ファイルI/Oはその共通基盤である。既存のTask Manager、scheduler、worktree、Codex thread、画像添付を再利用する。

今回のcommit準備では製品コードを変更せず、ブラウザ試験に転送中のHTTP要求検査を追加した。新規Bと既存Bの両方について、snapshot登録・Artifact入力登録APIが使われ、upload、download、入力ファイル取得要求が発生しないことを確認する。転送完了後のファイル内容とthread継続も検査する。

自己更新やRuntime分離の実装は含めていない。

## 検証の区別

実行コマンド・ソースハッシュ・結果は [commit-validation.json](commit-validation.json) に保存した。すべて一時DB・Gitリポジトリ・worktreeとlocalhostランダムポートで実行し、本番タスク状態を使っていない。

| 層 | 今回の実行 | 確認できたこと |
| --- | --- | --- |
| mock Codex + 実HTTP/SQLite/Git/filesystem | 253 passed | A→B単体・複数転送、新規B・既存B、CASとSHA-256、source削除後・再起動後、依存成功、BLOCKEDからの手動転送、不正参照・パス・symlink、容量・衝突・中断cleanup、画像・scheduler・recovery・DB回帰 |
| 実CLI + ローカル模擬Responses | 1 passed | 実app-serverとworkspace sandboxのtoolで一般入力と転送Artifactを読み、A削除後もBの同一threadで利用。実モデルには接続しない |
| 実Chromium + mock Codex subprocess | PC/モバイルの2 passed | ブラウザ操作だけのA→新規B・既存Bへの直接添付。転送中のdownload/re-upload禁止をHTTP要求で検証し、出力内容も一致 |
| subscription実モデル | 今回は再実行していない | 前回の同一製品コードで依存転送・再読込1件、実モデルPC/モバイルブラウザ2件が通過。SHA-256一致の記録は [verification.json](verification.json) と real_model_*.txt に保持 |
| 全体回帰 | 今回は全件を再実行していない | 前回1041 passed / 40 skipped。今回の製品コードはその検証時とハッシュ一致し、今回の関連253件と強化したE2Eも通過 |
| 未実施 | 実機スマートフォン、Safari、Firefox、Windowsネイティブサーバ | モバイル確認は390×844のChromium viewport。これら環境の実証を主張しない |

実CLI試験は、外側の開発sandboxに隠されたsocketディレクトリによる誤失敗を避けるため、その外側で実行した。子Codexのworkspace-write sandboxは維持しており、製品にsandboxバイパスやsocket設定変更は追加していない。

## 制約と不足

Linuxのopenat/O_NOFOLLOWと/proc/self/fd、既存の1 DBにつき1 GUIプロセスという運用が前提。一般名のファイル本文に含まれる秘密情報の網羅的なDLP、保存済み入力・Artifactの自動GCは提供しない。強制終了のタイミングによって未参照の入力行が残る可能性があるが、確定済み参照を維持し、既存scheduler claimで二重実行を防ぐ。これらは [仕様](README.md) にも記載した運用上の制約であり、上記のA→B直接転送が未実装であることを意味しない。

## commit範囲と統合判断

担当worktree: `/home/kanade/.local/share/codex-gui/worktrees/codex-gui/070d92b5`

担当branch: `codex-gui/070d92b5-file-01-codex-gui`

レビュー時のHEADおよびmain: `c70cb894428484423590c045ed08da7e0ec804c6`。製品コード・既存の変更テストは前回の検証ハッシュと一致し、別担当の変更を示す差分は見つかっていない。今回追加したのは転送経路を検査するテストと統合用の文書・検証記録。

commit対象はファイル／Artifact転送のコード、テスト、API・運用仕様と検証ログに限定する。認証ファイル、環境変数ファイル、DBは含まれず、テキストの秘密鍵・tokenパターン検査でも該当は検出されなかった。これはすべての秘密情報の不存在を保証するDLP検査ではない。

以下の任意の画面証跡は一時的な利用量・thread IDを含むため、変更・削除せず未追跡のままcommit対象外に残す。

- `docs/file-transfer/desktop.png`
- `docs/file-transfer/mobile.png`

レビュー時点で、対応範囲内のmain統合を妨げる既知の問題はない。mainはレビュー基点と同じSHAであり、このcommitを作るためのmerge/rebaseは不要。mainへの実際の統合、push、本番再起動は行わない。環境の未検証範囲は上記のとおり。
