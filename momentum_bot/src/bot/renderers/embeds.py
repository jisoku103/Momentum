"""Discord Embed and message content formatters for Momentum Bot.

Complies with Chapter 8 of the Momentum specification (v17).
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional
import discord

from src.core.time_utils import (
    format_jst_display,
    now_utc,
    parse_utc_iso,
    to_jst,
)
from src.db.repository import TaskRecord


def create_today_embed(
    today_slots: Dict[int, Optional[TaskRecord]],
    today_done_count: int,
    total_done_count: int,
    current_utc: Optional[datetime] = None,
) -> discord.Embed:
    """Generate the #today-focus parent embed (Section 8.1)."""
    ref_utc = current_utc or now_utc()
    occupied_count = sum(1 for t in today_slots.values() if t is not None)

    title = f"🎯 TODAY FOCUS ({occupied_count}/3)       🔥 今日: {today_done_count} ／ 累計: {total_done_count}"
    embed = discord.Embed(
        title=title,
        color=discord.Color.from_rgb(88, 101, 242),  # Blurple
    )

    slot_sections: List[str] = []
    for slot_idx in (1, 2, 3):
        task = today_slots.get(slot_idx)
        if task is None:
            slot_sections.append(f"[スロット{slot_idx}] （空きスロット - #backlog から追加できるよ🌱）")
        else:
            lines = [f"[スロット{slot_idx}]", f"📌 {task.title}"]

            # 期限表示（期限切れ検知）
            if task.due_date:
                is_expired = False
                try:
                    due_dt = parse_utc_iso(task.due_date)
                    is_expired = due_dt < ref_utc
                except Exception:
                    pass

                due_text = format_jst_display(task.due_date, include_weekday=True)
                if is_expired:
                    lines.append(f"❗ 期限: {due_text}（期限切れ）")
                else:
                    lines.append(f"📅 期限: {due_text}")

            # トリガー表示（NULLなら省略）
            if task.if_then_trigger:
                lines.append(f"⚡ トリガー: {task.if_then_trigger}")

            # 最初の2分表示
            if task.micro_step is None:
                lines.append("🌱 最初の2分: 未設定")
            elif task.is_micro_completed == 1:
                lines.append(f"~~🌱 最初の2分: {task.micro_step}~~ ✨ 初手クリア済み！")
            else:
                lines.append(f"🌱 最初の2分: {task.micro_step}")

            slot_sections.append("\n".join(lines))

    embed.description = "\n\n".join(slot_sections)
    return embed


def create_backlog_embed(
    tasks: List[TaskRecord],
    current_page: int,
    total_pages: int,
) -> discord.Embed:
    """Generate the #backlog parent embed (Section 8.2)."""
    embed = discord.Embed(
        title="📦 BACKLOG（控え室）",
        color=discord.Color.from_rgb(87, 242, 135),  # Green
    )

    if not tasks:
        embed.description = "控え室は空っぽです ✨"
        embed.set_footer(text=f"ページ {current_page}/{max(1, total_pages)}")
        return embed

    lines: List[str] = []
    for idx, t in enumerate(tasks, start=1):
        task_line = f"**{idx}.** 📌 {t.title}"
        details: List[str] = []
        if t.due_date:
            due_str = format_jst_display(t.due_date, include_weekday=True)
            details.append(f"📅 期限: {due_str}")
        else:
            details.append("期限なし")

        if t.micro_step and t.is_micro_completed == 1:
            details.append("✨ 初手クリア済")

        if details:
            task_line += f" （{' / '.join(details)}）"
        lines.append(task_line)

    embed.description = "\n\n".join(lines)
    embed.set_footer(text=f"ページ {current_page}/{max(1, total_pages)}")
    return embed


def create_overdue_embed(
    tasks: List[TaskRecord],
    current_page: int,
    total_pages: int,
) -> discord.Embed:
    """Generate the #overdue-tasks parent embed (Section 8.3)."""
    embed = discord.Embed(
        title="❗ OVERDUE TASKS（仕切り直し専用）",
        color=discord.Color.from_rgb(237, 66, 69),  # Danger Red
    )

    if not tasks:
        embed.description = "期限を超過したタスクはありません 🎉"
        embed.set_footer(text=f"ページ {current_page}/{max(1, total_pages)}")
        return embed

    lines: List[str] = []
    for idx, t in enumerate(tasks, start=1):
        task_line = f"**{idx}.** ❗ {t.title}"
        if t.due_date:
            due_str = format_jst_display(t.due_date, include_weekday=True)
            task_line += f" （期限切れ: {due_str}）"
        lines.append(task_line)

    embed.description = "\n\n".join(lines)
    embed.set_footer(text=f"ページ {current_page}/{max(1, total_pages)}")
    return embed


def format_done_log_message(task: TaskRecord) -> str:
    """Format single completion or did entry for #done-log (Section 8.5)."""
    time_str = format_jst_display(task.completed_at or task.created_at, include_weekday=False)
    if task.kind == "did":
        return f"⚡ **アクション記録!** {task.title} （記録: {time_str}）"
    return f"🎉 **達成!** {task.title} （完了: {time_str}）"
