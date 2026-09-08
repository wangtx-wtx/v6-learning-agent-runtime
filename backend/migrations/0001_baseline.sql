-- 0001_baseline.sql — V5.5 基线 Schema（对应 V5.4 时代的完整表结构）
-- 版本化迁移体系：由 backend/app/database.py 的 ensure_schema() 按版本号顺序执行。
-- 规则：本文件一旦应用（写入 schema_migrations）即不可修改（有 checksum 校验）。
-- 关键修正：lessons.chapter_id 必须指向 chapters(id)（旧库错误地指向 lessons(id)）。

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT, semester TEXT, teacher TEXT, schedule_json TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    chapter_no INTEGER, title TEXT NOT NULL, syllabus_ref TEXT,
    status TEXT DEFAULT 'not_started',
    review_status TEXT DEFAULT 'none',
    completed_at TEXT, reviewed_at TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    lesson_no TEXT, title TEXT, date TEXT,
    status TEXT DEFAULT 'not_started',
    note_id INTEGER, summary TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    file_path TEXT, file_hash TEXT, type TEXT, kind TEXT,
    display_name TEXT, name TEXT, mime TEXT,
    sha256 TEXT, size_bytes INTEGER DEFAULT 0,
    parser_status TEXT DEFAULT 'uploaded',
    parse_error TEXT,
    status TEXT DEFAULT 'uploaded',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS source_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER REFERENCES materials(id) ON DELETE CASCADE,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE CASCADE,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    type TEXT, locator TEXT, text TEXT,
    image_file TEXT, ocr_confidence REAL DEFAULT 1.0,
    topics_json TEXT, embedding TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE CASCADE,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    title TEXT, body TEXT,
    markdown_path TEXT, status TEXT DEFAULT 'draft',
    version INTEGER DEFAULT 1,
    evidence_json TEXT, model_used TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS homeworks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, status TEXT DEFAULT 'pending',
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE CASCADE,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    mode TEXT, date TEXT, input_dir TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    homework_id INTEGER REFERENCES homeworks(id) ON DELETE CASCADE,
    question_no INTEGER, text TEXT,
    image_file TEXT, knowledge_points_json TEXT,
    scope_json TEXT, risk_json TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS answer_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER REFERENCES questions(id) ON DELETE CASCADE,
    final_answer TEXT, solution_plan TEXT, detailed_solution TEXT,
    confidence REAL, model_used TEXT, parallel_solution TEXT,
    teaching TEXT, evidence_json TEXT,
    status TEXT DEFAULT 'draft', out_of_scope_risk TEXT,
    conflict TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
    question_text TEXT, image_file TEXT,
    student_answer TEXT, correct_answer TEXT, user_explanation TEXT,
    ai_error_json TEXT, final_error_json TEXT,
    mastery REAL DEFAULT 0.0,
    status TEXT DEFAULT 'provisional',
    next_review_at TEXT, review_stage INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    kind TEXT, exam_date TEXT, scope_json TEXT,
    outline TEXT, review_materials TEXT, self_test TEXT,
    auditor_result TEXT, score REAL,
    status TEXT DEFAULT 'pending', outputs_json TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS review_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER REFERENCES reviews(id) ON DELETE CASCADE,
    question_no TEXT, question_text TEXT, user_answer TEXT,
    is_correct INTEGER, mastery_after REAL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow TEXT NOT NULL, mode TEXT,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
    status TEXT DEFAULT 'pending',
    input_json TEXT, output_json TEXT, error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS run_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    node_name TEXT, agent_role TEXT, model TEXT, status TEXT,
    attempt INTEGER DEFAULT 1,
    started_at TEXT, finished_at TEXT,
    tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    input_ref TEXT, output_ref TEXT, output_json TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS sync_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT, asset_id INTEGER, asset_path TEXT,
    content_hash TEXT, status TEXT DEFAULT 'pending',
    retries INTEGER DEFAULT 0, last_error TEXT, synced_at TEXT
);

CREATE TABLE IF NOT EXISTS graph_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    title TEXT, node_type TEXT DEFAULT 'topic', meta_json TEXT
);

CREATE TABLE IF NOT EXISTS graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER REFERENCES graph_nodes(id) ON DELETE CASCADE,
    target_id INTEGER REFERENCES graph_nodes(id) ON DELETE CASCADE,
    relation TEXT, status TEXT DEFAULT 'confirmed'
);

CREATE TABLE IF NOT EXISTS academic_calendar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    event_type TEXT, title TEXT, date TEXT, detail TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS evidence_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT, owner_id INTEGER,
    chunk_id INTEGER REFERENCES source_chunks(id) ON DELETE SET NULL,
    source_type TEXT, quote TEXT, locator TEXT,
    evidence_kind TEXT, verify_status TEXT, verify_method TEXT,
    model TEXT, prompt_version TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS run_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER UNIQUE REFERENCES workflow_runs(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'queued',
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS parse_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER UNIQUE REFERENCES materials(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'queued',
    error TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_chapters_course ON chapters(course_id);
CREATE INDEX IF NOT EXISTS idx_lessons_chapter ON lessons(chapter_id);
CREATE INDEX IF NOT EXISTS idx_lessons_course ON lessons(course_id);
CREATE INDEX IF NOT EXISTS idx_materials_lesson ON materials(lesson_id);
CREATE INDEX IF NOT EXISTS idx_materials_course ON materials(course_id);
CREATE INDEX IF NOT EXISTS idx_materials_hash ON materials(sha256);
CREATE INDEX IF NOT EXISTS idx_chunks_material ON source_chunks(material_id);
CREATE INDEX IF NOT EXISTS idx_chunks_chapter ON source_chunks(chapter_id);
CREATE INDEX IF NOT EXISTS idx_notes_lesson ON notes(lesson_id);
CREATE INDEX IF NOT EXISTS idx_questions_homework ON questions(homework_id);
CREATE INDEX IF NOT EXISTS idx_answer_items_question ON answer_items(question_id);
CREATE INDEX IF NOT EXISTS idx_errors_chapter_status ON errors(chapter_id, status);
CREATE INDEX IF NOT EXISTS idx_run_nodes_run ON run_nodes(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_workflow ON workflow_runs(workflow, status);
