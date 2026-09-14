-- V6 Phase 6: explainable links from learning feedback to Knowledge Units.
CREATE TABLE knowledge_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE CASCADE,
    stable_key TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'concept',
    summary TEXT NOT NULL DEFAULT '',
    emphasis REAL NOT NULL DEFAULT 0 CHECK(emphasis >= 0 AND emphasis <= 1),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE UNIQUE INDEX uq_knowledge_units_scope_key ON knowledge_units(
    COALESCE(course_id,-1),COALESCE(chapter_id,-1),COALESCE(lesson_id,-1),stable_key);
CREATE INDEX idx_knowledge_units_scope ON knowledge_units(course_id,chapter_id,lesson_id);

CREATE TABLE knowledge_unit_sources (
    knowledge_unit_id INTEGER NOT NULL REFERENCES knowledge_units(id) ON DELETE CASCADE,
    source_span_id INTEGER NOT NULL REFERENCES source_spans(id) ON DELETE CASCADE,
    relation TEXT NOT NULL DEFAULT 'supports',
    PRIMARY KEY(knowledge_unit_id,source_span_id,relation)
);
CREATE INDEX idx_knowledge_unit_sources_span ON knowledge_unit_sources(source_span_id);

CREATE TABLE knowledge_mastery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_unit_id INTEGER NOT NULL REFERENCES knowledge_units(id) ON DELETE CASCADE,
    course_id INTEGER REFERENCES courses(id) ON DELETE CASCADE,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    mastery REAL NOT NULL DEFAULT 0.5 CHECK(mastery >= 0 AND mastery <= 1),
    confidence REAL NOT NULL DEFAULT 0 CHECK(confidence >= 0 AND confidence <= 1),
    signal_count INTEGER NOT NULL DEFAULT 0 CHECK(signal_count >= 0),
    updated_from TEXT NOT NULL DEFAULT 'manual',
    explanation TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(knowledge_unit_id)
);
CREATE INDEX idx_knowledge_mastery_scope ON knowledge_mastery(course_id, chapter_id, mastery);

CREATE TABLE learning_feedback_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_unit_id INTEGER NOT NULL REFERENCES knowledge_units(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL
        CHECK(source_type IN ('homework_question','error','review_attempt','manual')),
    source_id INTEGER NOT NULL,
    relation TEXT NOT NULL DEFAULT 'tests'
        CHECK(relation IN ('tests','mistake_on','reviewed','supports','manual')),
    weight REAL NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(knowledge_unit_id, source_type, source_id, relation)
);
CREATE INDEX idx_feedback_source ON learning_feedback_links(source_type, source_id, active);
CREATE INDEX idx_feedback_ku ON learning_feedback_links(knowledge_unit_id, active);

CREATE TABLE mastery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_unit_id INTEGER NOT NULL REFERENCES knowledge_units(id) ON DELETE CASCADE,
    feedback_link_id INTEGER REFERENCES learning_feedback_links(id) ON DELETE SET NULL,
    mastery_before REAL NOT NULL,
    mastery_after REAL NOT NULL,
    confidence_before REAL NOT NULL,
    confidence_after REAL NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_mastery_events_ku ON mastery_events(knowledge_unit_id, id);
