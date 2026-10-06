# 【仕様書 v17】Discord モチベーション駆動型 ToDo Bot「Momentum」

---

## 1. 概要・設計思想

- **目的:** タスク管理の義務感・プレッシャーを排し、行動の着手ハードルを極限まで下げて前向きな勢い（モメンタム）を生み出す。
- **自然文入力と解析:** ユーザーが `#task-inbox` に送信した自然文を Gemini API が解析。「極小の初手（最初の2分）・If-Thenトリガー・前向きな言い換え・期限」を自動抽出し、各チャンネルの UI を最新状態に保つ。
- **期限と超過の扱い（Today 集中ポリシー）:**
  - **Backlog（控え室）の期限超過:** 定期監視（既定 60 秒毎）により `#overdue-tasks` へ自動隔離（❗マーク付与）。
  - **Today スロットの期限超過:** 一度「今日やる」と決めたタスクは、日中に期限が切れてもスロットから自動退避させず、Embed 描画時に `❗` 警告を表示してその日の集中を維持。日の境目（04:00 JST）のリセット時に未完了の場合のみ `#overdue-tasks` へ移送。
  - **超過タスクの再設定:** 期限を未来に再設定、または期限をクリア（なし）にした場合、自動的に `#backlog` へ安全に復帰。
- **チャンネル分離:** 視界のノイズを完全に排除するため、役割ごとに 5 つのテキストチャンネルを厳密に分離。
- **安全な操作性:** 直前の操作（最大3回分）を Undo で安全に取り消し可能。完了や削除も確認モーダルを挟まず即時反映し、Undo を安全網とする。

### 1.1 利用前提・環境

- **シングルユーザー・シングルサーバー専用。** マルチユーザー非対応。
- 環境変数 `OWNER_USER_ID` 以外のユーザーによるメッセージ・コマンド・ボタン操作は無視（インタラクションにはエフェメラルで「このBotは個人用です」と返答）。
- `tasks` / `action_logs` に `user_id` カラムは保持しない。
- **タイムゾーン:** JST（Asia/Tokyo）固定。内部保存は UTC ISO 8601（ミリ秒3桁固定: `YYYY-MM-DDTHH:MM:SS.sssZ`）、表示・ユーザー入力・判定はすべて JST。
- **運用日（Business Date）:** 日の境目（`DAY_BOUNDARY_HOUR`、既定 04:00 JST）を基準とし、`運用日 = (now_jst - DAY_BOUNDARY_HOUR 時間).date()`（`YYYY-MM-DD`）と定義する。本書中の「04:00」「08:00」は既定値であり、実際の値は環境変数（11章）に従う。
- **Intents:** `Guilds`, `GuildMessages`, `MessageContent`（特権インテント有効化必須）。
- **Bot 権限:** `View Channel`, `Send Messages`, `Manage Messages`, `Read Message History`, `Embed Links`。スラッシュコマンドはギルドコマンドとして登録するため、Bot 側の追加権限は不要。

---

## 2. システムアーキテクチャ・並行性制御

### 2.1 排他ロックの局所化アーキテクチャ

外部 API（Gemini）呼び出しによる Bot 全体のブロッキングを防ぎ、かつ SQLite のロック競合やスロット一意制約違反を根絶するため、**非同期排他ロック（`asyncio.Lock`）の範囲を「DB トランザクション ＋ UI 再描画キュー」に限定**する。

```text
[自然文メッセージ] ──► (ロック外: 受信チェック 500字 / 即時返信) ──► [直列入力キュー] ──► (ロック外: Gemini API 解析) ──┐
[ボタン・メニュー] ──► (ロック外: defer_update 実行) ─────────────────────────────────────────────────────────────┼─► [プロセス内 asyncio.Lock]
[スラッシュコマンド]─► (ロック外: defer(ephemeral=True) 実行) ──────────────────────────────────────────────────┤         │
[定期監視 / 日次ジョブ]────────────────────────────────────────────────────────────────────────────────────────┘         ▼
                                                                                                        [1. SQLite トランザクション (即時確定)]
                                                                                                        [2. Discord メッセージ更新 (順序保証)]
```

1. **ロック外で実行する処理:**
   - Discord インタラクションの即時応答（3秒タイムアウト回避）:
     - ボタン / セレクトメニュー: 開始直後に `await interaction.response.defer_update()`
     - スラッシュコマンド: 開始直後に `await interaction.response.defer(ephemeral=True)`
   - 受信メッセージの文字数検証（500文字超過の即時拒否）
   - 「⏳ 整理中…」の即時フィードバック返信
   - Gemini API への HTTP リクエスト・リトライ待機（最大約22秒 = 10秒 + 待機2秒 + 10秒）
   - 入力プロンプト用のタスク一覧スナップショット取得（読み取りのみ。実行時にロック配下で再検証する）
2. **`asyncio.Lock` 配下で実行する処理:**
   - SQLite への全トランザクション（空き枠確認 〜 INSERT/UPDATE/DELETE）
   - Discord 固定親メッセージの Embed 再描画（`#today-focus`, `#backlog`, `#overdue-tasks`）
   - `#done-log` への投稿および削除
3. **自然文メッセージの直列処理（入力キュー）:**
   - `#task-inbox` の自然文は、到着順に 1 件ずつ処理する専用キュー（DB ロックとは別）で処理する。
   - 「追加 → すぐ編集・削除」のように連投しても、後続メッセージには最新のタスク一覧が Gemini に渡され、処理順序も入れ替わらない。ボタン・コマンド・定期ジョブは入力キューを待たない。
4. **書き込みトランザクション:** `BEGIN IMMEDIATE` で開始する（読み取りから書き込みへの昇格による `SQLITE_BUSY` を防ぐ）。
5. **Discord 側の通信失敗時の耐障害性（DB優先 ＋ At-Least Once 配信保証）:**
   - Discord 再描画や `#done-log` 投稿が一時的なネットワーク切断・レートリミット等で失敗した場合も、**DB のコミットは絶対に巻き戻さない**。失敗はエラーログに記録して処理を継続する。
   - **親メッセージの自動修復:** 次回の UI 再描画時、またはコマンド `/refresh` 実行時に自己修復される。
   - **`#done-log` の At-Least Once（最低1回到達）配信保証:**
     - 投稿失敗時は DB 上で `done_log_message_id = NULL` のまま保持され、定期監視ジョブ（7.1）および起動時キャッチアップ（7.4）で自動再送される。
     - Discord 投稿成功直後・DB 保存前にプロセスがクラッシュした場合等、極めて稀な障害復旧時には重複投稿（2回送信）が発生しうる。Momentum では **完全な Exactly-Once ではなく At-Least Once を許容し、DB 上のタスク状態（`status='completed'`）を唯一の正（Single Source of Truth）としてログの欠落防止を最優先** とする。

### 2.2 データベース整合性設定

SQLite 接続確立時、直ちに以下の PRAGMA 文を発行する。

```sql
PRAGMA journal_mode = WAL;          -- 読み書きの並行性向上
PRAGMA busy_timeout = 5000;         -- ロック競合時は最大5秒待機
PRAGMA foreign_keys = ON;           -- 外部キー制約を有効化
```

---

## 3. チャンネル構成・権限設計

### 3.1 チャンネル一覧と役割

| チャンネル名 | 役割・表示内容 | ユーザーの操作 | 一般送信権限 |
| --- | --- | --- | --- |
| `#today-focus` | **今日集中するタスク（最大3件）** 固定親メッセージ（Embed）。期限・トリガー・最初の2分・操作ボタンを表示 | 操作ボタン（初手クリア / 完了 / 控え室へ / Undo） | **禁止** |
| `#task-inbox` | **入力・Bot返答専用** 自然文窓口。Bot 整形結果・通知・朝リマインド・自動リセット通知を表示 | 自然文入力 / `/undo` `/reset-day` / 朝リマインドボタン | **許可** |
| `#backlog` | **控え室（期限未到来・期限なしタスク）** 固定親メッセージ（Embed）。期限順ソート。ページネーションと昇格メニュー | ページ送り / セレクトメニューで Today 昇格 | **禁止** |
| `#overdue-tasks` | **期限超過タスク（仕切り直し専用）** 固定親メッセージ（Embed）。超過タスク集約（❗表示）。緊急昇格メニュー | ページ送り / セレクトメニューで Today 緊急昇格 | **禁止** |
| `#done-log` | **実績ログ** 完了タスクおよび Did を1件1メッセージで投稿 | 閲覧のみ | **禁止** |

> **権限設定:** `#task-inbox` 以外の 4 チャンネルは、親メッセージの埋もれを防ぐため `@everyone`（および対象ユーザー）の「メッセージを送信（Send Messages）」権限を明示的に **OFF** に設定する（Bot には `Manage Messages` および `Send Messages` を付与）。

### 3.2 コマンド受付ルール

- `/undo`: `#task-inbox` および `#today-focus`（返答は常に `ephemeral=True` で返し、ダッシュボード画面を汚染させない）
- `/reset-day`: `#task-inbox`
- `/refresh`: 全 5 チャンネルで実行可（`ephemeral=True` で結果報告）
- **自然文の受付対象:** `#task-inbox` 内の `OWNER_USER_ID` の新規メッセージのみ。Bot・他ユーザー・システムメッセージ・メッセージの編集イベント・他チャンネルの投稿は無視する。
- ※対象外チャンネルで実行された場合はエフェメラルで誘導。

---

## 4. Gemini API 連携・整形仕様

### 4.1 呼び出し方針

