-- 0016_v6_material_domain.sql
-- V6 Learning Engine Phase 1: Material Domain (一次运行理论必须处理的材料集合).
--
-- 只追加,不修改既有 migration. 新表只引用现有实体(run/course/chapter/lesson/material).
--
-- 关键不变量:
--   * 一个 run 恰好有一个 Material Domain (run_id UNIQUE).
--   * 同一 domain 内 material_id 不得重复 ((domain_id, material_id) UNIQUE).
--   * unsupported / failed / excluded_with_reason 必须带 reason_code (CHECK).
--   * domain 一旦 frozen 不允许原地删除成员; 范围变化必须新建 version
--     (由 learning_engine.domain 强制, 见 apps 层保护).

CREATE TABLE material_domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL UNIQUE REFERENCES workflow_runs(id) ON DELETE CASCADE,
    scope TEXT NOT NULL DEFAULT 'lesson' CHECK(scope IN ('lesson','chapter','course')),
    course_id INTEGER REFERENCES courses(id) ON DELETE SET NULL,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
    lesson_id INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
    version INTEGER NOT NULL DEFAULT 1,
    schema_version TEXT NOT NULL DEFAULT 'v6.1',
    domain_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'frozen' CHECK(state IN ('draft','frozen')),
    engine_version TEXT NOT NULL DEFAULT 'v5',
    transcript_chars INTEGER NOT NULL DEFAULT 0,
    frozen_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX idx_material_domains_course ON material_domains(course_id);
CREATE INDEX idx_material_domains_lesson ON material_domains(lesson_id);
CREATE INDEX idx_material_domains_state ON material_domains(state);

CREATE TABLE material_domain_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id INTEGER NOT NULL REFERENCES material_domains(id) ON DELETE CASCADE,
    material_id INTEGER REFERENCES materials(id) ON DELETE SET NULL,
    source_kind TEXT NOT NULL,
    locator TEXT,
    ordinal INTEGER NOT NULL,
    required INTEGER NOT NULL DEFAULT 1,
    raw_chars INTEGER NOT NULL DEFAULT 0,
    raw_tokens INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT,
    state TEXT NOT NULL DEFAULT 'included'
        CHECK(state IN ('included','duplicate','unsupported','failed','excluded_with_reason')),
    reason_code TEXT,
    reason_detail TEXT,
    duplicate_of_item_id INTEGER REFERENCES material_domain_items(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(domain_id, material_id),
    CHECK(state NOT IN ('unsupported','failed','excluded_with_reason') OR reason_code IS NOT NULL),
    CHECK(state <> 'duplicate' OR duplicate_of_item_id IS NOT NULL),
    CHECK(required IN (0,1))
);
CREATE INDEX idx_material_domain_items_domain ON material_domain_items(domain_id);
CREATE INDEX idx_material_domain_items_material ON material_domain_items(material_id);
CREATE INDEX idx_material_domain_items_state ON material_domain_items(domain_id, state);

-- 冻结后的 domain 禁止删除 item。此处不使用 SQLite TRIGGER: 现有迁移执行器
-- (app.database._split_statements) 按分号拆分语句, 无法承载 BEGIN..END 触发器体。
-- 因此冻结保护由 app/learning_engine/domain.py 在应用层强制, 并有测试覆盖
-- (test_v6_material_integrity.TestDomainFreeze)。domain_items 只增不删是本阶段契约。
