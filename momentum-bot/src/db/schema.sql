-- Momentum SQLite DDL Schema (Specification Chapter 9 Compliant)

CREATE TABLE IF NOT EXISTS tasks (
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

CREATE UNIQUE INDEX IF NOT EXISTS ux_today_slot ON tasks(slot_index) WHERE status = 'today';
CREATE INDEX IF NOT EXISTS ix_tasks_status_due ON tasks(status, due_date);
CREATE INDEX IF NOT EXISTS ix_tasks_deleted ON tasks(deleted_at) WHERE status = 'deleted';
CREATE INDEX IF NOT EXISTS ix_tasks_completed_at ON tasks(completed_at);
-- 未送信の #done-log を高速検出するパーシャルインデックス
CREATE INDEX IF NOT EXISTS ix_tasks_done_log_pending ON tasks(status, done_log_message_id) 
    WHERE status = 'completed' AND done_log_message_id IS NULL;

CREATE TABLE IF NOT EXISTS action_logs (
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

CREATE INDEX IF NOT EXISTS ix_action_logs_batch ON action_logs(batch_id);

CREATE TABLE IF NOT EXISTS bot_messages (
    key TEXT PRIMARY KEY,                   -- 'today_focus' | 'backlog' | 'overdue_tasks'
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,                   -- 'last_daily_job_date' | 'last_morning_brief_date'
    value TEXT NOT NULL                     -- 運用日付 'YYYY-MM-DD'
);