- モデル: 環境変数 `GEMINI_MODEL`（JSON Mode / `response_mime_type: "application/json"` を指定）。
- **文字数制限（事前チェック）:** メッセージ受信時、直ちに文字数を検証。500 文字を超過している場合はキュー投入・「⏳ 整理中…」返信を行わず、即座に「メッセージが長すぎるよ（500文字以内にしてね。今回は {文字数} 文字）」と返信して中断。
- **即時フィードバック:** 文字数検証通過後、直ちにユーザーのメッセージへ「⏳ 整理中…」と返信する。返信の参照は「参照先が存在しなくても送信できる」設定（discord.py の `fail_if_not_exists=False`）で行い、元メッセージが削除されていても失敗しないようにする。
- **編集反映とフォールバック:**
  - Gemini 処理および DB 反映完了後、Bot が送信した「⏳ 整理中…」の返信メッセージを `edit()` して実行結果を表示する。
  - 待機中にその返信が削除され `discord.NotFound`（404）となった場合は、`#task-inbox` に新規メッセージとして結果を送信する。
  - 予期せぬ例外（ネットワーク遮断、予期しないエラー等）が発生した場合は、返信メッセージを「⚠️ 予期しないエラーが発生したよ。時間をおいてもう一度試してみてね！」に編集し、DB 変更は一切行わない。
- **タイムアウトとリトライ:** 10 秒（失敗時 2 秒待って 1 回リトライ）。HTTP 429・JSON 不正・スキーマ違反もリトライ対象とする。復帰不能時は共通メッセージ「うまく解析できなかったよ。言い方を変えて送ってみてね！」を返信し、DB 変更は行わない。
- **出力の拘束:** `response_mime_type: "application/json"` に加え、4.2 ② の出力フォーマットを `response_schema` として指定し、`intent` / `target_bucket` / `clear_fields` を enum で拘束する。

### 4.2 Gemini プロンプト仕様

#### ① 入力プロンプト構造

```text
現在日時: {YYYY-MM-DD(曜) HH:MM JST} (※日の境目は 04:00 JST)

現在のタスク一覧（照合対象。最大50件）:
T1 [today]   レポートを提出して週末を気持ちよく迎える（〆 10/09 18:00）
T2 [today]   部屋を片付けてスッキリ過ごす
T3 [overdue] ❗ 資格試験の願書を投函する（〆 10/05 23:59）
T4 [backlog] 歯医者の予約を入れて安心する（〆 10/12 23:59）
...

ユーザー入力:
{ユーザーの入力文}
```

- **タスク 0 件時:** `現在のタスク一覧:` の直下に `（現在登録されているタスクはありません）` と出力する。
- **スナップショット行フォーマット:**
  `T{番号} [{status}] {❗ if overdue or (today and past due)}{title}{（〆 MM/DD HH:mm） if due_date}`
- **一覧抽出ロジック（最大50件）:** Today（最大3件） → Overdue（最大10件） → Backlog（期限順に残枠分）。一覧に載らないタスク（Backlog が 37 件を超える場合の期限が遠いもの等）は、完了・編集・削除の対象にできない（4.3 の「特定できない」扱い）。
- **動的マッピング辞書:** 呼び出しごとに Bot 内部で `T番号 -> task_id (UUID)` の辞書を保持する。

#### ② システムプロンプト

```text
あなたは行動科学に基づき「着手ハードルを極限まで下げるToDo管理アシスタント」です。
与えられた現在日時（JST）・現在のタスク一覧・ユーザー入力から、意図を正確に判定し、指定のJSONのみを出力してください。

【意図（intent）の種類】
- add:      これからやる新規タスクの追加
- complete: 現在のタスク一覧に存在するタスクを「終えた・完了した・片付けた」報告
- did:      現在のタスク一覧に存在しない、突発的に行った行動や新しい実績の報告
- edit:     既存タスクの内容変更（期限変更・タイトル修正・初手変更・移動など）
- delete:   既存タスクの中止・削除（「〜はやめた」「〜は消して」）

【重要な判定ルール】
1. 完了報告の照合（complete vs did）と曖昧性の防御:
   - 既存タスクに一意に合致する達成報告は必ず complete とし、target_ref にその T番号 を指定する。
   - 一覧に類似するタスクが複数存在して1件に絞り込めない場合、または確信が持てない場合は、決して推測で1件を選ばず、必ず target_ref を null にする。その際、reply に「どのタスクのことかな？（例: T1: 英語レポート、T2: 経済レポート）」と確認を促すメッセージを出力する。
   - 一覧に存在しない全く新しい実績のみ did とする（target_ref は null）。
2. 新規追加・移動の振分（target_bucket）:
   - 通常時間帯（04:00〜23:59 JST）:
     - 「今日やる」「今日中」「今からやる」「今夜」などと明示されている、または due_date が現在の運用日の終わり（次の 04:00 JST）までに来る（かつ現在より未来の）タスクは "today" とする。
     - 明日以降のタスク、期限の指定がない一般的な思いつき・ストックは "backlog" とする。
   - 深夜帯（00:00〜04:00 JST）:
     - 「今から」「寝る前」など直近で着手する旨が明示されているタスクのみ "today" とする。
     - 「今日」「今日中」「明日」などのタスク、および期限指定のないタスクは、起きた後の新運用日向けであるため "backlog" とする（朝08:00のリマインドでToday候補となる）。
   - 期限が現在より過去のタスクは、「今日やる」等の明示がない限り "backlog" とする（期限超過の振り分けは Bot 側が行う）。
   - add で振り分けが不明な場合は "backlog"、edit で場所を変えない場合は null とする。complete / did / delete は常に null とする。
3. edit / delete の対象特定と曖昧性の防御:
   - 対象を1件に特定できる場合のみ target_ref にその T番号 を指定する。
   - 対象が複数該当しうる場合や特定できない場合は、推測で選ばず target_ref を null にし、reply で候補を挙げて確認を促す。
   - 同一タスクに対する複数の項目変更は 1 つの edit operation にまとめる。
   - edit では、変更する項目のみ値を入れ、変更しない項目は null にする。項目を空にしたい場合（例:「期限はなしで」）のみ clear_fields に項目名を入れる。add / complete / did / delete では clear_fields は空配列にする。
4. 複数操作の分割:
   - 1つの入力に複数の操作が含まれる場合は operations 配列に分割する（最大5件まで）。
5. 雑談・非アクション（is_actionable = false）:
   - 愚痴、挨拶、単なる感想などは is_actionable を false にし、reply に共感と前向きな一言（40字以内）を入れ、operations は空配列にする。

【日時・境界値の解釈ルール（JST厳守）】
- 「日の境目は 04:00 JST」とする。
- 現在時刻が深夜帯（00:00〜04:00 JST）の場合:
  - 「今から」「寝る前」「今夜」: 当日朝 04:00 JST を期限とする。
  - 「今日」「今日中」: カレンダー上の当日（起きた後の日）の 23:59:59 とする。
  - 「明日」: カレンダー上の翌日の 23:59:59 とする。
- 通常時間帯（04:00〜23:59 JST）の場合:
  - 「今日」「今日中」「今夜」: 当日 23:59:59。
  - 「明日」: 翌日 23:59:59。
- due_date の形式: ISO 8601（YYYY-MM-DDTHH:MM:SS+09:00）。指定がなければ null。
  - 日付のみ → その日の 23:59:59。
  - 時刻のみ（「18時まで」）→ 直近未来のその時刻（すでに過ぎていれば翌日）。
  - 曜日のみ（「金曜」）→ 今日を含めて最も近い未来のその曜日（今日が該当曜日で、時刻が未来または時刻指定なしなら今日）。
  - 「来週X曜」→ 次の月曜始まりの週のX曜日。
  - 時間帯のみの語（日付なし）→「朝」09:00、「昼」12:00、「午後」18:00、「夜」21:00 を既定とし、すでに過ぎていれば翌日の同時刻とする。

【フィールド生成規則】
- reply:
  - is_actionable = true の場合:
    - 曖昧な対象特定時: 候補タスクを挙げて確認を促す問いかけ（50字以内）。
    - 正常時: アクションの着手を後押しする前向きで短い声かけ（30字以内）、または null。
  - is_actionable = false の場合: 共感と前向きな一言（40字以内）。
- title（add/edit）: 義務感のある表現（「〜ねばならない」等）を排除し、完了時のメリットや着手しやすい前向きな行動表現に書き換える（40字以内）。
- title（did）: 達成を称える前向きな過去形に書き換える（40字以内）。complete / delete では title は null にする（Bot が既存タスクの title を使う）。
- if_then_trigger（add/edit）: 「いつ・どこで・何をきっかけに着手するか」（add は必須提案、40字以内）。
- micro_step（add/edit）: 最初の2分以内にノーリスクでできる極小の第一歩（add は必須提案、50字以内）。
- did: if_then_trigger / micro_step / due_date はすべて null にする。
- clear_fields: クリアしたい項目名の配列を指定する（"title" はクリア不可。使用条件は重要な判定ルール 3 を参照）。

【出力フォーマット（JSON厳守）】
{
  "is_actionable": boolean,
  "reply": "文字列 または null",
  "operations": [
    {
      "intent": "add" | "complete" | "did" | "edit" | "delete",
      "target_bucket": "today" | "backlog" | null,
      "title": "文字列 または null",
      "if_then_trigger": "文字列 または null",
      "micro_step": "文字列 または null",
      "due_date": "文字列(ISO 8601) または null",
      "target_ref": "T番号 または null",
      "clear_fields": ["if_then_trigger" | "micro_step" | "due_date"]
    }
  ]
}
```

### 4.3 Bot による検証・実行ルール

- **All-or-Nothing（完全アトミック）原則:**
  - 1つの入力に含まれる複数 operations の事前検証を**ロック配下で**行う（Gemini 呼び出し中に状態が変化している可能性があるため）。
  - **検証項目:**
    1. 対象タスクが存在し、現在の `status` が操作に適合する（`complete` / `edit` / `delete` は `today` / `backlog` / `overdue` のタスクのみ）こと。
    2. `complete` / `edit` / `delete` において `target_ref` が一意に特定されていること（`target_ref` が null の場合は対象特定不能・曖昧として検証エラーとする）。
    3. 同一バッチ内で同一タスクに対する競合操作（例: 同一タスクに対する `complete` と `edit` の重複指定など）が存在しないこと。
    4. 日時が正常にパースできること。
    5. 必須値（`add` / `did` の `title` など）が存在すること。
    6. `intent` / `target_bucket` が定義済みの値であること。
  - **いずれか1件でも検証エラーとなった場合は DB 変更を一切行わず全操作を中止**し、失敗した操作とその理由、および「何も変更していないよ」の旨を返信する。
  - **対象特定不能（曖昧）時の案内:** Gemini の `reply` に確認メッセージが含まれている場合はその `reply` を返し、`reply` が空の場合は「どのタスクのことか分からなかったよ。タイトルの一部を入れて送ってみてね」と案内する。
