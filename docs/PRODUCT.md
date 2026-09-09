# 产品概览

版本 5.5.1（稳定性补丁）。

## 核心业务流

| 流 | 入口 | 链路（DAG 节点） |
| --- | --- | --- |
| 听课流 lesson | 听课流页 / 移动端 | 材料解析 → 混合检索（run级chunk优先+RAG） → 大纲 → 笔记 → 证据校验 → **真实 critic 审查+修订闭环（最多1轮）** → 持久化（证据空不自动确认） → Obsidian 真实路径同步 |
| 作业流 homework | 作业处理页 | OCR（仅图片时先 `homework_ocr` 停在 `awaiting_confirmation`，用户 confirm 后才求解） → 风险分级 → 求解+独立第二解 → 裁决（冲突 → `requires_human_review`） → 教学讲解 → 范围审计 → 持久化；**先做后看**：学生提交作答后才展示解答 |
| 错题流 error | 错题页 | 视觉提取 → 分层错因（现象/直接/根本/知识缺口，结构化 JSON 落库） → 独立审查 → provisional 入库 → **confirm/reject（rowcount+409+error_events 留痕）** → SM-2 首次复习 |
| 复习流 review | 复习中心 | 资源聚合 → 复习包 → 自测题 → **作答→判分→SM-2 间隔与掌握度闭环**（rating 0-5，连续错重置，掌握度仅由正误驱动） |

## 关键业务规则

- **材料 blob 化**：上传即 `file_blobs`（sha256 去重、引用计数、GC 状态机 live→pending_delete→deleted），删除材料仅解引用，物理 GC 异步补偿。
- **类型支持矩阵**：pdf/ppt/doc/text/txt/md 可解析；image→`needs_ocr`；audio→`transcribing`；其余类型上传即拒绝，不产生假占位。
- **OCR 两阶段**：仅图片作业不直接求解；确认接口修正文本后才排队求解。
- **先做后看**：`questions.reveal_allowed` 未提交作答前，作业详情与 run result 均不返回解答。
- **Obsidian 真实路径**：`01 Courses/<课程名>/<第N章 章节名>/[note-<id>] 标题.md`，文件名 Windows 安全化（保留名/非法字符清洗），稳定 ID 防重命名断链。
- **运行控制**：取消（queued 直接取消；running 置 cancel_requested 由 Worker 中断）、重试（子 run 复用父成功节点输出）、重启自动恢复（interrupted→queued、孤儿 run 重新入队、租约过期回收）。

## 页面（前端）

今日总览 / 待确认错题 / 课程与考试 / 章节进度 / 听课流 / 材料收件箱 / 作业处理 / 复习中心 / 模型与额度 / Obsidian 同步 / 运行日志 / 远程访问凭证 / 移动采集。运行日志页对运行中的 run 使用 SSE 实时推送（断线指数退避重连，5 次后降级轮询）。
