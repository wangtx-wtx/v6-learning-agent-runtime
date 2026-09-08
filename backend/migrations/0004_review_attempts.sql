-- 0004_review_attempts.sql — 复习作答与 SM-2 掌握度闭环（V5.5 方案 5.3/5.4）
-- 每次复习保存完整历史记录，不允许只覆盖错题表最终值。

ALTER TABLE review_attempts ADD COLUMN error_id INTEGER REFERENCES errors(id) ON DELETE SET NULL;
ALTER TABLE review_attempts ADD COLUMN expected_answer TEXT;
ALTER TABLE review_attempts ADD COLUMN self_rating INTEGER;
ALTER TABLE review_attempts ADD COLUMN score REAL;
ALTER TABLE review_attempts ADD COLUMN feedback TEXT;
ALTER TABLE review_attempts ADD COLUMN mastery_before REAL;
ALTER TABLE review_attempts ADD COLUMN interval_before REAL;
ALTER TABLE review_attempts ADD COLUMN interval_after REAL;
ALTER TABLE review_attempts ADD COLUMN reviewed_at TEXT;
ALTER TABLE review_attempts ADD COLUMN next_review_at TEXT;

CREATE INDEX IF NOT EXISTS idx_review_attempts_review ON review_attempts(review_id);
CREATE INDEX IF NOT EXISTS idx_review_attempts_error ON review_attempts(error_id);
