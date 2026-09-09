# v5 产品文档

> 产品版本 **5.5.1** · DB Schema **v8**（migrations 0001–0008）· API 前缀 `/api` v1 · FastAPI + Vue3 · 本地模型网关 `127.0.0.1:8080`

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [PRODUCT.md](PRODUCT.md) | 产品概览：功能范围、业务流、角色与页面 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 技术架构：模块划分、DAG/Worker、数据模型、可靠性设计 |
| [OPERATIONS.md](OPERATIONS.md) | 运维手册：启动/备份/恢复/迁移/排障 |
| [TESTING.md](TESTING.md) | 测试体系：单测/冒烟/契约测试的分层与运行方式 |
| [VERSIONS.md](VERSIONS.md) | 版本统一说明与历史沿革 |

## 快速启动

```powershell
# 后端（127.0.0.1:8801）
cd v5\backend
python -m uvicorn app.main:app --port 8801

# 前端（开发 5173 / 构建产物 dist/）
cd v5\frontend
npm install
npm run dev      # 或 npm run build
```

## 目录结构

```
v5/
├── backend/
│   ├── app/            # FastAPI 应用（main.py 路由、dag_* 业务流、workers 后台执行）
│   ├── migrations/     # SQLite 迁移（0001-0008，带 checksum，禁止改已应用文件）
│   ├── prompts/        # 外置提示词（<workflow>/<name>.<version>.md，带 sha256）
│   ├── tools/          # 运维脚本（migrate_database、restore_snapshot）
│   ├── tests/          # unittest 套件（108 个用例）
│   └── data/           # 运行数据（v5.db、blobs/、obsidian_vault/、backups/）
├── frontend/           # Vue3 + Vite + Tailwind（vue-router 分包、SSE 实时流）
└── docs/               # 本文档
```
