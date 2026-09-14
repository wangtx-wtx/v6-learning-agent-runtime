CREATE TABLE model_profiles (
    id TEXT PRIMARY KEY,
    gateway_model TEXT NOT NULL,
    provider TEXT NOT NULL,
    family TEXT NOT NULL,
    capability TEXT NOT NULL CHECK (capability IN ('text','vision','multimodal','ocr','embedding','rerank')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    is_builtin INTEGER NOT NULL DEFAULT 0 CHECK (is_builtin IN (0,1)),
    context_window INTEGER,
    embedding_dimensions INTEGER,
    input_cost REAL,
    output_cost REAL,
    cost_unit TEXT NOT NULL DEFAULT 'unknown',
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE model_role_routes (
    role TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    model_id TEXT NOT NULL REFERENCES model_profiles(id) ON DELETE RESTRICT,
    PRIMARY KEY (role, position),
    UNIQUE (role, model_id)
);

CREATE INDEX idx_model_profiles_enabled ON model_profiles(enabled, family);
CREATE INDEX idx_model_role_routes_model ON model_role_routes(model_id);

INSERT INTO model_profiles
    (id, gateway_model, provider, family, capability, enabled, is_builtin, context_window, embedding_dimensions, input_cost, output_cost, cost_unit, notes)
VALUES
    ('deepseek_v4_free', 'DeepSeek-flash', 'deepseek', 'deepseek', 'multimodal', 1, 1, 1000000, NULL, 0.15, 0.60, 'USD/1M tokens off-peak', '主力解题、笔记与图片理解'),
    ('deepseek_v4_official', 'deepseek-v4-pro', 'deepseek', 'deepseek', 'text', 1, 1, 1000000, NULL, 0.66, 1.98, 'USD/1M tokens off-peak', 'DeepSeek 高质量备用'),
    ('qwen3_8_27b', 'qwen3.8-27b', 'qwen', 'qwen', 'multimodal', 1, 1, 256000, NULL, 3.0, 12.0, 'CNY/1M tokens', 'Qwen 独立候选'),
    ('qwen3_flash', 'qwen3.8-flash', 'qwen', 'qwen', 'text', 1, 1, 1000000, NULL, 0.8, 2.7, 'CNY/1M tokens', '轻量结构化任务'),
    ('qwen3_vl', 'qwen3-vl-flash', 'qwen', 'qwen', 'vision', 1, 1, 256000, NULL, 0.15, 1.5, 'CNY/1M tokens <=32K', '通用视觉备用'),
    ('ocr', 'qwen-vl-ocr', 'qwen', 'qwen', 'ocr', 1, 1, NULL, NULL, 0.3, 0.5, 'CNY/1M tokens', '专用 OCR'),
    ('minimax_m3', 'MiniMax-M3', 'minimax', 'minimax', 'multimodal', 1, 1, 1000000, NULL, 2.1, 8.4, 'CNY/1M tokens <=512K', '高风险并行与争议仲裁'),
    ('glm_flash', 'glm-5.3-flash', 'zhipu', 'glm', 'multimodal', 1, 1, NULL, NULL, 0.8, 2.8, 'CNY/1M tokens', '独立审查与证据核验'),
    ('embedding', 'ecnu-embedding-small', 'ecnu', 'embedding', 'embedding', 1, 1, 8192, 1024, 0.05, NULL, 'credits/call', '1024 维文本向量'),
    ('rerank', 'ecnu-rerank', 'ecnu', 'rerank', 'rerank', 1, 1, 8192, NULL, 0.1, NULL, 'credits/call', 'bge-reranker-v2-m3')
;

INSERT INTO model_role_routes (role, position, model_id) VALUES
    ('vision_reader', 0, 'deepseek_v4_free'),
    ('vision_reader', 1, 'qwen3_vl'),
    ('vision_reader', 2, 'minimax_m3'),
    ('transcriber_splitter', 0, 'qwen3_flash'),
    ('transcriber_splitter', 1, 'deepseek_v4_free'),
    ('lesson_structurer', 0, 'qwen3_flash'),
    ('lesson_structurer', 1, 'deepseek_v4_free'),
    ('student_simulator', 0, 'deepseek_v4_free'),
    ('student_simulator', 1, 'deepseek_v4_official'),
    ('note_writer', 0, 'deepseek_v4_free'),
    ('note_writer', 1, 'deepseek_v4_official'),
    ('critic', 0, 'glm_flash'),
    ('critic', 1, 'qwen3_8_27b'),
    ('critic', 2, 'minimax_m3'),
    ('evidence_auditor', 0, 'glm_flash'),
    ('evidence_auditor', 1, 'qwen3_flash'),
    ('scope_auditor', 0, 'glm_flash'),
    ('scope_auditor', 1, 'minimax_m3'),
    ('solver', 0, 'deepseek_v4_free'),
    ('solver', 1, 'deepseek_v4_official'),
    ('parallel_solver', 0, 'minimax_m3'),
    ('parallel_solver', 1, 'glm_flash'),
    ('solution_explainer', 0, 'glm_flash'),
    ('solution_explainer', 1, 'deepseek_v4_free'),
    ('error_analyst', 0, 'deepseek_v4_free'),
    ('error_analyst', 1, 'glm_flash'),
    ('review_writer', 0, 'deepseek_v4_free'),
    ('review_writer', 1, 'glm_flash'),
    ('self_test_writer', 0, 'qwen3_flash'),
    ('self_test_writer', 1, 'deepseek_v4_free'),
    ('ocr', 0, 'ocr'),
    ('ocr', 1, 'deepseek_v4_free'),
    ('embedding', 0, 'embedding'),
    ('rerank', 0, 'rerank'),
    ('adjudicator', 0, 'glm_flash'),
    ('adjudicator', 1, 'minimax_m3')
;