- **連鎖操作の遮断:** `target_ref` に指定できるのは入力プロンプト生成時点で存在したタスクのみ。同一入力内で新規追加（`add`）したタスクを後続 operation で参照することは不可。
- **`due_date` の UTC 正規化:** Python 側でパースし、UTC の `YYYY-MM-DDTHH:MM:SS.sssZ`（ミリ秒3桁固定）に正規化する。パース失敗時は処理を中断しエラーを返信する。
- **バッチ内実行順序:** Today スロットの空き枠を正しく計算するため、同一バッチ内では以下の順で実行する（同一グループ内は operations の記載順）。
  1. **枠を解放する操作:** Today のタスクに対する `complete` / `delete`、`edit` で Today から退避するもの（5.1 の退避先ルールを適用）
  2. **枠に影響しない操作:** 上記以外の `edit`、`did`、Today 外のタスクに対する `complete` / `delete`
  3. **枠を消費する操作:** `add`（Today 配置）、`edit` で Today へ移動するもの
- **各 intent の実行内容:**
  - `add`:
    - `target_bucket = 'today'`（期限が過去でも可）かつ Today に空き枠がある場合: 最小番号の空きスロットへ配置し、`status='today', today_since=現在時刻(UTC)` で登録（期限が過去なら Embed に ❗ が付く）。
    - `target_bucket = 'today'` だが Today が満杯（3件）の場合: 5.1 の退避先ルールで `backlog` または `overdue` として登録し、返信で「Today が満杯だったため控え室（または #overdue-tasks）に追加したよ」と通知。
    - `target_bucket = 'backlog'`（または null）の場合: 期限が過去なら `status='overdue'`、それ以外は `status='backlog'`（いずれも `slot_index=NULL, today_since=NULL`）で登録。
  - `complete`: 対象タスクを `status='completed', completed_at=現在時刻(UTC), slot_index=NULL, today_since=NULL` に更新。`#done-log` へ投稿し、取得した `message.id` を `done_log_message_id` に保存（通信失敗時は NULL のまま保持し 7.1 / 7.4 で再送）。表示する {title} は既存タスクの `title` を用いる（Gemini の出力 title は使用しない）。対象の元 status（today / backlog / overdue）は問わない。
  - `did`: `kind='did', status='completed', completed_at=現在時刻(UTC)` で登録。`#done-log` へ投稿し `message.id` を保存（失敗時は 7.1 / 7.4 で再送）。
  - `edit`: 対象タスクを更新（非 null の項目を上書きし、`clear_fields` の項目を NULL にする。Gemini の再実行はしない）。
    - 期限更新時（対象の現在の `status` による）:
      - `today`: 期限が過去になってもスロットに残す（Today 集中ポリシー。Embed に ❗ を表示）。
      - `backlog`: 変更後の期限が過去なら `status='overdue'` へ移送。
      - `overdue`: 期限が未来または NULL になれば `status='backlog'` へ復帰。期限が過去のままなら `overdue` を維持。
    - バケット移動時（`target_bucket` が非 null）:
      - `'today'`: Today に空き枠があれば最小番号の空きスロットへ配置（`today_since` を設定）。**満杯の場合は移動のみスキップ**し（他の項目の更新は適用）、返信で「Today が満杯だったので移動はしなかったよ」と通知する（検証エラーとは扱わない）。すでに Today にある場合は何もしない。
      - `'backlog'`: Today のタスクなら 5.1 の退避先ルールで退避しスロットを解放する。期限が過去の `overdue` タスクは `overdue` を維持し、返信で「期限が過ぎているので #overdue-tasks に残しているよ。期限を変えると控え室に戻せるよ」と通知する。
  - `delete`: 論理削除（`status='deleted', deleted_at=現在時刻(UTC), slot_index=NULL, today_since=NULL`）。
- **`is_actionable = false` 時の処理:** DB 変更および `action_logs` の記録を一切行わず、Gemini の `reply` のみ返信。
- **`is_actionable = true` だが operations が空** の場合も、`is_actionable = false` と同様に扱う（`reply` が空なら 4.1 の共通メッセージを返信）。
- **Inbox 返信フォーマット:**
  - `is_actionable = true`: 各 operation の実行結果サマリー（箇条書き） ＋ （`reply` がある場合は `\n\n` ＋ `reply`）。
  - `is_actionable = false`: Gemini の `reply` のみ。

### 4.4 入出力例

現在日時: 2026-10-06(火) 21:30 JST、タスク一覧は 4.2 ① のとおり。

| ユーザー入力 | 解釈（operations の要点） |
| --- | --- |
| `金曜の18時までに英語の課題を出さなきゃ。図書館行ったらやる` | `add` / `target_bucket: "backlog"` / `due_date: "2026-10-09T18:00:00+09:00"` / `if_then_trigger: "図書館の席に着いたら"` |
| `レポート出し終わった` | `complete` / `target_ref: "T1"`（`did` ではない） |
| `レポート終わった`（※レポート関連タスクが2件以上ある場合） | `complete` / `target_ref: null` / `reply: "どのレポートのことかな？（T1: 英語レポート、T2: 経済レポート）"`（DB変更なしで確認案内） |
| `机のゴミ片付けた` | `did` / `title: "机の上のゴミを片付けて視界をクリアにした"` / `target_ref: null` |
| `歯医者の予約、期限は来週の水曜にして` | `edit` / `target_ref: "T4"` / `due_date: "2026-10-14T23:59:59+09:00"` |
| `願書の期限、なしにして` | `edit` / `target_ref: "T3"` / `clear_fields: ["due_date"]`（→ `overdue` から `backlog` へ自動復帰） |
| `部屋の片付けはもういいや` | `delete` / `target_ref: "T2"` |
| `買い物行った。あと明日までに洗濯する` | `did` 1件 ＋ `add` 1件（`due_date: "2026-10-07T23:59:59+09:00"`, `target_bucket: "backlog"`） |
| `なんかもう全部めんどくさい` | `is_actionable: false` / `reply: "お疲れさま。まずは深呼吸ひとつからでOKだよ"` |

---

## 5. コア機能・状態遷移仕様

### 5.1 スロット配置ルール（Today）

- スロット番号は `1, 2, 3` のみ。
- **新規配置時:** 最小番号の空きスロットへ割り当て。
- **完了・退避時:** **他のタスクのスロット番号は詰めず、空き状態を維持する**（位置関係の急変による認知的混乱を防止）。
- **`today_since` の整合性:**
  - Today 配置・昇格時: 必ず `today_since = 現在時刻(UTC ISO 8601 ミリ秒)` を設定。
  - Backlog / Overdue への退避・完了・削除時: 必ず `today_since = NULL` にクリア。
- **`is_micro_completed` の扱い:** Today から Backlog や Overdue へ退避された場合も、初手クリアフラグ（`is_micro_completed`）はリセットせず維持する。
- **不変条件:** `status='today'` のタスクは必ず `slot_index` と `today_since` を持ち、それ以外の status では両方 NULL とする（DB の CHECK 制約で保証。9章）。
- **Today からの退避先ルール（共通）:** タスクが Today から外れる場合（`[📦 控え室へ]` ボタン、`edit` による移動、6.3 の押し出し、7.2 / `/reset-day`）、退避先は期限で決める。
  - `due_date` が現在時刻より過去 → `status='overdue'`
  - それ以外（期限が未来または NULL） → `status='backlog'`
  - いずれも `slot_index=NULL, today_since=NULL` とする。`#backlog` に期限切れタスクが入って定期監視（7.1）で即座に再移送される、という往復を防ぐ。

---

## 6. Undo（取り消し）仕様

### 6.1 対象アクションとロールバック挙動

直前の操作ミスを瞬時に救済するため、バッチ単位の一括ロールバックを提供する。

| action_type | 発生トリガー | Undo 時の処理 |
| --- | --- | --- |
| `add` | 自然文追加 | タスクを `deleted` に論理削除（`status='deleted', deleted_at=現在時刻, slot_index=NULL, today_since=NULL`） |
| `complete` | 自然文完了 / ボタン完了 | `#done-log` のメッセージ削除を試行（NotFound 等のエラーは握りつぶして続行）。元ステータスへ復元（元が Today の場合は 6.3 適用、Backlog/Overdue の場合はスロットなしで復元）し、`completed_at=NULL, done_log_message_id=NULL` にクリア |
| `did` | 自然文実績報告 | `#done-log` のメッセージ削除を試行。レコードを `deleted` に論理削除（`status='deleted', deleted_at=現在時刻`） |
| `clear_micro` | ボタン初手クリア | `is_micro_completed` を `0` に戻す |
| `to_backlog` | ボタン退避 | Today へ復帰（6.3 適用） |
| `to_today` | メニュー / リマインド昇格 | 元の場所（Backlog または Overdue）へ戻す（`status` を戻し、`slot_index=NULL, today_since=NULL`） |
| `edit` | 自然文編集 | `previous_state` の値に完全復元（復元先が Today の場合は 6.3 適用） |
| `delete` | 自然文削除 | `deleted_at=NULL` にし、削除前の状態へ完全復元（復元先が Today の場合は 6.3 適用） |
| `reset_day` | 手動 / 04:00 自動リセット | 退避された全タスクを元の Today スロットへ復帰（6.3 適用） |

※定期監視による自動移送（`to_overdue`）は Undo 対象外。

### 6.2 履歴管理・ロールバック順序

