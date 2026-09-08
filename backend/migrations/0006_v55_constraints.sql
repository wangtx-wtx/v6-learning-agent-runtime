-- 0006_v55_constraints.sql — V5.5 约束与错题事件（方案 7.4/8.3/10.1）
-- 先做后看字段、错题状态事件表、业务唯一约束。

-- 7.4 先做后看：学生先提交自己的答案，提交后才允许看答案/讲解
ALTER TABLE questions ADD COLUMN student_answer TEXT;
ALTER TABLE questions ADD COLUMN submitted_at TEXT;
ALTER TABLE questions ADD COLUMN reveal_allowed INTEGER NOT NULL DEFAULT 0;

-- 8.3 错题状态事件表：confirmed/rejected 等状态变化全量留痕
CREATE TABLE IF NOT EXISTS error_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    error_id INTEGER NOT NULL REFERENCES errors(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,          -- created | confirmed | rejected | reopened | edited
    old_status TEXT,
    new_status TEXT,
    payload_json TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_error_events_error ON error_events(error_id);

-- 业务唯一约束（历史数据已由 run_migrations 去重，可安全创建）
CREATE UNIQUE INDEX IF NOT EXISTS idx_courses_name_semester
    ON courses(name, COALESCE(semester, ''));
CREATE UNIQUE INDEX IF NOT EXISTS idx_materials_file_hash
    ON materials(file_hash) WHERE file_hash IS NOT NULL AND file_hash != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_chapters_course_no
    ON chapters(course_id, chapter_no) WHERE chapter_no IS NOT NULL;
