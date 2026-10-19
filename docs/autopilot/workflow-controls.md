# 大タスクの設定と操作

`/autopilot` で大タスクを登録し、一覧の名前から `/autopilot/workflows/{id}` を開きます。
詳細URLは再読込み・ブックマークに対応しています。進捗、確認事項、子タスク、固定入力、
計画変更、追加指示と操作履歴を同じ画面で確認できます。

## モデルと推論強度

基本モデルは登録画面で選択または入力できます。空欄の場合はCodex CLIの設定を使用し、
未確定のモデル名を表示上で推測しません。CLIがモデルを報告した後は、詳細に直近の実行モデルを表示します。
推論強度の既定値は `high` です。実装、修正、統合後の実装、追加指示に同じ解決規則を適用します。

優先順位は、担当Markdownの明示指定、nodeの明示指定、COMMON.mdの指定、workflow policyの順です。
モデルと推論強度はそれぞれ解決し、Dry-runと子タスク詳細に設定元を表示します。
対応モデルがCLIから取得できる場合は、未対応の推論強度を黙って引き下げません。

Markdownではfront matterを使用できます。

```yaml
---
model: your-codex-model
reasoning_effort: high
---
```

本文の独立した `Model: your-codex-model`、`Reasoning effort: High`、`モデル:`、`推論強度:`
も認識します。コードブロックや引用中の例は設定にしません。同じ優先度の文書で指定が矛盾した場合は、
Dry-runで修正を求めます。

JSON/YAML manifestでは次のpolicyを使用します。既存のClaudeレビュー用 `claude_model` と
Codexレビュー用 `codex_model` は、実装用の基本モデルとは別の指定です。

```yaml
policy:
  implementation_model: your-codex-model
  reasoning_effort: high
  mentor_enabled: true
  mentor_max_rounds: 2
```

## 確認事項とメンター

Needs Reviewは、現在の候補に残る指摘、未確認の範囲、人に必要な判断を1つの文章にまとめます。
解決済み候補の古い指摘は再掲しません。対象ファイルや判断操作へのリンクを残し、JSON・検証ログ・
レビュー履歴は詳細から確認できます。文章の集約自体には追加のAI呼び出しを使いません。

独立レビューは、修正可能な技術的問題について具体的な修正と検証方法を示します。
修正後にも指摘が残る場合、既定で独立したメンター相談を挟み、元の実装スレッドへ修正を戻します。
Claude Codeを優先し、既存の明確な利用枠上限判定に限って独立Codexへ切り替えます。
メンターは既定最大2回で、既存の修正回数・進捗停止・認証・利用枠・停止制約も適用されます。
メンターの助言を受入証拠にせず、修正後は改めてvalidationと独立reviewを実行します。
仕様不足、権限、人による承認が必要な項目は確認待ちを維持します。

## Repositoryと追加作業

Repositoryは通常のNew Taskと同じ最近使用したリポジトリ候補を使用します。

追加指示で子タスクを選ぶと、対象の既存Codexスレッドを継続します。受入済みの子タスクも、
その結果を後続がまだ消費していなければ追加指示を送れます。以前の受入を新しい作業へ使い回しません。
対象を選ばない入力は、大タスクのメモとして履歴に保存します。

追加タスクは担当path、依存関係、必須validationを明示して登録します。既存の固定入力・候補・
受入済み成果を変更せず、追加後の全体DAGに循環、担当範囲の競合、無効な依存がないことを確認します。
実行中の大タスクでは既存スケジューラが処理し、完了済みの大タスクに追加した場合はPausedとなり、
Resumeで開始します。main公開を含む計画への追加は、新しい最終統合計画の確認が必要です。

追加操作は計画digestとrequest keyで保護し、古い画面からの変更と二重送信を拒否・抑止します。
追加指示の履歴と実行予約は同じtransactionで保存し、保存後にdispatchします。

## 検証環境の要件

通常の単体/APIテストは `python -m pytest -q`、画面テストは `CODEX_GUI_BROWSER=1` で有効化します。
実際のmanaged validationにはbubblewrapとLinux名前空間の作成権限が必要です。
GitHub Actionsは一時Ubuntu runner上で回帰とChromiumテストを実行します。
実Codex/Claudeアカウントを使うテストは引き続き個別opt-inです。
