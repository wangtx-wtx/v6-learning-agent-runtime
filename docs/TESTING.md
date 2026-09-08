# 测试体系

## 分层

| 层 | 位置 | 说明 |
| --- | --- | --- |
| 单元/集成测试 | `backend/tests/` | unittest，86 用例，全部离线（不依赖本地网关），~22s |
| 真实冒烟 | `backend/smoke_phase*.py` | 端到端真实运行（依赖后端 8801 + 真实模型网关 8080），人工/发布前执行 |
| 契约测试 | `backend/tests/test_phase_h.py` | Fake Gateway 按真实 chat 响应结构（content/tokens_in/tokens_out/model）离线验证 DAG 节点 |
| CI | `.github/workflows/ci.yml` | 后端 compileall + unittest；前端 vue-tsc + build |

## 运行

```powershell
cd v5\backend
python -m unittest discover -s tests -t .        # 全量单测（自动使用临时库，不碰生产数据）
python -m unittest tests.test_phase_e -v         # 单个模块

# 冒烟（先起后端）
python -m uvicorn app.main:app --port 8801
python smoke_phaseb.py    # Worker 并发/取消/重试
python smoke_phasec.py    # blob 去重/GC
python smoke_phased.py    # 复习作答闭环
python smoke_phasee.py    # 听课 critic/错题事件/作业状态机
python smoke_phasef.py    # 混合检索/审计
```

## 测试约定

- 测试用 `db.configure_db(tmp)` 隔离数据库，`tearDown` 里 `reset_connections()`；
- TestClient 会触发 mobile-token 中间件 → setUp 置空 `app_config.MOBILE_TOKEN`，tearDown 还原；
- Windows 控制台统一 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`；
- 涉及外网关的行为（embedding/chat）在单测中一律 mock/降级路径验证，真实调用只在冒烟脚本中发生。

## 用例分布（86）

- `test_core.py` 14 · `unit/test_database.py` 11 · `test_workers.py` 13 · `test_blob_gc.py` 10 · `test_review_flow.py` 13
- `test_phase_e.py` 10（Obsidian 路径/证据空规则/错题事件/作业状态机/先做后看）
- `test_phase_f.py` 10（prompt 加载/夹具覆盖/schema 绑定/混合检索/审计）
- `test_phase_h.py` 5（快照恢复闭环/Fake Gateway 契约）
