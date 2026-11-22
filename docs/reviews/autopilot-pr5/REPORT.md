PR #5 独立レビュー結果（2026-10-15）

**コード判定：CHANGES_REQUIRED。main統合前にP1の2件を修正する必要がある。**
レビュー成果物を専用branchへcommit・通常pushし、[レビュー用Draft PR #6](https://github.com/Kanade147359/codex-gui/pull/6)として提出した。

このレビュー作業は実装者とは別のGUIタスク `b910d878` の専用worktree・既存の新規Reviewerスレッドで完了した。他AIは起動していない。提出状態とレビュー用Draft PRは [RESULTS.json](RESULTS.json) の `review_submission` を参照。本レビューのSUCCESSは「レビュー提出完了」であり、元タスク、PR #5、稼働反映の承認ではない。

対象は [PR #5](https://github.com/Kanade147359/codex-gui/pull/5)。baseを `12dabe5385f130383b00cd2609dd6bff7360d68f`、提出HEADを `d395d232807203813b5052ae0df9ab926690d180`、提出者の検証コードを `a5a9daad8dc4c99b00a6655e68044fdd52cba6bc` に固定した。開始時と追試後のGitHub読取結果は同じbase・HEADで、open/Draft、GitHub reviewは0件だった。別SHAへの対象変更はしていない。

検証コードSHAから提出HEADまでの差分は文書7ファイルだけで、コード・テスト変更はない。内訳は `docs/autopilot/README.md`、`document-workflow-e2e.json`、`document-workflow-test-results.txt`、`document-workflow-verification.json`、`document-workflow-verification.md`、`document-workflows.md`、`provider-compatibility.md`。本Reviewerは提出HEADをarchiveして実行した。試験後、HEAD側159ファイル・base側139ファイルをGit blob SHAと照合し、不一致は0だった。

**Needs Reviewの実際の理由：GUIの自動完了承認が無効。**

既存GUIの `http://127.0.0.1:8765` にGETのみでアクセスした。元タスク `dd40fc8f` はrepository `/home/kanade/workspace/codex-gui`、worktree末尾 `/dd40fc8f` と一致する。GUIに残るbranch名は `codex-gui/dd40fc8f-implementation-md-codex-gui-au` で、PRのbranch `codex-gui/dd40fc8f-autopilot-documents` と異なる。しかし同タスクのGit読取APIの先頭ログは提出HEAD `d395d23`、次が `a5a9daa` であり、保存された最終semantic resultにもPR #5のURLがある。branchの接頭辞だけで同一タスクと推定したのではない。

2026-10-15 03:59 UTC台の読取結果は次のとおり。APIの保存値と確認時刻はRESULTS.jsonにも収録した。

| 項目 | 確認値 |
| --- | --- |
| execution_status | completed |
| task_outcome | needs_review |
| outcome_reason | `Completion checks passed; automatic completion approval is disabled.` |
| outcome_source | gate |
| semantic_status / semantic result | SUCCESS / SUCCESS |
| evidence_result | PASS |
| completion_checks | 正常終了pass、validationは未設定のためskipped、structured semantic resultはpass |
| completion_contract | required_paths/required_changed_paths/validation_commandsは空、require_any_change/require_commit/manual_approvalはfalse |
| GUI完了承認設定 | `auto_approve_verified_success=false` |
| Autopilot登録 | `/api/autopilot/tasks/dd40fc8f` は `autopilot=false`。workflow一覧も空 |

最終応答SUCCESSは実装セッションの完了申告である。GUIのevidence PASSは設定済みの完了検査が通った意味で、今回はvalidation commandが設定されていない。これだけで全テスト成功や人間の採否判断を意味しない。contractのmanual_approval=falseでもGUI全体の自動承認無効設定が残るため、task_outcomeはneeds_reviewになる。Autopilot reviewは別の登録・検証・独立AIレビュー経路であり、この元タスクには適用されていない。GitHub PR reviewも別で、確認時点では未提出。本Reviewerも元PRへのApprove/Request changesを送信していない。

稼働反映はさらに別の操作である。GETで取得した `static/autopilot.js` のSHA256は `d695160cd197183af0cf2d8833ee6f68ed8f244e34e657c608c4c5158ce82ad6` で、baseの同ファイルに一致し、提出HEADには一致しない。**この静的ファイルだけでは、稼働Pythonプロセス全体のcommitは特定できないためUNKNOWN**とした。元実装記録は未deployだが、それを稼働版の独立確認の代わりにはしていない。サービス、設定、承認状態には書き込んでいない。

**R5-01 / P1：00の計画revisionが担当指示書をowner承認なしで削除できる。**

対象：[planning.py:39](https://github.com/Kanade147359/codex-gui/blob/d395d232807203813b5052ae0df9ab926690d180/app/autopilot/planning.py#L39)、同50–60行、[inputs.py:54](https://github.com/Kanade147359/codex-gui/blob/d395d232807203813b5052ae0df9ab926690d180/app/autopilot/inputs.py#L54)。

`input_files` はモデル生成revisionで変更可能だが、owner承認が必要なexpansionsの判定は編集paths・validation profile・manual_gates/contractの変更だけである。`input_files=[]` で元の担当文書を外してもreadyになる。requirementsの組立ては更新後のinput_filesとCOMMONだけを列挙するため、元の担当文書は実装者にもReviewerにも渡らなくなる。manifest内に文書本文が残っていても、当該タスクの入力bundleからは消える。

最小再現は同梱の `test_planner_cannot_remove_owner_assigned_document`。公開Markdown fixtureを通常APIで登録し、00のfake実装の出力だけに `A.input_files=[]` を加える。実際にcommit・検証・fixture独立レビューを通った00のartifactに対して、実controllerのpropose/applyを実行する。`expansions=[]` のままready→appliedとなり、owner decisionなしでworkflowがcompleted、Aがacceptedになる。Aの実candidateに保存されたrequirementsにはCOMMONと00のhandoffはあるが `documents/A.md` はない。安全性assertionはこの欠落により失敗した。実モデルがこの不正計画を承認することまで実証したという主張ではなく、controllerの許可範囲検査と後続入力の保証が欠けている再現である。

修正案：元の担当文書参照を自動revisionでは削除・置換できないよう保持する。文書の変更を許す設計なら、具体化の許可範囲外としてexact revisionに対するowner承認を必須にし、元要求の失効を黙認しない。正常な具体化と悪意ある削除の両方について、実装者・Reviewerに届く文書の実体を回帰検証する。

**R5-02 / P1：登録runtimeの子孫にある秘密情報・GUI stateがsandboxへ入る。**

対象：[environment.py:84](https://github.com/Kanade147359/codex-gui/blob/d395d232807203813b5052ae0df9ab926690d180/app/autopilot/environment.py#L84)、同50行。

normalizeはmount元自身のpath構成要素だけで `.aws` や `codex-gui` を拒否する。子孫走査は4種類のファイル名、symlink、socketだけを検査し、子孫ディレクトリの機微名、`.npmrc`、`.env`、GUI stateなどを検査しない。その後ディレクトリ全体をro-bindするため、候補コードの実行範囲がこれらへ広がる。read-only mountでも読み取り・ログへの転記は防げない。

最小再現は同梱の `test_registered_runtime_cannot_expose_nested_credentials`。一時runtimeに `.aws/credentials`、`.npmrc`、`.env`、`codex-gui/state.sqlite` を作り、全ファイルに公開のsynthetic markerだけを書く。通常のprofile、preflight、実bwrap Validatorを通し、`/opt/validation/cache` 配下から4ファイルすべてを読めることをassertするcommandがexit 0、ValidatorがPASSになった。本番の秘密情報やDBは一切使用していない。漏出を拒否すべき安全性assertionが失敗した。

修正案：mount元の祖先だけでなく、子孫の機微ディレクトリ・資格情報・GUI state・特殊ファイルも拒否する。ディレクトリ名や深さだけを「narrow」の根拠にせず、明示的に許可したruntime内容のprivate stagingなどで候補から読める内容を限定する。この4ケースの拒否回帰と正規toolchainの成功を両方残す。

**指定観点の確認結果。**

| 観点 | 独立確認と限界 |
| --- | --- |
| Markdown/ZIP安全性 | safe_path、ZIPをdiskへ展開しない実装、symlink/特殊entry・重複・暗号化の拒否を確認。200件、256 KiB/文書、4 MiB合計、展開率100倍の上限。取込安全性試験を再実行して成功 |
| COMMON・担当文書・規約・前段成果物 | 正常系E2Eで実装・レビューの受信bundleを検査。base固定AGENTS/CLAUDEとaccepted SHAからのregular UTF-8 handoff、改竄拒否も成功。ただし担当文書の維持はR5-01で破れる |
| 要求/candidate/plan変更と旧承認 | input/spec/policy/contract digest、head/tree、execution_key、owner target/epoch/run_generationを確認。stale head/contract/policy、owner instruction、新candidate、stale planの拒否試験は成功。版固定があっても未承認の文書除外を許すR5-01は解消しない |
| 分割レビュー・限定snapshot read | 全diff partition＋overallが必須。partitionのCHANGES_REQUESTEDをoverall APPROVEで消せない。tracked snapshot限定、symlink/traversal・行数/byte/round上限を確認。大きなUTF-8 diffのbyte保全、requested_reads、範囲外read拒否試験が成功 |
| merge-then-implement | 競合なしのI1でもwiring.pyを追加する実装turnとstructured resultが必須。競合解消後の別実装turn、merge後crash/reconcileの回帰も成功。機械mergeだけのsuccessへ置換されない |
| 00の権限範囲 | graph/permissions/mode変更を拒否、paths/profile/manual contractの拡張はowner待ち。既存試験は成功。ただし担当文書除外が拡張判定から漏れるR5-01 |
| owner承認・Stop/再起動/重複 | transaction、target digest、decision_epoch、candidate acceptanceとdesign approvalの分離を確認。実SQLite再open、duplicate approve/resume、2度目のhuman question、Stop後拒否、atomic claim・不明process再実行防止の試験が成功。全ての外部processのexactly-onceを証明したわけではない |
| toolchain/cache隔離 | 正規Rust/Node/Python offline build、source書込不可、scratch出力の継続、scratch symlink拒否は成功。ただしmount内容の機微情報除外にR5-02 |
| accepted SHAと後続入力 | `_inputs` がaccepted candidateのheadを採用し、integrationで各headのancestor確認、handoffはそのheadから取得。縦断試験でDのbaseがI1の実装後accepted HEADに一致することを再確認 |

**追試結果。全体PASSとは判定していない。**

提出者の `document-workflow-verification.json` と関連記録を読み、別途作成した一時venv・archive・DB・Git repo/worktreeで実行した。Python 3.14.4、Codex CLI 0.159.2、Claude Code 2.1.185、Rust 1.98.1、Node 20.17.0。依存package版、実行時間、失敗assertion、log/JUnit SHA256、compactな今回のE2E candidate・SHA・handoff evidenceはRESULTS.json、要約は [TEST-RESULTS.txt](TEST-RESULTS.txt) に保存した。生ログは `/tmp/pr5-independent-review/` にあり、一時領域なので永続性は保証しない。

| 実行 | 対象 | 実測結果 |
| --- | --- | --- |
| pytest全体 | 提出HEAD | 799 passed / 29 skipped / **3 failed** |
| Autopilot回帰＋installed CLI local mock＋toolchain smoke | 提出HEAD | **69 passed**。提出者の71件からauth metadata smoke 2件を除外して追試した |
| Chromium Markdown upload/Autopilot＋320 px | 提出HEAD | **4 passed / 9 deselected** |
| requirements-dev.txtだけの新規venvでMarkdown縦断 | 提出HEAD | **1 passed** |
| 既存失敗3件の比較 | 未変更base | **3 failed**。提出HEADとtest ID・assertion messageが一致 |
| Reviewer追加の安全性回帰 | 提出HEAD・同梱fixture | **2 failed**。R5-01/R5-02を再現 |
| diff whitespace / JS syntax / Python F401,F821 | 提出HEAD差分・app/autopilot | PASS |

既存3失敗は以下。すべて実CLIとlocal scripted endpointとの互換試験で、実モデル推論ではない。元のassertionを削除したりskipを追加したりしていない。提出スイートの新規失敗は0件だが、それとは独立に、今回追加した安全性回帰2件は新規欠陥を検出した。

1. `tests/test_ctx_e2e.py::test_real_codex_cache_write_tool_calls_and_large_outputs_are_recorded` — HEAD/baseとも `assert (1 >= 1 and 0 == 1)`。
2. `tests/test_ctx_e2e.py::test_real_codex_tool_output_limit_reaches_the_model` — HEAD/baseとも `assert 156 < 156`。
3. `tests/test_ctx_integration.py::test_tool_output_token_limit_only_lowers_the_cap` — HEAD/baseとも `assert 39 < 39`。

MarkdownのAPI縦断では00受理後に同じ一時SQLiteを別GUI instanceで再openし、A/B/C具体化、COMMON不足をAの同じ実装threadで修正、Bのtyped quota fixtureでCodex fixtureへfallback、I1の追加実装、DのSHA/handoffを確認した。Browserでも一括uploadから同じ経路を実行した。fake providerは入出力と状態遷移を検査するもので、実モデルのレビュー品質・実quota不足の証拠ではない。

再現用にはこのrepositoryから提出HEADを新しい一時directoryへ `git archive d395d232807203813b5052ae0df9ab926690d180` で取り出し、新規venvにその `requirements-dev.txt` を入れる。標準の全体試験と回帰commandはRESULTS.jsonに記録した。独立fixtureは **archiveのdirectoryをcwdにし、archiveのpytest.iniを明示して** 次のように実行する（`REVIEW_ARTIFACT` は本branchの同梱ファイルの絶対path）。pytestの設定自動探索がレビューbranchのbase実装をimportしないよう、対象を明示する。

```sh
PYTHONPATH="$PWD:$PWD/tests" /path/to/isolated/venv/bin/python -m pytest -c "$PWD/pytest.ini" -q "$REVIEW_ARTIFACT/reproduce_findings.py"
```

このfixtureのassertionは安全性要件を表す。未修正HEADでは2件失敗するのが再現結果であり、これを成功に見せるためのxfail/skipは付けていない。fixtureのモデル出力は公開の一時スクリプトで、他AIや実モデルは起動しない。

**main統合前、運用受入、所有者判断の残件。**

main統合前には実装者がR5-01/R5-02を修正し、追加した安全性assertionを満たす回帰を対象の新しい固定SHAで再レビューする必要がある。既存の3失敗は別問題として解消、または所有者が根拠を確認して扱いを決める。既存失敗を口実に全体PASSへ読み替えない。本Reviewerは対象production codeを修正していない。

実運用開始前には、実モデルE2Eのレビュー品質・長文/分割/追加read、実認証とsubscriptionの適用条件、両accountの追加従量利用無効、実quota枯渇とfallback/復旧、実案件のtoolchain/cache、必要なhardwareを別の運用受入として確認する。**実モデルE2E・実quota枯渇・追加課金設定確認は今回未実施**。auth metadata smokeも今回のReviewerは再実行しておらず、提出者の主張を追試済みとして採用していない。未確認の費用・認証条件でモデルを呼んでいない。

所有者に残るのはPR #5の採否、元タスクのGUI完了承認、GitHub PR review/main統合、運用受入の実行許可、稼働版を確かめた上での反映・再起動・rollbackの判断である。これらの回答を待たずレビュー成果物を提出する。元タスクのsuccess上書き、contract緩和、手動承認代行、Autopilotへの自己PR承認委任、元PR変更、main merge、deploy、本番DB操作、課金変更、稼働GUI再起動は実施していない。

実装者へ返す短い修正指示：

> R5-01（P1）: planning revisionのinput_files削除・置換を自動適用しないでください。元担当文書を保持し、範囲外の変更にはexact revisionへのowner承認を要求してください。
>
> R5-02（P1）: runtime mount配下の.aws/credentials、.npmrc、.env、codex-gui/state.sqliteがvalidatorから読めます。子孫を含め機微情報/GUI stateを拒否し、正規toolchainの成功も維持してください。
>
> 同梱reproduce_findings.pyの2つの安全性assertionを保持して修正後SHAで再実行し、通常回帰・Markdown縦断も追試してください。本レビューはコード修正・自己承認をしていません。
