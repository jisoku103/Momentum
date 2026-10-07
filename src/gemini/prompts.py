"""Prompt builders and system prompt definitions for Gemini API integration.

Complies with Section 4.2 ① and ② of the Momentum specification (v17).
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from src.core.time_utils import (
    TIMEZONE_JST,
    format_jst_display,
    now_jst,
    now_utc,
    parse_utc_iso,
    to_jst,
)
from src.db.repository import TaskRecord, TaskRepository

# Full system prompt conforming to Section 4.2 ②
SYSTEM_PROMPT = """あなたは行動科学に基づき「着手ハードルを極限まで下げるToDo管理アシスタント」です。
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
"""


async def build_task_snapshot(
    task_repo: TaskRepository,
    current_utc: Optional[datetime] = None,
    max_count: int = 50,
) -> Tuple[str, Dict[str, str]]:
    """Generate task snapshot string and dynamic T-number mapping (Section 4.2 ①).

    Extraction priority:
      1. Today tasks (max 3)
      2. Overdue tasks (max 10)
      3. Backlog tasks (remaining up to max_count)

    Returns:
      (snapshot_text, t_map)
      where t_map is {"T1": "task-uuid-1", ...}
    """
    ref_utc = current_utc or now_utc()

    # 1. Today tasks (max 3)
    today_tasks = await task_repo.get_today_tasks()

    # 2. Overdue tasks (max 10)
    overdue_tasks = await task_repo.get_overdue_tasks(limit=10)

    # 3. Backlog tasks (remaining slots)
    remaining_slots = max(0, max_count - len(today_tasks) - len(overdue_tasks))
    backlog_tasks = await task_repo.get_backlog_tasks(limit=remaining_slots)

    all_selected: List[TaskRecord] = today_tasks + overdue_tasks + backlog_tasks

    if not all_selected:
        return "（現在登録されているタスクはありません）", {}

    lines: List[str] = []
    t_map: Dict[str, str] = {}

    for idx, t in enumerate(all_selected, start=1):
        t_ref = f"T{idx}"
        t_map[t_ref] = t.id

        # Determine warning prefix ❗
        # ❗ if overdue or (today and past due)
        has_warning = False
        if t.status == "overdue":
            has_warning = True
        elif t.status == "today" and t.due_date:
            try:
                due_dt = parse_utc_iso(t.due_date)
                if due_dt < ref_utc:
                    has_warning = True
            except Exception:
                pass

        warn_prefix = "❗ " if has_warning else ""

        # Format due date suffix
        due_suffix = ""
        if t.due_date:
            try:
                due_suffix = f"（〆 {format_jst_display(t.due_date, include_weekday=False)}）"
            except Exception:
                pass

        # Format: T{番号} [{status}] {❗}{title}{（〆 MM/DD HH:mm）}
        line = f"{t_ref} [{t.status}] {warn_prefix}{t.title}{due_suffix}"
        lines.append(line)

    return "\n".join(lines), t_map


def build_user_prompt(
    user_input: str,
    snapshot_text: str,
    current_time_jst: Optional[datetime] = None,
) -> str:
    """Format input prompt according to Section 4.2 ①."""
    dt_jst = current_time_jst or now_jst()
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    weekday_str = weekdays_ja[dt_jst.weekday()]
    formatted_now = dt_jst.strftime(f"%Y-%m-%d({weekday_str}) %H:%M JST")

    return f"""現在日時: {formatted_now} (※日の境目は 04:00 JST)

現在のタスク一覧（照合対象。最大50件）:
{snapshot_text}

ユーザー入力:
{user_input}
"""
