CREATE TABLE document_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER REFERENCES notes(id) ON DELETE CASCADE,
    review_id INTEGER REFERENCES reviews(id) ON DELETE CASCADE,
    document_type TEXT NOT NULL CHECK(document_type IN ('lesson_note','review_handout')),
    template_id TEXT NOT NULL DEFAULT 'british_newspaper_v1',
    template_version INTEGER NOT NULL DEFAULT 1,
    structured_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    html_path TEXT,
    pdf_path TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','rendering','ready','partial','failed')),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK ((note_id IS NOT NULL AND review_id IS NULL) OR (note_id IS NULL AND review_id IS NOT NULL)),
    UNIQUE(note_id, template_id, template_version),
    UNIQUE(review_id, template_id, template_version)
);
CREATE INDEX idx_document_artifacts_note ON document_artifacts(note_id);
CREATE INDEX idx_document_artifacts_review ON document_artifacts(review_id);
