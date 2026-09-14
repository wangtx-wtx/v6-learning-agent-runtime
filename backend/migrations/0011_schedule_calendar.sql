-- v5.6.4: 可管理的学期、固定课表与调课调休例外。

CREATE TABLE IF NOT EXISTS academic_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    school_year TEXT,
    semester TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    first_week_monday TEXT NOT NULL,
    teaching_weeks INTEGER NOT NULL DEFAULT 16 CHECK(teaching_weeks BETWEEN 1 AND 30),
    exam_start TEXT,
    exam_end TEXT,
    source TEXT DEFAULT 'manual',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_academic_terms_active ON academic_terms(active, start_date);

CREATE TABLE IF NOT EXISTS course_schedule_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    weekday INTEGER NOT NULL CHECK(weekday BETWEEN 1 AND 7),
    start_week INTEGER NOT NULL DEFAULT 1 CHECK(start_week >= 1),
    end_week INTEGER NOT NULL DEFAULT 16 CHECK(end_week >= start_week),
    week_parity TEXT NOT NULL DEFAULT 'all' CHECK(week_parity IN ('all', 'odd', 'even')),
    periods_json TEXT NOT NULL DEFAULT '[]',
    start_time TEXT,
    end_time TEXT,
    location TEXT,
    teacher TEXT,
    note TEXT,
    source TEXT DEFAULT 'manual',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_schedule_rules_course ON course_schedule_rules(course_id, enabled);
CREATE INDEX IF NOT EXISTS idx_schedule_rules_weekday ON course_schedule_rules(weekday, enabled);

CREATE TABLE IF NOT EXISTS calendar_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    adjustment_type TEXT NOT NULL CHECK(adjustment_type IN (
        'cancel', 'reschedule', 'makeup', 'holiday', 'day_off', 'workday', 'custom'
    )),
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    date TEXT NOT NULL,
    target_date TEXT,
    source_weekday INTEGER CHECK(source_weekday IS NULL OR source_weekday BETWEEN 1 AND 7),
    periods_json TEXT NOT NULL DEFAULT '[]',
    target_periods_json TEXT NOT NULL DEFAULT '[]',
    start_time TEXT,
    end_time TEXT,
    target_start_time TEXT,
    target_end_time TEXT,
    location TEXT,
    teacher TEXT,
    title TEXT,
    detail TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_calendar_adjustments_date ON calendar_adjustments(date, adjustment_type);
CREATE INDEX IF NOT EXISTS idx_calendar_adjustments_target_date ON calendar_adjustments(target_date, adjustment_type);
