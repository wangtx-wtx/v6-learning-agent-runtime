-- V6 Phase 5: independent quality/publication/sync states and immutable note revisions.
CREATE TABLE learning_quality_states (
    run_id INTEGER PRIMARY KEY REFERENCES workflow_runs(id) ON DELETE CASCADE,
    note_id INTEGER REFERENCES notes(id) ON DELETE SET NULL,
    processing_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(processing_status IN ('pending','running','completed','failed')),
    coverage_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(coverage_status IN ('pending','passed','degraded','failed')),
    evidence_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(evidence_status IN ('pending','passed','partial','failed','not_required')),
    review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(review_status IN ('pending','passed','revision_required','failed')),
    publication_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(publication_status IN ('pending','rendering','rendered','partial','failed')),
    sync_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(sync_status IN ('pending','syncing','synced','failed','skipped')),
    degradation_reason TEXT,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_learning_quality_note ON learning_quality_states(note_id);

CREATE TABLE note_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    structured_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    evidence_status TEXT NOT NULL,
    coverage_status TEXT NOT NULL,
    review_status TEXT NOT NULL,
    selection_audit_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(note_id, revision)
);
CREATE INDEX idx_note_revisions_run ON note_revisions(run_id);
CREATE INDEX idx_note_revisions_hash ON note_revisions(content_hash);