- 1 回のユーザー操作または 1 回の日次処理を 1 つの `batch_id`（UUID）として `action_logs` に記録。
- **保持世代数:** 直近 3 バッチ分。新しいバッチを記録するたびに、4 件目以降の古いバッチの `action_logs` 行を削除する。
- **空バッチの禁止:** 実際に変更されたタスクが 0 件の操作（Today が空の状態での日次リセット等）は `batch_id` を発行せず、履歴の世代を消費しない。
- **履歴ゼロ時:** 有効な `batch_id` が存在しない場合はエフェメラルで「取り消せる直前の操作がありません」と返答して終了。
- **ロールバック実行順序:**
  - 04:00 自動リセット / `/reset-day` の Undo: `previous_state` 内の `slot_index` 昇順（1 → 2 → 3）で復元。
  - 通常の複数操作バッチの Undo: **LIFO（実行時と逆順: `action_logs.id DESC`）** で 1 件ずつロールバック。
- ロールバック完了後、対象 `batch_id` のレコードを削除。
- **Undo 完了通知:** 取り消した内容をエフェメラルでユーザーへ報告する（例: 「↩️ 直前の操作を取り消しました: 『{title}』の完了を取り消しました」）。

### 6.3 Today 復帰時のスロット競合解決ルール（ユニーク制約・CHECK 制約保護）

Undo やスナップショット復元によってタスクが Today へ戻る際、**配置先スロットを先に決定してから 1 回の UPDATE で復元**する（`status='today'` かつ `slot_index=NULL` という中間状態は作らない。9章の CHECK 制約に違反するため）。

1. 元の `slot_index`（`previous_state` の値）が空いていれば、そのスロットへ復帰。
2. 元のスロットが埋まっていて他のスロットが空いていれば、最小番号の空きスロットへ復帰。
3. **3枠すべてが埋まっている場合:**
   - 既存タスクのうち `today_since` が最も新しいタスク（同時刻の場合はスロット番号が大きい方）を押し出し対象とする。**同一バッチで先に復元したタスクは押し出し対象から除外する。**
   - もし除外対象外のタスクが存在しない極限ケース（3枠すべてが同バッチで既に復元されたタスクで埋まっている等）が発生した場合は、押し出しを行わず安全に Backlog（期限切れなら Overdue）へ配置する。
   - 押し出し対象を 5.1 の退避先ルール（期限が過去なら `overdue`、それ以外は `backlog`）で退避し、空いたスロットに復帰対象を配置。
   - `#task-inbox` に押し出し通知を投稿（押し出し操作は Undo バッチに記録しない）。
4. 復帰したタスクの `today_since` は復帰時刻（現在時刻 UTC）に更新する。

---

## 7. バックグラウンドジョブ・スケジューラ仕様

### 7.1 期限超過の定期監視 & 未送信リカバリ（`OVERDUE_CHECK_INTERVAL_SEC`、既定 60 秒毎）

1. ロック配下で、`due_date IS NOT NULL AND due_date < 現在時刻(UTC) AND status = 'backlog'` を満たすタスクを抽出。
2. 対象タスクを **`status='overdue'`**（`slot_index=NULL, today_since=NULL` のまま）へ一括更新。この自動移送は Undo 対象外（`action_logs` に記録しない）。
3. 対象があれば `#backlog` および `#overdue-tasks` の Embed を再描画。
4. `#task-inbox` へ集約通知を投稿。
   - 1〜5件: 「❗ 期限を過ぎたタスク『{title1}』…を #overdue-tasks に移動しました」（全件のタイトルを列挙）
   - 6件以上: 「❗ 期限を過ぎたタスク {N}件（『{title1}』『{title2}』他{N-2}件）を #overdue-tasks に移動しました」
5. **Today の期限跨ぎ検知:** `status='today'` で、前回の監視時刻以降に `due_date` を過ぎたタスクがある場合は、`#today-focus` のみ再描画して ❗ 表示を最新化する（移動・通知はしない）。
6. **`#done-log` 未送信リカバリ（At-Least Once）:** `status = 'completed' AND done_log_message_id IS NULL` を満たすタスクおよび Did を抽出し、Discord の `#done-log` へ再投稿を試行する。成功した場合は取得した `message.id` を `tasks.done_log_message_id` に保存する（8.5）。

### 7.2 日境目の自動リセット処理（毎日 04:00 JST）

1. **未完了タスクの仕分け（1バッチとして Undo 可能）:**
   - 単一の `batch_id` を発行し、変更前の状態を `action_logs` に記録（Today が空の場合は `batch_id` を発行せず、Undo 履歴を消費しない）。退避先は 5.1 の退避先ルールに従う。
   - Today 内で `due_date < 現在時刻` のタスク → `status='overdue', slot_index=NULL, today_since=NULL` へ移動。
   - Today 内で期限未到来または期限なしのタスク → `status='backlog', slot_index=NULL, today_since=NULL` へ退避。
2. **物理削除クリーンアップ（Undo 履歴保護）:**
   - 以下の条件を満たすタスクを完全物理削除する:
     ```sql
     DELETE FROM tasks 
     WHERE status = 'deleted' 
       AND deleted_at <= DATETIME('now', '-30 days')
       AND id NOT IN (SELECT task_id FROM action_logs);
     ```
   - **Undo 履歴の保護:** `action_logs`（直近 3 世代の有効な Undo 履歴）から参照されているタスクは、30 日が経過していても物理削除から除外する（Undo 履歴から押し出されて参照がなくなった時点で次の日次ジョブにより完全削除される）。
3. **実行フラグ記録:** `meta` テーブルの `last_daily_job_date` に現在の運用日（`YYYY-MM-DD`）を保存。
4. `#today-focus`, `#backlog`, `#overdue-tasks` を再描画し、`#task-inbox` に「🌅 新しい一日です。スロットをリセットしました！」と投稿。

### 7.3 朝のリマインド（毎日 08:00 JST）

- **推薦候補の選定ルール（最大3件・重複なし）:**
  1. 第1優先: `#overdue-tasks` から期限が古い順（放置期間が長い順）
  2. 第2優先: `#backlog` から直近 48 時間以内に期限が来るタスク（期限が近い順）
  3. 第3優先: 枠が余っていれば、`#backlog` の期限未到来・なしタスク（作成日順）
- **候補数の上限:** 3 件と Today の空き枠数の小さい方。空き枠が 0 件、または候補が 0 件の場合は投稿しない（その場合も `last_morning_brief_date` は更新する）。
- **メッセージ本文:** 「☀️ おはよう！今日の候補はこちら。気になるものをタップで Today に追加できるよ」＋ 候補ごとの `{title}（〆 MM/DD HH:mm）`（期限なしは省略。期限超過は ❗ を付与）。
- **ボタン仕様:**
  - `custom_id`: `btn:brief:promote:{task_id}`
  - ラベル: `⬆️ {title}`（タイトルは最大20文字で切り詰め）。
  - 各行 1 ボタン（最大3行）。
  - **再起動耐性:** Bot 起動時に `btn:brief:promote:` プレフィックスを処理するリスナー（または動的インタラクションハンドラ）を登録。
- **ボタン押下時ハンドリング:**
  1. 直ちに `defer_update()` を実行。
  2. ロック配下で対象タスクを検証: DB 上に存在し、かつ `status IN ('backlog', 'overdue')` であるか。不一致時はエフェメラルで警告しボタンを Disabled 化。
  3. Today が満杯（3件）の場合: エフェメラルで「Today が満杯（3件）です。スロットを空けてから追加してね」と返答。
  4. 空き枠がある場合: Today へ昇格させ（`to_today` として Undo 履歴に記録）、メッセージ内の押されたボタンを Disabled 化（ラベルに `✨ 追加済み` 付与）して更新し、`#today-focus` と元のチャンネル（`#backlog` または `#overdue-tasks`）を再描画。
- **実行フラグ記録:** `meta` テーブルの `last_morning_brief_date` に現在の運用日を保存。

### 7.4 起動時キャッチアップと重複防止

- 定期ループ（7.1）および起動時（`on_ready`）において、`meta` テーブルの日付を基準としてジョブの実行要否を判定する。
  - `last_daily_job_date < 本日運用日` → 7.2 を実行（運用日が切り替わった時点で条件を満たす。複数日停止していた場合も 1 回のみ実行）。
  - `last_morning_brief_date < 本日運用日` かつ `現在時刻（JST の時刻部分）>= 08:00` → 7.3 を実行（深夜 00:00〜04:00 の起動は時刻が 08:00 未満のため、前日分の朝リマインドは投稿されない）。
- **起動時の `#done-log` 未送信リカバリ:** 起動時にもロック配下で 7.1 手順 6 を実行し、停止期間中や障害時に取りこぼした未送信ログを即座に再送する。
- **初回起動時・未登録キーの初期化:** `meta` テーブルに `last_daily_job_date` または `last_morning_brief_date` が存在しない場合、ジョブを実行せず、未登録のキーに対して現在の運用日付を INSERT してスキップ。

---

## 8. Discord UI / コンポーネント詳細設計

- **再起動耐性（全コンポーネント共通）:** すべてのボタン・セレクトメニューは、`custom_id` のプレフィックス（`btn:` / `select:`）で `on_interaction` から振り分けるか、`timeout=None` の Persistent View として起動時に登録する。再起動後も既存の親メッセージ上の操作が有効であること。
- **View インスタンス再生成の原則:** Discord クライアント側の選択状態キャッシュによる誤動作を防ぐため、親メッセージの Embed 再描画時は毎回新しい View インスタンスを生成してメッセージを `edit()` する。
- **共通の押下時処理:** `defer_update()` → ロック配下で対象タスクの存在と `status` を検証 → 不一致ならエフェメラルで通知して該当画面を再描画（DB 変更なし）→ 一致すれば DB 更新・`action_logs` 記録・再描画。

### 8.1 `#today-focus`

固定親メッセージ 1 件を常時更新。

