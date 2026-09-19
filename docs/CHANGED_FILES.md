# 已改动文件清单（交接书附录）

> 本文件是 `THIRD_PARTY_HANDOFF_GUIDE.md` 的附录，记录 2026-09-15 运维与修复会话
> 对项目产生的**全部实际改动**，供接手方核对基线差异。当时的源工作区没有可供
> 交接的完整 Git 基线，本清单基于操作记录 + 文件时间戳整理；后续已建立 Git 私有备份。

- **改动日期**：2026-09-15
- **改动范围**：1 个源码文件（3 处编辑）+ 前端重新构建 + 运行时产物
- **改动主题**：修复移动端上传页"需要带 token"提示条的误导性逻辑
- **后端源码**：**零改动**（`backend/app/**` 未动）

---

## A. 源码改动（1 个文件，3 处）

### `frontend/src/components/MobileUploadPage.vue`

最后修改时间：2026-09-15 14:32:32（大小 27,304 字节）

#### 改动 1：新增凭证有效性判定状态（状态声明区）

```ts
const loadingMeta = ref(false)
const metaError = ref('')
// 凭证有效性判定：loadMeta 的三个请求都过 Token 鉴权，成功即凭证有效。
// serverInfo.mobile_token_required 只表示"服务端启用了鉴权"（白名单路径，
// 与调用方是否带有效 token 无关），不能单独作为警告依据。
const metaLoaded = ref(false)
```

#### 改动 2：`loadMeta()` 维护 `metaLoaded`

```ts
    courses.value = cs || []
    chapters.value = chs || []
    lessons.value = ls || []
    metaLoaded.value = true          // ← 新增：三个鉴权请求全部成功
    if (courses.value.length && !courseId.value) {
      courseId.value = courses.value[0].id
    }
  } catch (e: any) {
    metaLoaded.value = false         // ← 新增：任一请求失败（含 403）
    metaError.value = e?.message || String(e) || '加载失败'
```

#### 改动 3：提示条逻辑修正（模板）

修改前：

```vue
<p v-if="serverInfo.mobile_token_required" class="...amber...">
  ⚠ 当前已启用 Token 鉴权。请在电脑端...
</p>
```

修改后：

```vue
<p v-if="serverInfo.mobile_token_required && !metaLoaded" class="...amber...">
  ⚠ 当前已启用 Token 鉴权。请在电脑端「远程上传凭证」页生成带 ?token= 的专属链接,
  或在本页下方「凭证」区手动输入 token。
</p>
<p v-else-if="serverInfo.mobile_token_required && metaLoaded"
   class="...emerald... text-emerald-400">
  ✓ Token 鉴权已启用，凭证有效，可正常上传。
</p>
<p v-else class="mt-2 text-xs text-slate-500">
  手机需安装 Tailscale App 且登录同一账号;Funnel 模式需在 URL 后加 ?token=xxx。
</p>
```

### 修复原因（缺陷定性）

`/api/mobile/server-info` 返回的 `mobile_token_required` 定义于
`backend/app/main.py:2621`：`bool(config.MOBILE_TOKEN)`——仅表示**服务端**
是否启用鉴权的全局配置事实；且该端点在鉴权白名单内（`_PUBLIC_PATHS`），
调用方不带 token 也能访问，**结构上不可能**反映调用方凭证是否有效。

原模板 `v-if="serverInfo.mobile_token_required"` 导致：只要服务端配置了
token，警告黄条**恒显示**——即使调用方已持有有效凭证。实测持有正确
token 时公网 API 返回 200，用户却被黄条误导认为鉴权失败。

### 修复后行为矩阵

| 场景 | 显示 |
|---|---|
| 服务端启用鉴权 + 凭证有效（loadMeta 成功） | ✅ 绿条"凭证有效，可正常上传" |
| 服务端启用鉴权 + 凭证缺失/无效（loadMeta 403） | ⚠ 黄条提示 |
| 服务端未启用鉴权 | 灰色说明文字（原样） |

---

## B. 构建产物更新（由 A 引发）

- `frontend/dist/**` 全量重新构建（`npm run build`，vite 2.38s，无错误、无 lint 告警）
- 关键产物 hash 变化（旧版本同目录内已被构建覆盖）：
  - `assets/MobileUploadPage-DBmlh1yr.js`（17.65 kB / gzip 6.69 kB）
  - `assets/index-Osbfxj8Y.js`（入口，140.52 kB）
- 已验证：公网 Funnel 返回的 `index.html` 与本地 `dist/index.html` 引用同一新产物

**回滚方法**：`git` 不存在，若需回滚源码，按上文改动 1/2/3 逆向删除新增行即可；
然后重新 `npm run build`。

---

## C. 运行时产物（非源码，系统自动生成）

| 文件/对象 | 性质 | 说明 |
|---|---|---|
| `backend/data/_server.out.log` | 运维日志（**可删**） | 本次以 production 模式后台启动后端时创建 |
| `backend/data/_server.err.log` | 运维日志（**可删**） | 同上（后端 stdout/stderr 重定向文件） |
| `backend/data/backups/v5_20260915_141137.db` | 自动备份 | 后端每次启动固有行为，保留最近 7 个 |
| `v5.db` schema 迁移 | 系统自动 | 本次启动时应用版本 24、25（user_version 23→25） |

---

## D. 进程与运行状态（不落盘）

| 进程 | 端口 | 启动方式 |
|---|---|---|
| LLM 网关（`runtime\node.exe dist\index.js`，PID 22280） | 8317 | `Start-Gateway.ps1 -NoBrowser` |
| 后端 uvicorn（PID 8800） | 8800 | `python run.py`，环境：`V5_ENV=production`、`V5_GATEWAY_MODE=live`、`V6_LEARNING_ENGINE=on`、`V6_SEGMENT_GATEWAY_TIMEOUT=600`、`V5_GATEWAY_URL=http://127.0.0.1:8317` |
| Tailscale Funnel | — | 配置早已存在（`/ → http://127.0.0.1:8800`），本次仅验证，未改配置 |

验证记录（2026-09-15）：
- 网关 `GET /health` → ok；后端 `GET /api/health` → ok（gateway_mode=live）
- 公网 `https://hantangmatebook.tailc03ef9.ts.net/api/health` → 200
- 公网无 token 请求 `/api/courses` → 403（鉴权中间件生效）
- 公网带 `?token=` 请求 `/api/courses` → 200

---

## E. 明确未改动声明

- `backend/app/**`（含 `main.py`、`learning_engine/**`、prompts、migrations）—— 零改动
- `frontend/src/**` 其余组件 —— 零改动
- `backend/.env`、网关 `.env` —— 零改动（仅读取验证）
- Tailscale serve/funnel 配置 —— 零改动
- 会话早前生成的代码合并 dump 位于桌面（工作区外），项目目录内无遗留临时脚本