```text
┌───────────────────────────────────────────────────┐
│ 🎯 TODAY FOCUS (2/3)       🔥 今日: 3 ／ 累計: 42 │
├───────────────────────────────────────────────────┤
│ [スロット1]                                        │
│ 📌 レポートを提出して週末を気持ちよく迎える          │
│ 📅 期限: 10/09(金) 18:00                           │
│ ⚡ トリガー: 図書館の席に着いたら                    │
│ 🌱 最初の2分: PCを開いてタイトルと学籍番号を入力     │
│                                                    │
│ [スロット2]                                        │
│ 📌 英語の課題を片付けてスッキリする                 │
│ ❗ 期限: 10/05(月) 23:59（期限切れ）                │
│ ⚡ トリガー: デスクに座ったら                        │
│ ~~🌱 最初の2分: 課題PDFを開くだけ~~ ✨ 初手クリア済み！│
│                                                    │
│ [スロット3] （空きスロット - #backlog から追加できるよ🌱）│
└───────────────────────────────────────────────────┘
Row 1: [🌱 初手クリア] [✅ 完了] [📦 控え室へ]  <-- スロット1用
Row 2: [✨ 初手達成済] [✅ 完了] [📦 控え室へ]  <-- スロット2用 (達成済ボタンはDisabled)
Row 3: [↩️ 直前の操作を取り消す]
```

- **ボタン行の構成:** 使用中のスロットごとに 1 行（初手クリア / 完了 / 控え室へ）を生成し、空きスロットの行は出さない。最終行に Undo ボタンを 1 つ置く（最大 4 行）。
- **`[📦 控え室へ]` の退避先:** 5.1 の退避先ルールに従う（期限が過ぎている ❗ のタスクは `#overdue-tasks` へ移る）。
- **ボタン `custom_id` 体系:**
  - 初手クリア: `btn:today:micro:{task_id}`
  - 完了: `btn:today:done:{task_id}`
  - 控え室へ: `btn:today:backlog:{task_id}`
  - Undo: `btn:today:undo`
- **期限切れ警告表示:**
  - Embed 描画時、`status = 'today'` かつ `due_date < 現在時刻(UTC)` のタスクには動的に `❗` 警告プレフィックスを付与。時間経過による表示更新は 7.1 手順 5 が担う。
- **初手達成時の表示切り替え:**
  - クリア前: `🌱 最初の2分: {micro_step}`
  - クリア後: `~~🌱 最初の2分: {micro_step}~~ ✨ 初手クリア済み！`（ボタンは Disabled 化）。
- **項目が NULL の場合:** `micro_step` が NULL（`edit` でクリアされた場合）は `🌱 最初の2分: 未設定` と表示し、初手クリアボタンを Disabled にする。`if_then_trigger` / `due_date` が NULL の場合は該当行を省略する。
- **統計カウンタ定義:**
  - 今日: `status = 'completed'` かつ `completed_at` が直近の 04:00 JST（運用日の開始）以降のタスクおよび Did の総数（Undo で `deleted` になった Did は、`completed_at` が残っていても数えない）。
  - 累計: `status = 'completed'` の全タスクおよび Did の総数。
- **ボタン操作時の検証:**
  - 直ちに `defer_update()` を実行。
  - 対象タスクが現在も存在し `status = 'today'` であるかを検証。不一致時はエフェメラルで通知し画面を再描画。

### 8.2 `#backlog`（控え室）

- **ソート規則:** 期限が近い順（期限なしは最後、作成日順）。

  ```sql
  ORDER BY (due_date IS NULL) ASC, due_date ASC, created_at ASC
  ```
- **ページネーション:** 1 ページあたり最大 8 件。
  - Row 1: セレクトメニュー（`custom_id: select:backlog:promote`）
    - 表示中ページのタスクを選択肢化（最大8件）。
    - 選択肢 label: タスクの `{title}`（最大100文字）。
    - 選択肢 description: `〆 {MM/DD HH:mm JST}`（期限なしタスクは `期限なし`）。
    - 選択肢 value: `{task_id}`。
  - Row 2: ページ送りボタン
    - `[◀️ 前へ]`（`custom_id: btn:backlog:prev`）
    - `[▶️ 次へ]`（`custom_id: btn:backlog:next`）
- **ページ状態管理:** インメモリ辞書（`channel_id -> current_page`）で管理し、描画のたびに `current_page = min(current_page, max(1, total_pages))` へ補正する（件数が減って現在ページが範囲外になった場合は最終ページへ寄せる）。再起動後は 1 ページ目に戻る。先頭ページでは `[◀️ 前へ]`、最終ページでは `[▶️ 次へ]` を Disabled にする。
- **昇格メニュー選択時:** `defer_update()` → ロック配下で対象が `status='backlog'` か検証（不一致ならエフェメラルで通知して再描画）→ Today が満杯ならエフェメラルで「Today が満杯（3件）です。スロットを空けてから追加してね」と返答し DB 変更なし → 空きがあれば最小番号のスロットへ昇格（`today_since` 設定、`to_today` として Undo 履歴に記録）し、`#today-focus` と `#backlog` を再描画。
- 0 件時は「控え室は空っぽです ✨」と表示し、コンポーネントを非表示化。

### 8.3 `#overdue-tasks`（期限超過タスク）

- Embed カラー: 赤系（Danger: `#ED4245`）。
- ソート規則: `ORDER BY due_date ASC, created_at ASC`（放置期間が長い順）。
- 1 ページあたり最大 8 件。
  - Row 1: セレクトメニュー（`custom_id: select:overdue:promote`）
    - 選択肢 label: タスクの `{title}`（最大100文字）。
    - 選択肢 description: `❗ 期限切れ: {MM/DD HH:mm JST}`。
    - 選択肢 value: `{task_id}`。
  - Row 2: ページ送りボタン（`custom_id: btn:overdue:prev`, `btn:overdue:next`）
- 0 件時は「期限を超過したタスクはありません 🎉」と表示し、コンポーネントを非表示化。
- **緊急昇格メニュー選択時:** 8.2 と同じ手順（検証対象は `status='overdue'`）。昇格後のタスクは期限が過去のまま Today に入り、❗ 付きで表示される。ページ状態管理・ページ送りボタンの Disabled 化は 8.2 に準じる。

### 8.4 親メッセージ復旧と過去ログ掃除

- **対象チャンネル:** 固定親メッセージを管理する `#today-focus`, `#backlog`, `#overdue-tasks` の 3 チャンネルのみに限定。
- **描画基本方針:** 通常時は `bot_messages` に登録されている親メッセージを `edit()` で再描画する。
- **復旧トリガー:**
  - 初回起動時（`bot_messages` にキーが存在しない場合）。
  - 通常描画時にメッセージ取得で `discord.NotFound`（404）を検知した際。
  - コマンド `/refresh` 実行時。
- **掃除手順:**
  1. チャンネル内の過去メッセージ（最大 100 件）を取得。
  2. 14 日以内のメッセージ: 一括削除（`purge` / `delete_messages`）。
  3. 14 日超過メッセージ: 直近最大 5 件のみ個別 `delete()` を実行し、残りは無視。
  4. 新規に親メッセージを 1 件投稿し、`bot_messages` テーブルを更新。
- 掃除・再投稿は `asyncio.Lock` 配下で実行する（描画中の他処理との競合を防ぐ）。`#done-log` は実績の記録そのものなので掃除対象に含めない。

### 8.5 `#done-log`（実績ログ & At-Least Once 配信保証）

- 完了タスクおよび Did を 1 件 1 メッセージで投稿。
- フォーマット:
  - 通常タスク完了: `🎉 **達成!** {title} （完了: {MM/DD HH:mm JST}）`
  - Did 報告: `⚡ **アクション記録!** {title} （記録: {MM/DD HH:mm JST}）`
- 投稿成功時に返ってきた `message.id` を `tasks.done_log_message_id` に保存する。
- **未送信リカバリ仕様と重複許容ポリシー:**
  - Discord 側の API 障害や一時的切断により投稿に失敗した場合は、`done_log_message_id = NULL` のままコミットされる。
  - 定期監視（7.1 手順 6）および起動時キャッチアップ（7.4）において、`status = 'completed' AND done_log_message_id IS NULL` のレコードを抽出し、自動的に `#done-log` へ再送する。投稿に成功した時点で `done_log_message_id` を保存し、二重送信を防ぐ。
  - **At-Least Once 保証:** Discord への投稿成功後、DB への `done_log_message_id` 保存前に Bot プロセスが不測の事態で強制終了した場合、次回起動時に当該ログが再送される。システムは実績ログの欠落を絶対に防ぐことを優先し、この稀なクラッシュリカバリにおける重複を許容する（DB 側の `tasks` レコードは単一のまま正常に保たれる）。

---

## 9. データベース設計（SQLite DDL）

```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,                    -- UUID v4
    kind TEXT NOT NULL DEFAULT 'task',      -- 'task' | 'did'
    title TEXT NOT NULL,
    raw_input TEXT,
    if_then_trigger TEXT,
    micro_step TEXT,
    is_micro_completed INTEGER NOT NULL DEFAULT 0,
    due_date TEXT,                          -- UTC ISO 8601 ミリ秒3桁 (YYYY-MM-DDTHH:MM:SS.sssZ) / NULL
    status TEXT NOT NULL DEFAULT 'backlog', -- 'today' | 'backlog' | 'overdue' | 'completed' | 'deleted'
    slot_index INTEGER,                     -- 1, 2, 3 (status='today' のみ許容)
    today_since TEXT,                       -- UTC ISO 8601 ミリ秒3桁 / NULL
    done_log_message_id TEXT,               -- #done-log のメッセージID / NULL
    created_at TEXT NOT NULL,               -- UTC ISO 8601 ミリ秒3桁
    completed_at TEXT,                      -- UTC ISO 8601 ミリ秒3桁 / NULL
    deleted_at TEXT,                        -- UTC ISO 8601 ミリ秒3桁 / NULL
    CHECK (kind IN ('task', 'did')),
    CHECK (status IN ('today', 'backlog', 'overdue', 'completed', 'deleted')),
    CHECK (is_micro_completed IN (0, 1)),
    -- Today の不変条件: today のときだけ slot_index(1..3) と today_since を持つ
    CHECK (status != 'today' OR (slot_index IS NOT NULL AND slot_index BETWEEN 1 AND 3 AND today_since IS NOT NULL)),
    CHECK (status = 'today' OR (slot_index IS NULL AND today_since IS NULL)),
    -- 状態とタイムスタンプの整合
    CHECK (status != 'completed' OR completed_at IS NOT NULL),
    CHECK ((status = 'deleted' AND deleted_at IS NOT NULL) OR (status != 'deleted' AND deleted_at IS NULL))
);

CREATE UNIQUE INDEX ux_today_slot ON tasks(slot_index) WHERE status = 'today';
CREATE INDEX ix_tasks_status_due ON tasks(status, due_date);
CREATE INDEX ix_tasks_deleted ON tasks(deleted_at) WHERE status = 'deleted';
CREATE INDEX ix_tasks_completed_at ON tasks(completed_at);
-- 未送信の #done-log を高速検出するパーシャルインデックス
CREATE INDEX ix_tasks_done_log_pending ON tasks(status, done_log_message_id) 
    WHERE status = 'completed' AND done_log_message_id IS NULL;

CREATE TABLE action_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    action_type TEXT NOT NULL,              -- 'add','complete','did','clear_micro','to_backlog',
                                            -- 'to_today','reset_day','edit','delete'
    previous_state TEXT,                    -- 操作前 tasks 行全カラムのスナップショット (JSON) / NULL
    created_at TEXT NOT NULL,               -- UTC ISO 8601 ミリ秒3桁
    CHECK (action_type IN ('add', 'complete', 'did', 'clear_micro', 'to_backlog', 'to_today', 'reset_day', 'edit', 'delete')),
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX ix_action_logs_batch ON action_logs(batch_id);

CREATE TABLE bot_messages (
    key TEXT PRIMARY KEY,                   -- 'today_focus' | 'backlog' | 'overdue_tasks'
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL
);

CREATE TABLE meta (
    key TEXT PRIMARY KEY,                   -- 'last_daily_job_date' | 'last_morning_brief_date'
    value TEXT NOT NULL                     -- 運用日付 'YYYY-MM-DD'
);
```

---

## 10. コマンドリファレンス & エラーハンドリング

### 10.1 スラッシュコマンド一覧

すべてのスラッシュコマンドは、呼び出し直後に `await interaction.response.defer(ephemeral=True)` を実行し、処理完了後に `followup.send(...)` でエフェメラル返答を完結させる。

| コマンド | 引数 | 実行チャンネル | 動作内容 |
| --- | --- | --- | --- |
| `/undo` | なし | `#task-inbox` `#today-focus` | 直近のユーザー操作または日次リセットを 1 バッチ分ロールバック（最大3回）。取り消した内容をエフェメラルで報告（履歴ゼロ時は「取り消せる直前の操作がありません」） |
| `/reset-day` | なし | `#task-inbox` | Today の未完了タスクを 5.1 の退避先ルール（期限超過 → overdue、それ以外 → backlog）で仕分けして Today スロットをリセット。Today が空の場合はエフェメラルで「Today はすでに空だよ」と返し、Undo 履歴も作らない。正常時は `#task-inbox` に通常投稿し、実行者にはエフェメラルで完了通知。**`meta.last_daily_job_date` は更新しない**（物理削除クリーンアップも行わない） |
| `/refresh` | なし | 全 5 チャンネル | 固定 3 チャンネルの過去ログ掃除と親メッセージ再描画・復旧を実施し、エフェメラルで「画面を最新の状態に更新したよ ✨」と返答 |

### 10.2 エラーハンドリング基準

1. **他ユーザーの操作:** インタラクションにはエフェメラルで「このBotは個人用です」と返答。自然文は完全無視。
2. **文字数超過（500文字）:** 受信直後に即座に「メッセージが長すぎるよ（500文字以内にしてね）」と返信して処理終了。
3. **Gemini エラー・不正フォーマット:** リトライ失敗時、DB 変更は行わず 4.1 の共通メッセージを返信。予期せぬ例外時はエラー案内メッセージへ編集。
4. **対象特定不能（曖昧性）:** 対象タスクを1件に特定できない場合、DB 変更は行わず Gemini の `reply`（候補提示）または案内メッセージを返信してユーザーに確認を促す。
5. **Today スロット満杯:** セレクトメニューやリマインドボタンによる昇格要求時、Today が満杯であればエフェメラル通知で拒否し DB 変更を行わない。
6. **Discord 側の通信失敗:** DB のコミットを優先し、親メッセージは次回再描画または `/refresh` で修復、未送信 `#done-log` は At-Least Once 保証に基づき定期監視・起動時リカバリで自動再送する。

---

## 11. 環境変数・設定値一覧

| 変数名 | 既定値 | 説明 |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | Discord Bot トークン |
| `GUILD_ID` | — | 稼働対象サーバー（ギルド）ID |
| `OWNER_USER_ID` | — | 操作を許可する唯一の管理者ユーザー ID |
| `CH_TODAY_FOCUS` | — | `#today-focus` チャンネル ID |
| `CH_TASK_INBOX` | — | `#task-inbox` チャンネル ID |
| `CH_BACKLOG` | — | `#backlog` チャンネル ID |
| `CH_OVERDUE_TASKS` | — | `#overdue-tasks` チャンネル ID |
| `CH_DONE_LOG` | — | `#done-log` チャンネル ID |
| `GEMINI_API_KEY` | — | Google Gemini API キー |
| `GEMINI_MODEL` | —（必須） | 使用する Gemini モデル名。提供終了済みのモデル（Gemini 1.5 系など）を避け、起動時点で利用可能なモデルを公式ドキュメントで確認して指定する |
| `DAY_BOUNDARY_HOUR` | `4` | 日の切り替え時刻（JST: 04:00） |
| `MORNING_BRIEF_HOUR` | `8` | 朝のリマインド投稿時刻（JST: 08:00） |
| `OVERDUE_CHECK_INTERVAL_SEC` | `60` | 期限超過の定期監視（7.1）の実行間隔（秒） |
| `DELETED_RETENTION_DAYS` | `30` | 論理削除データの物理完全削除猶予日数 |
| `TZ` | `Asia/Tokyo` | スケジューラ動作用タイムゾーン |

---

## 12. 受け入れテスト仕様（Acceptance Test Cases）

実装完了および動作保証の判定基準として、以下の受け入れテストケース（TC-001 〜 TC-029）を定義する。全テストケースの通過をリリース基準とする。

### 12.1 Today スロット管理・不変条件テスト

| テストID | テスト項目 | 事前状態 | 操作 / トリガー | 期待される結果（DB状態・UI・返答） |
| --- | --- | --- | --- | --- |
| **TC-001** | 空状態からの Today 追加 | Today 0件 | 自然文 `今からレポートやる`（add: today） | `status='today'`, `slot_index=1`, `today_since=現在時刻` で登録。`#today-focus` のスロット1に表示。 |
| **TC-002** | 歯抜けスロットへの最小番号配置 | Today スロット 1, 3 使用中（スロット 2 空き） | 自然文 `今日中に書類出す`（add: today） | 最小空き枠の `slot_index=2` に配置。スロット 1, 3 の位置・`slot_index` は変化しない。 |
| **TC-003** | Today 満杯時の新規追加の退避 | Today 3件（満杯） | 自然文 `今日中に洗濯する`（add: today） | スロットには入らず、期限未到来なら `status='backlog'`（過去なら `overdue`）として登録。返信で「Today が満杯だったため控え室に追加したよ」と通知。 |
| **TC-004** | タスク完了時のスロット詰めなし維持 | Today スロット 1, 2, 3 使用中 | スロット 2 の `[✅ 完了]` ボタン押下 | スロット 2 のタスクが `status='completed', slot_index=NULL, today_since=NULL` に更新。スロット 1 と 3 は詰められずそのまま維持。今日カウンタ +1。 |
| **TC-005** | Today タスクの期限切れ（集中維持） | Today スロット 1 にタスク配置中 | 現在時刻がタスクの `due_date` を超過 | スロットから退避されず、スロット 1 に留まる。Embed 描画時に `❗` 警告プレフィックスが付与される。 |

### 12.2 Undo（取り消し）と競合解決テスト

| テストID | テスト項目 | 事前状態 | 操作 / トリガー | 期待される結果（DB状態・UI・返答） |
| --- | --- | --- | --- | --- |
| **TC-006** | 完了操作の完全ロールバック | スロット 1 のタスクを完了直後 | `/undo` コマンド実行 | 対象タスクが元のスロット 1 に `status='today'` として復元。`#done-log` の達成メッセージが削除される。今日・累計カウンタが -1 される。 |
| **TC-007** | Did 実績報告の取り消し | Did を投稿直後 | `/undo` コマンド実行 | Did レコードが `status='deleted'` に論理削除。`#done-log` のメッセージが削除される。 |
| **TC-008** | Today 満杯時の復帰（押し出し処理） | Today 3件満杯。Undo で別タスクが Today 復帰を要求 | `/undo` コマンド実行 | 既存3件のうち `today_since` が最も新しいタスクが退避先ルール（Backlog/Overdue）で押し出され、復帰タスクがスロットへ配置。Inbox に押し出し通知投稿。 |
| **TC-009** | 同一バッチ連続復元時の押し出し保護 | 日次リセットで3件退避直後。Today が別のタスク2件で埋まっている | `/undo` 実行（3件一括復元） | 1件目・2件目が復元された後、3件目の復帰時に同一バッチで復元済みのタスクは押し出し対象から除外され、既存タスクのみが押し出される。 |
| **TC-010** | Undo 履歴世代管理と空バッチ保護 | 4回連続でタスク操作を実行 | `/undo` を 4 回連続実行 | 直近 3 回分は正常にロールバック成功。4 回目は「取り消せる直前の操作がありません」と返答。空操作（Today空でのリセット等）で履歴が消費されない。 |

### 12.3 Gemini 自然文解析・曖昧性防御テスト

| テストID | テスト項目 | 事前状態 | 操作 / トリガー | 期待される結果（DB状態・UI・返答） |
| --- | --- | --- | --- | --- |
| **TC-011** | 複数操作の All-or-Nothing | タスク一覧に T1 のみ存在 | 自然文 `T1完了。あと存在しないT99の期限を明日にして` | 事前検証で T99 不在を検知。**T1 の完了も含めて全操作が中止（DB変更ゼロ）**。「何も変更していないよ」の旨を返答。 |
| **TC-012** | 曖昧な対象特定の防御（推測禁止） | 「英語レポート」「経済レポート」の 2 件が登録中 | 自然文 `レポート終わった` | Gemini は推測で選ばず `target_ref: null` を出力。Bot は DB 変更を行わず、「どのレポートのことかな？」と確認メッセージを返答。 |
| **TC-013** | 同一タスクに対する競合操作の遮断 | タスク一覧に T1 が存在 | 自然文 `T1を完了して、T1の期限を来週にして` | 同一タスクに対する `complete` と `edit` の競合を事前検証で検知し全中止。DB変更なし。 |
| **TC-014** | 500文字超過の即時遮断 | 任意の初期状態 | 501文字以上のメッセージを `#task-inbox` に投稿 | 「⏳ 整理中…」を出さず、キューにも投入せず、即座に「メッセージが長すぎるよ」と返信して処理終了。Gemini API は呼び出されない。 |
| **TC-015** | 深夜帯（00:00〜04:00）の日時解釈 | 深夜 02:00 JST | 自然文 `今日部屋を片付ける` |起きた後の新運用日向けと判定され、`target_bucket='backlog'` で登録。04:00 の日次リセットで即座に退避されない。 |
| **TC-016** | Gemini タイムアウトとリトライ | 任意の初期状態 | Gemini API 呼び出しが 10 秒タイムアウト | 2 秒待機後に 1 回リトライ。2 回目も失敗した場合は DB 変更を行わず、共通エラーメッセージを返答。 |

### 12.4 定期監視・日次ジョブ・復旧耐性テスト

| テストID | テスト項目 | 事前状態 | 操作 / トリガー | 期待される結果（DB状態・UI・返答） |
| --- | --- | --- | --- | --- |
| **TC-017** | 期限超過の定期自動移送 | Backlog に期限切れタスクが存在 | 60秒定期監視ループ実行 | 対象タスクが `status='overdue'` に自動移送。`#backlog` と `#overdue-tasks` が再描画。Inbox に集約通知が投稿される（Undo対象外）。 |
| **TC-018** | 日境目（04:00 JST）の自動リセット | Today に期限切れタスク1件、未到来タスク1件が存在 | 04:00 JST 到来（または定期監視による検知） | 期限切れタスクは `overdue` へ、未到来タスクは `backlog` へ退避。30日超過の deleted タスクが完全削除。`meta` 更新。Inbox に「新しい一日です」通知。 |
| **TC-019** | 起動時キャッチアップ（停止復帰） | 前日 23:00 に Bot 停止、翌日 05:00 JST に Bot 起動 | Bot 起動（`on_ready`） | `last_daily_job_date < 本日運用日` を検知し、直ちに日次リセット（7.2）が 1 回実行され、最新状態にキャッチアップされる。 |
| **TC-020** | 朝のリマインド（08:00 JST） | Backlog / Overdue にタスク存在、Today 空き2枠 | 08:00 JST 到来（または 08:00 以降の起動） | 優先度順（Overdue → 48h以内Backlog）で最大2件の候補をボタン付きで投稿。ボタン押下で Today へ昇格し、ボタンは `✨ 追加済み`（Disabled）化。 |
| **TC-021** | `#done-log` 未送信リカバリ | 完了時に Discord API 障害で `#done-log` 投稿失敗（`done_log_message_id=NULL`） | 次回の定期監視ループ（60秒後）実行 | 未送信の completed タスクを検出し、`#done-log` へ再送成功。`done_log_message_id` が保存される。 |

### 12.5 Discord UI・コンポーネント・権限管理テスト

| テストID | テスト項目 | 事前状態 | 操作 / トリガー | 期待される結果（DB状態・UI・返答） |
| --- | --- | --- | --- | --- |
| **TC-022** | 親メッセージ喪失からの自動復旧 | 管理者が `#today-focus` の固定親メッセージを手動削除 | `/refresh` 実行、または親メッセージ更新イベント発生 | `discord.NotFound` を検知し、過去ログ掃除（14日以内purge）を実行後、新規に親メッセージを投稿して `bot_messages` テーブルを更新。 |
| **TC-023** | Bot 再起動後のコンポーネント有効性 | 親メッセージが表示されている状態 | Bot プロセスを再起動し、親メッセージのボタンを押下 | Persistent View またはプレフィックスルーティングにより、再起動前のメッセージのボタン・メニューが正常に動作し、DB/画面が更新される。 |
| **TC-024** | 他ユーザーからの操作遮断 | 任意の初期状態 | `OWNER_USER_ID` 以外のユーザーがボタン押下またはスラッシュコマンド実行 | エフェメラルで「このBotは個人用です」と返答され、DB 変更は一切行われない。自然文投稿は完全無視される。 |

### 12.6 並行性・障害リカバリ・境界値テスト（高度な検証）

| テストID | テスト項目 | 事前状態 | 操作 / トリガー | 期待される結果（DB状態・UI・返答） |
| --- | --- | --- | --- | --- |
| **TC-025** | `#done-log` 送信直後クラッシュの At-Least Once 許容 | `#done-log` 投稿成功、message.id 保存前にプロセス強制終了 | Bot 再起動・リカバリジョブ実行 | DB に `done_log_message_id=NULL` が残るためログが再送される（At-Least Once 重複許容）。タスク状態は `completed` のまま一貫性を維持。 |
| **TC-026** | 30日経過タスクの Undo 履歴保護 | `action_logs` に記録された削除済みタスク（30日経過） | 04:00 日次物理削除クリーンアップ実行 | `action_logs` で参照されている間は物理削除されず保護される。`/undo` により正常に復元可能。 |
| **TC-027** | Gemini 解析中の並行 UI 操作 | 自然文 A を受信し Gemini 解析中（ロック外） | ユーザーがスロット 1 の `[✅ 完了]` ボタンを押下 | ロック配下でスロット 1 が完了。その後自然文 A の検証がロック配下で実行され、最新の DB 状態に対して矛盾なく処理される。 |
| **TC-028** | ほぼ同時の複数 Today 昇格 | Today 空き 2 枠。Backlog にタスク A, B が存在 | タスク A と タスク B の Today 昇格をミリ秒差で同時要求 | `asyncio.Lock` により直列化され、空きスロット（例: スロット 1 と 2）へ 1 件ずつ配置。スロット重複や UNIQUE 制約違反が発生しない。 |
| **TC-029** | 日境目（03:59:59 → 04:00:01）の運用日・境界値切り替え | 03:59:59 JST にタスク操作 | 04:00:00 JST を跨いで 04:00:01 JST に次の操作を実行 | 03:59:59 は前日の運用日として扱われ、04:00:00 以降は当日の新運用日へ切り替わる。日次リセットジョブが 1 回のみ正確に実行される。 |

---

## 付録: 改訂履歴

### v13 → v14 の修正一覧

| # | 箇所 | v13 の問題 | v14 での修正 |
| --- | --- | --- | --- |
| 1 | 1.1 / 全体 | `$...$`（LaTeX）表記が Markdown で崩れる。`Use Slash Commands` は Bot 権限として不要 | プレーンテキスト化。権限記述を整理 |
| 2 | 2.1 / 4.1 | Gemini の最大待機が「20秒」だが実際は 10+2+10=22 秒 | 約22秒に修正 |
| 3 | 2.1 | 自然文を並列処理するため、連投時に処理順が入れ替わり、古いタスク一覧で Gemini が判定する | 自然文は入力キューで到着順に直列処理 |
| 4 | 2.1 | 書き込みトランザクションの開始方法が未定義（`SQLITE_BUSY` の恐れ） | `BEGIN IMMEDIATE` を指定。Discord 失敗時の方針を明記 |
| 5 | 3.2 | メッセージ編集イベントの扱いが未定義 | 編集イベントは無視 |
| 6 | 4.1 | 「同メッセージを編集」が曖昧（ユーザーの投稿は編集不可）。元メッセージ削除時の返信が失敗する | Bot の返信を編集と明記。参照先なしでも送信できる設定を指定。エラー文言を統一 |
| 7 | 4.2 | v13 で曜日・「来週X曜」の解釈ルールが消えていた | 復活（例: 「金曜の18時まで」が解釈できる） |
| 8 | 4.2 | `edit` で「null は変更なし」の規則、`did` / `complete` / `delete` の null 規則が欠落 | 追記 |
| 9 | 4.2 | 期限が過去のタスクが `today` に振り分けられ得る。「今日中」「今夜」の扱いが曖昧 | 過去期限は原則 `backlog`、「今日中」「今夜」を明記 |
| 10 | 4.2 / 4.3 | `complete` で Gemini の title と既存 title のどちらを `#done-log` に使うか未定義 | 既存 title を使用 |
| 11 | 4.3 | 検証がロック外の古いスナップショットに依存し、status の整合確認もない | ロック配下で再検証。status 適合も確認 |
| 12 | 4.3 | `edit` で Today のタスクの期限が過去になると `overdue` へ移送（Today 集中ポリシーと矛盾） | Today は残し、❗ 表示のみ |
| 13 | 4.3 | `edit` で Today 移動を指示したとき満杯の場合の挙動が未定義 | 移動のみスキップして通知（他項目は適用） |
| 14 | 4.3 | `add` で「今日やる」かつ期限が過去の場合、指示が無視される | 明示された場合は Today に配置（❗ 付き） |
| 15 | 4.3 / 5.1 | Today から退避した過去期限タスクが `backlog` に入り、7.1 で再移送される往復が発生 | 退避先ルール（過去なら `overdue`）を 5.1 に新設し全箇所に適用 |
| 16 | 4.3 | バッチ内の実行順序で `edit` の枠への影響が未分類 | 解放 / 影響なし / 消費の 3 グループに整理 |
| 17 | 4.3 | `is_actionable: true` で operations が空のときの挙動が未定義 | 非アクション扱い |
| 18 | 4.4 | 入出力例が消えていた | 例を追加（`complete`・`edit`・期限クリア等を含む） |
| 19 | 6.2 | 履歴 3 世代の削除タイミング未定義。0 件の日次リセットが履歴を消費する | 超過分の削除と空バッチ禁止を明記 |
| 20 | 6.3 | 同一バッチで復元済みのタスクが押し出し対象になり得る。復元時の `today_since` が未定義。押し出し先が常に `backlog` | 除外ルール・`today_since` 更新・退避先ルールを追加。中間状態を作らない手順に変更 |
| 21 | 7.1 | 実行間隔が「1〜5分」で曖昧。通知文が「6件」「他4件」と固定値。Today の ❗ が時間経過で更新されない | 環境変数で 60 秒既定。`{N}` に修正。Today の期限跨ぎ検知を追加 |
| 22 | 7.3 | 朝リマインドの本文・空き枠との関係・候補 0 件時のフラグ更新が未定義 | 追記。昇格は `to_today` として Undo 記録 |
| 23 | 7.4 | 日次ジョブの `現在時刻 >= 04:00` 条件が運用日判定と重複し、深夜の再起動時に補完が遅れる | 運用日の比較のみに簡素化 |
| 24 | 8 | 再起動耐性が朝リマインドのみ。昇格メニュー・Overdue のハンドリングが未定義 | 全コンポーネント共通方針と、Backlog / Overdue の昇格手順を追加 |
| 25 | 8.1 | 行構成（空きスロットの行）、NULL 項目の表示、「今日」カウンタ（削除済み Did を含む恐れ）が未定義/不正 | 追記・`status='completed'` 条件を追加 |
| 26 | 8.2 | ページ補正式 `max(1, total_pages)` が常に最終ページへ飛ぶ | `min(current_page, max(1, total_pages))` に修正。端のボタンを Disabled |
| 27 | 8.4 | 掃除とロックの関係が未定義 | ロック配下で実行 |
| 28 | 9 | `status='today'` で `slot_index` が NULL の行を許してしまう（SQLite の CHECK は NULL を通す）。`completed_at` / `deleted_at` の整合なし | CHECK 制約を追加 |
| 29 | 10 | `/reset-day` の Today 空時の挙動、Discord 失敗時の扱いが未定義 | 追記 |
| 30 | 11 | `GEMINI_MODEL` の既定値 `gemini-1.5-flash` は提供終了済みの可能性が高い | 既定値を外し必須化。監視間隔の環境変数を追加 |

### v14 → v15 の修正一覧

| # | 箇所 | v14 の問題・曖昧点 | v15 での修正 |
| --- | --- | --- | --- |
| 1 | 4.1 | 500文字超過チェックの実行タイミングが曖昧で、「⏳ 整理中…」返信後やキュー投入後に判定される恐れがあった | 受信直後に即時検証し、超過時は即座に警告返信してキュー投入前に遮断するフローを明確化 |
| 2 | 4.1 | Gemini 解析中や DB トランザクション処理中に予期せぬ例外が発生した場合、「⏳ 整理中…」のまま放置される | `try...except` で捕捉し「⚠️ 予期しないエラーが発生したよ…」へメッセージ編集する安全網を追加 |
| 3 | 4.2 ② | `is_actionable = true` の場合の `reply` フィールド生成ルールが未定義で、Gemini の出力が不安定になる恐れがあった | フィールド生成規則に「着手を後押しする前向きで短い声かけ（30字以内）または null」と明記 |
| 4 | 4.2 ② | 深夜帯（00:00〜04:00 JST）に「今日」「今日中」と入力した場合、数時間後の 04:00 日次リセットで即座に退避されてしまう | 深夜帯は直近着手（「今から」「寝る前」等）のみ Today とし、「今日」「今日中」は起きた後の新運用日向けとして原則 Backlog へ振り分けるルールを明記 |
| 5 | 4.2 ① | スナップショット各行の厳密な文字列フォーマット定義が欠落していた | 書式 `T{番号} [{status}] {❗}{title}{（〆 MM/DD HH:mm）}` を明記 |
| 6 | 4.3 | All-or-Nothing 検証項目に、同一タスクに対する競合操作（完了と編集の重複等）の排他チェックが明示されていなかった | 検証項目 3 として同一タスクへの競合操作を検知し全中止するルールを追加 |
| 7 | 5.1 | Today から退避されたタスクの初手クリアフラグ（`is_micro_completed`）の扱いが未定義 | 退避時もフラグは維持する旨を明記 |
| 8 | 6.1 | `complete` の Undo 時、復元先が Today 以外（Backlog/Overdue）の場合のスロット扱いが曖昧だった | Today は 6.3 適用、Backlog/Overdue はスロットなしで復元することを明記 |
| 9 | 6.2 | Undo 完了時のユーザーへの返答内容が具体的に規定されていなかった | 取り消したタスク名・操作内容をエフェメラルで明示して通知する仕様を追記 |
| 10 | 6.3 | 3枠すべてが同一バッチで復元されたタスクで埋まっており、押し出し対象が存在しない極限ケースの挙動が未定義 | 押し出しを行わず安全に Backlog（期限切れなら Overdue）へフォールバックする例外安全策を規定 |
| 11 | 7.3 | 朝リマインドボタン押下後のボタン表示変化が未定義だった | 押下済みボタンを Disabled 化し、ラベルに `✨ 追加済み` を付与して更新する仕様を追加 |
| 12 | 7.4 / 9 | `meta` テーブルに片方のキーしか存在しない場合の起動時挙動が未定義 | 各キーを個別に存在チェックし、未登録キーのみ現在の運用日付を INSERT する安全設計に整理 |
| 13 | 8 / 8.2 / 8.3 | Embed 再描画時に Discord のセレクトメニュー選択状態キャッシュが残る問題への対策が未定義 | 再描画時は毎回新しい View インスタンスを生成して `edit()` する原則を明記。セレクトメニューの options（label, description, value）仕様を具体化 |
| 14 | 8.1 | 空きスロットの表示文言が簡素で動機付けが弱かった | `[スロットN] （空きスロット - #backlog から追加できるよ🌱）` とし、行動促進のUIに洗練 |
| 15 | 8.4 | 初回起動時（`bot_messages` にキー未登録）の親メッセージ初期生成トリガーが明記されていなかった | 復旧トリガーの条件に「初回起動時（キー未登録時）」を明記 |
| 16 | 9 | `is_micro_completed` および `action_logs.action_type` に CHECK 制約がなかった。`deleted_at` の CHECK 制約構文を標準論理式へ改善 | `CHECK (is_micro_completed IN (0, 1))`、`action_type` の enum CHECK、および厳密な論理式を追加 |
| 17 | 10.1 | スラッシュコマンド完了後のインタラクション応答フロー（`followup.send`）が曖昧だった | 全コマンドが `defer(ephemeral=True)` から `followup.send` でエフェメラル完結する規約を統一 |

### v15 → v16 の修正一覧

| # | 箇所 | v15 の課題・改善点 | v16 での修正・強化内容 |
| --- | --- | --- | --- |
| 1 | **12 (新設)** | 実装完了・合否判定のための受け入れテスト仕様が体系化されていなかった | **12章「受け入れテスト仕様」を新設**。Todayスロット管理、Undo競合解決、Gemini自然文解析・曖昧性防御、定期監視・日次ジョブ・復旧耐性、Discord UI・権限管理を網羅する 24 件のテストケース（TC-001〜TC-024）を定義 |
| 2 | **2.1 / 7.1 / 7.4 / 8.5 / 9** | Discord 投稿失敗時に `#done-log` が未送信のまま永久に放置され、将来再送する仕組みがなかった | `done_log_message_id IS NULL AND status = 'completed'` のタスクを検出するパーシャルインデックス `ix_tasks_done_log_pending` を DDL に追加。定期監視（7.1 手順6）および起動時キャッチアップ（7.4）で自動再送するリカバリ機構を実装 |
| 3 | **4.2 ② / 4.3 / 4.4 / 10.2** | 類似タスクが複数ある場合など、Gemini が推測で誤ったタスクを選んでしまう「意味的誤判定」の防御フローが弱かった | 類似候補が複数存在する場合や特定に確信が持てない場合は必ず `target_ref: null` とし推測を禁止。Gemini に候補提示確認の `reply` を出力させ、Bot は DB 変更を一切行わず確認を促す曖昧性防御フローを規定 |

### v16 → v17 の修正一覧

| # | 箇所 | v16 の課題・改善点 | v17 での修正・強化内容 |
| --- | --- | --- | --- |
| 1 | **2.1 / 7.1 / 8.5 / 10.2 / TC-025** | `#done-log` 投稿成功直後・DB保存前にクラッシュした場合、二重投稿が発生しうるクラッシュウィンドウが存在した | **At-Least Once（最低1回到達）配信保証** を仕様として明記。障害復旧時の稀な重複投稿を許容し、DB の `tasks` レコードを唯一の正としてログ欠落防止を最優先とする設計を確立（TC-025 追加） |
| 2 | **7.2 / TC-026** | 30日経過した deleted タスクが物理削除される際、直近3世代の `action_logs` に参照されていると Undo 履歴が消失する恐れがあった | 物理削除条件に `id NOT IN (SELECT task_id FROM action_logs)` を追加。有効な Undo 履歴から参照されている間は 30 日を超過しても物理削除から保護するルールを明記（TC-026 追加） |
| 3 | **12.6 (新設)** | 並行処理・障害リカバリ・日境目境界値の受け入れテストケースが不足していた | **12.6節を新設し TC-025 〜 TC-029 の 5 つの高度なテストケースを追加**（計 29 件）。Gemini 解析中の並行 UI 操作（TC-027）、ほぼ同時の複数 Today 昇格（TC-028）、03:59:59 → 04:00:01 の運用日・日次リセット切り替え（TC-029）を網羅 |
