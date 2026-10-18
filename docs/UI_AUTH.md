# 账号 UI 设计：注册 · 登录 · 微信扫码 · 账号设置

> 状态：**设计稿，P0-2 与 P1-3 已落地**：登录/注册双视图与字段级校验已实现；账号设置页
> （身份摘要/资料/安全/第三方/数据概览）和管理员用户管理页（分页/防抖/弹层生成密码/确认/审计筛选）
> 已实现。微信链路增强与用户菜单无障碍增量已实现；仍待做的仅 §10.1 中的产品决策项。
>
> 关联文档：[界面设计总纲](UI_DESIGN.md) · [工作区 UI 设计](UI_WORKSPACE.md) ·
> [自选等功能设计](UI_WATCHLIST.md) · 现行后端设计：[工作区与角色权限设计](WORKSPACE.md)、
> [用户数据隔离设计（历史）](USER_ISOLATION.md)、[实施计划](IMPLEMENTATION_PLAN.md)。
>
> 范围：登录、注册、微信扫码与绑定、OAuth 回调落点、账号设置、管理员用户管理中与**账号生命周期**
> 相关的界面，以及所需的最小后端契约补丁（均标【拟新增/增量】）。
> 非目标：视觉稿（Figma/像素级）、浅色主题、国际化、邮箱找回全流程（列待决）、二次验证、账号注销、
> 头像上传，以及工作区/自选页的完整设计（见另两册）。

---

## 1. 目标与用户旅程

| # | 目标 | 可观察的成功标准 |
|---|---|---|
| 1 | 首次部署 30 秒内完成“注册 → 成为管理员 → 进入工作台” | 首次访问默认落在注册视图，提交后进入工作台且角色为 admin |
| 2 | 每种失败都能看懂“为什么 + 下一步” | 不再只显示状态码或英文 Pydantic 文案，每种失败有中文动作 |
| 3 | 三种运行姿态（强制登录、单机直连、微信）可见可解释 | 横幅/按钮按 `GET /auth/config` 显隐，不出现“点了没反应” |
| 4 | 微信登录与绑定是两条清晰路径，失败可回退 | 取消/失败回登录页并带可读原因；绑定成功回账号页并刷新 |
| 5 | 账号自助管理不误操作 | 改密/解绑说明影响面；解绑唯一登录方式被服务端拒绝且有前置提示 |
| 6 | 管理员处理账号生命周期 ≤3 步 | 新建、重置密码、启停各一次确认完成，结果明确 |
| 7 | 键盘与读屏可用 | label 关联、错误可播报、失败后焦点落到正确字段 |

**角色**：匿名访客（门禁开→跳登录；门禁关→进工作台）；普通用户（工作台 + 账号设置）；
管理员（+ 用户管理，来源：首个账号 / 管理员创建 / `SM_AUTH_ADMIN_*` 或 CLI）；
部署者（环境变量与 CLI，界面只读取结果）。

### 1.1 旅程 A：首次部署（门禁开启、无账号）

```text
首次访问 /                GET /auth/config → {auth_enabled:true, has_users:false}
   └─ SessionGate 重定向 ──→ /login?next=%2F
        ├─ has_users=false：默认落在「注册」视图，提示“首个账号即管理员”
        └─ 提交 201 → 保存 token → 进入 /（next）
              └─ 角色 = admin（SM_AUTH_FIRST_USER_ADMIN=true 且库中无用户）
```

### 1.2 旅程 B：日常登录 / 忘记密码

```text
/login
 ├─ 密码登录（用户名或邮箱）→ 200 → next
 │    ├─ 401 bad_credentials  → 字段级“用户名或密码不正确”，聚焦密码
 │    ├─ 403 user_disabled    → 横幅“账号已被禁用，请联系管理员”
 │    └─ 429 too_many_attempts→ 横幅含等待分钟数，按钮短暂禁用
 ├─ 忘记密码 → 现状无自助找回（README §8）：提示联系管理员重置，管理员可用微信登录兜底
 └─ 已登录访问 /login → 自动 replace 回 next（现状已实现）
```

### 1.3 旅程 C：微信扫码登录 / 绑定

```text
[登录页] 微信扫码登录 → GET /auth/wechat/url?redirect=<next>&bind=false
   ▼ 微信授权页（open 桌面扫码 / mp 微信内 / mock 本地模拟）
   ▼ GET /api/v1/auth/wechat/callback?code=…&state=…
   ├─ 成功 → 302 /auth/callback?ticket=<一次性>&next=<next>[&bound=1]
   │           └─ 前端 POST /auth/ticket 换 token → next（或 /account?bound=1）
   ├─ 取消 → 302 /login?error=已取消微信授权
   └─ 失败 → 302 /login?error=<可读原因>
```

### 1.4 降级：`SM_AUTH_ENABLED=false`（单机直连）

```text
任意页面（不强制登录）
 ├─ 工作台照常打开；右上角是次要按钮「账号入口」而非「登录」
 ├─ 已登录用户仍显示完整用户菜单（token 有效时 /auth/me 照常解析）
 ├─ /login 显示黄条“服务器未开启登录校验……登录后仍可使用账号相关功能”
 ├─ /users 对匿名可用（服务端回落 local_guest_admin，id=0）→ 界面须明确标注本机模式
 └─ 首个注册账号仍会成为管理员，为后续开启门禁做准备
```

### 1.5 会话状态流转

```text
起始 → config 成功 → 有 token ? 已登录 : 匿名
匿名 + 门禁开 → /login 重定向；匿名 + 门禁关 → 工作台
已登录 → 工作台；/auth/me 失败 → 清 token → 匿名（【增量】提示登录已过期）
```

---

## 2. 现有基线【现状】

### 2.1 页面与路由

| 路由 | 页面文件 | 门禁 | 访问条件 | 现状要点 |
|---|---|---|---|---|
| `/login` | `frontend/src/pages/Login.tsx` | 公开 | 任意 | 单卡片 + 登录/注册 Tab；`?next=`、`?error=` 入参 |
| `/auth/callback` | `frontend/src/pages/AuthCallback.tsx` | 公开 | 任意 | 读 `ticket`/`next`/`bound`，只交换一次 |
| `/account` | `frontend/src/pages/Account.tsx` | SessionGate | 已登录（或门禁关闭） | 资料、密码、微信、数据概览；未登录占位 |
| `/users` | `frontend/src/pages/Users.tsx` | SessionGate | admin 或门禁关闭 | 列表、新建、重置密码、启停、角色、审计 |
| `/*` | `frontend/src/App.tsx` | SessionGate | 同上 | 工作台；`/login`、`/auth/callback` 不渲染全局导航 |

### 2.2 前端文件与职责

| 文件 | 现状 | 本册改动 |
|---|---|---|
| `pages/Login.tsx`（229 行） | 按 `allow_register` 显示 Tab；门禁黄条；微信按钮；`has_users` 提示 | 【增量】拆分登录/注册视图 + 字段级校验（§4.1、§4.2） |
| `pages/AuthCallback.tsx`（62 行） | `useRef` 防 StrictMode 重复交换；loading/失败两态 | 【增量】错误分类、模式提示、返回链接（§4.3） |
| `pages/Account.tsx`（323 行） | 资料/密码/微信/数据概览四块 | 【增量】信息架构重排、字段级反馈、未登录态（§4.4） |
| `pages/Users.tsx`（399 行） | 表格 + 内联创建 + 重置弹层 + 审计 | 【增量】分页、确认、错误态、密码生成（§4.5） |
| `App.tsx` | `SessionGate`、`Layout`、`UserMenu`、`WorkspaceBar` | 【增量】用户菜单可访问性、匿名入口文案（§3） |
| `auth.ts` | zustand：config/user/init/login/register/logout/applyToken；`canManageUsers`、`requiresLogin` | 【增量】记录会话失效原因（§7） |
| `api.ts` | token 存 `localStorage["sm.auth.token"]`；401 清 token 派发 `unauthorized`；只取 `detail` | 【增量】解析 `X-Auth-Error`/body `code`（§6.2） |
| `types.ts` | `AuthConfig`/`AuthUser`/`AdminUser`/`Identity`/`TokenResponse`/`UserPage`/`AuditLog` | 【增量】错误码与字段级错误类型（不加后端字段） |

### 2.3 后端端点【现状】

| 方法 + 路径 | 认证 | 作用 | 关键行为 |
|---|---|---|---|
| `GET /api/v1/auth/config` | 公开 | 登录页引导 | `auth_enabled`、`allow_register`、`wechat_enabled`、`wechat_mode`、`has_users`、`workspace_enabled`、`version` |
| `POST /api/v1/auth/register` | 公开 | 自助注册 | 201；关闭注册 403；首账号提权 admin；自动建个人工作区 |
| `POST /api/v1/auth/login` | 公开 | 用户名/邮箱 + 密码 | 200；失败统一 `bad_credentials`；8 次/900 秒限流 |
| `POST /api/v1/auth/logout` | 可选 Bearer | 退出 | 204；`token_version += 1`，所有设备失效 |
| `GET /api/v1/auth/me` | Bearer | 当前账号 | 200；含 `has_password`、`identities` |
| `PATCH /api/v1/auth/me` | Bearer | 改昵称/邮箱/头像 URL | 200；邮箱冲突 409；服务端 trim 并截断 |
| `POST /api/v1/auth/me/password` | Bearer | 改密/设密 | 200 返回新 token；原密码错 400；弱密码 422 |
| `GET /api/v1/auth/me/identities` | Bearer | 绑定列表 | 200（前端未调用，直接用 `/me` 内嵌列表） |
| `DELETE /api/v1/auth/me/identities/{id}` | Bearer | 解绑微信 | 204；唯一登录方式 400 |
| `GET /api/v1/auth/wechat/url` | 可选 Bearer | 生成 state 与授权 URL | 200；`off` 400；`bind=true` 无 token 401 |
| `GET /api/v1/auth/wechat/mock/authorize` | 公开 | mock 授权页（HTML） | 仅 `wechat_mode=mock`，否则 404 |
| `GET /api/v1/auth/wechat/callback` | 公开 | 微信回调 | 302 回 SPA：成功带 ticket，失败带 `?error=` |
| `POST /api/v1/auth/ticket` | 公开 | 一次性 ticket 换 token | 200；已用/过期 400 `flow_state_invalid` |
| `GET /api/v1/users` | 管理员 | 用户列表 | 200；`q`/`role`/`status`/`page`/`page_size`；含统计列 |
| `POST /api/v1/users` | 管理员 | 创建账号 | 201；重复 409；弱密码 422（此路径有捕获） |
| `PATCH /api/v1/users/{id}` | 管理员 | 改昵称/邮箱/角色/状态 | 200；改自己角色/状态 400；最后管理员保护 400 |
| `POST /api/v1/users/{id}/password` | 管理员 | 重置密码 | 204；弱密码 422；目标所有会话立即失效 |
| `DELETE /api/v1/users/{id}` | 管理员 | 删除账号 | 204；不能删自己/最后管理员 |
| `GET /api/v1/users/audit-logs` | 管理员 | 审计流水 | 200；`limit` 1–500，可按 `action` 过滤 |

### 2.4 开关与环境变量

| 环境变量 | 默认 | 现状语义 | UI 可见结果 |
|---|---|---|---|
| `SM_AUTH_ENABLED` | false | 是否强制登录；中间件拦 `/api/v1` 白名单外请求 | 登录页黄条；匿名可进工作台；`/users` 匿名可用 |
| `SM_AUTH_ALLOW_REGISTER` | true | 是否允许自助注册 | 是否显示“注册”Tab；接口 403 |
| `SM_AUTH_FIRST_USER_ADMIN` | true | 库中 0 用户时首账号提权 admin | `has_users=false` 提示 |
| `SM_AUTH_TOKEN_TTL_MINUTES` | 10080（7 天） | access token 有效期 | 过期后 401 `expired` |
| `SM_AUTH_SECRET_KEY` | 空 | 签名密钥；空则落盘 `.auth_secret` | 无 UI；多实例不一致会全员掉线 |
| `SM_AUTH_MAX_FAILED_LOGINS` / `_WINDOW` | 8 / 900 秒 | 同一“标识+IP”失败上限，0 关闭 | 429 + 等待提示 |
| `SM_AUTH_OAUTH_STATE_TTL_SECONDS` / `_TICKET_TTL_SECONDS` | 600 / 120 | state / ticket 有效期 | 超时回调 → `/login?error=` |
| `SM_AUTH_FRONTEND_BASE` | 空 | 回调后跳转的 SPA 源（开发跨端口必备） | 配错时微信回跳白屏/404 |
| `SM_WECHAT_LOGIN_MODE` | open | `open`/`mp`/`mock`/`off` | 微信按钮显隐与 mock 提示 |
| `SM_WECHAT_APP_ID` / `SM_WECHAT_APP_SECRET` | 空 | 凭据；缺失时 open/mp 实际降级 off | 按钮消失 |
| `SM_WECHAT_REDIRECT_URI` | 空 | 须与平台登记完全一致 | 配错时微信拒绝授权 |
| `SM_DATA_ISOLATION` | false | 工作区隔离 | `/account` 数据概览、`/users` 统计列是否有意义 |

> 注：管理员引导变量文档写 `SM_ADMIN_USERNAME`，代码实际读取 `SM_AUTH_ADMIN_USERNAME`（详见 §10.2 #1）。

### 2.5 错误码清单【现状】

`X-Auth-Error` 是**响应头**；中间件拦截返回 JSON body `{"detail","code"}`（无该头），两处形态不同。
以下 code 均在代码中真实存在：

| code | HTTP | 出处 | `detail` 原文 |
|---|---|---|---|
| `bad_credentials` | 401 | `services/service.py::authenticate` | 用户名或密码不正确 |
| `user_disabled` | 403 | `service.py`、`core/auth_deps.py` | 账号已被禁用，请联系管理员 |
| `too_many_attempts` | 429 | `service.py::check_throttle` | 登录失败次数过多，请 N 分钟后再试 |
| `username_taken` | 409 | `service.py::create_user` | 用户名 X 已存在 |
| `email_taken` | 409 | `service.py::create_user` | 邮箱 X 已被使用 |
| `flow_state_invalid` | 400 | `service.py::FlowStateError` | 登录会话已失效，请重新发起登录 / 该登录链接已被使用 |
| `identity_taken` | 409 | `service.py::bind_wechat` | 该微信已绑定其他账号 |
| `last_login_method` | 400 | `service.py::unbind_identity` | 这是账号唯一的登录方式，请先设置密码再解绑 |
| `not_found` | 404 | `service.py::unbind_identity` | 绑定不存在 |
| `auth_error` | 400 | `AuthError` 默认值 | 用户名需 3-64 位…… / 邮箱格式不正确 / 角色只能是 admin 或 user |
| `unauthorized` | 401 | `auth_deps.py`、中间件 | 请先登录 |
| `token_revoked` | 401 | `resolve_token_user` | 登录状态已失效，请重新登录 |
| `expired` | 401 | `tokens.py` → `resolve_token_user` | 登录已过期，请重新登录 |
| `user_missing` | 401 | `resolve_token_user` | 账号不存在 |
| `bad_signature`/`malformed`/`bad_algorithm`/`missing_claim`/`iat_in_future` | 401 | `TokenError.reason` 透传 | 登录凭证无效 |
| `forbidden` | 403 | `require_admin` | 需要管理员权限 |

**没有 code 的失败**（前端只能靠状态码 + `detail`）：

| 场景 | HTTP | 现状响应 |
|---|---|---|
| 注册关闭 | 403 | `detail="当前已关闭自助注册，请联系管理员开通账号"`，无 `X-Auth-Error` |
| 改邮箱冲突 | 409 | `detail="邮箱已被使用"`，无 code |
| 原密码不正确 | 400 | `detail="原密码不正确"`，无 code |
| 弱密码（注册路径） | 500 | **未捕获**：`WeakPasswordError` 在 `register` 中未捕获（见 §10.2） |
| 弱密码（改密/重置/管理员创建） | 422 | `detail=<passwords.py 中文>`，无 code |
| 微信未配置 / 网络 5xx | 400 / — | 长文案无 code / 前端 `请求失败 (HTTP N)` |

---

## 3. 信息架构与入口

### 3.1 登录前（`/login`、`/auth/callback`）

```text
┌ StockMaster（仅品牌）
├ 页面主体：单列，max-width 400px，垂直居中
│  ├ 提示区（门禁关闭 / 注册关闭 / 首个账号 / 服务不可用；最多 1 条，按优先级）【增量】
│  ├ 视图切换：登录 / 注册（allow_register=false 时完全不渲染）
│  └ 表单 + 第三方登录（wechat_enabled=true）
└ 页脚：免责声明 / 门禁关闭时“直接进入工作台 →”
```

不渲染全局导航、工作区切换器、状态栏（现状已如此：两个路由都在 `Layout` 之外）。移动端同构，仅宽度变化。

### 3.2 登录后：右上角用户菜单

```text
[桌面 sm+]  StockMaster  A股日线量化分析工作台  总览 … 设置 [工作区] [用户]  [头像 昵称 ▾]
                                                                  ├ 管理员 / 普通用户
                                                                  ├ 账号设置 → /account
                                                                  ├ 用户管理 → /users（仅 admin）
                                                                  └ 退出登录 → 清 token 回 /login
[移动 <sm]  顶栏第一行右侧 [头像 昵称 ▾]；导航横向滚动，不折叠为汉堡菜单
```

未登录 + 门禁开显示 `.btn-primary`「登录」；未登录 + 门禁关显示次按钮「账号入口」（【增量】加 `title`
说明本机模式）；已登录 + admin 额外有「用户管理」。【增量】菜单加只读角色行。

### 3.3 入口可见性矩阵【现状】

| 条件 | `/login` | 注册 Tab | 微信按钮 | 用户菜单 | `/account` | `/users` |
|---|---|---|---|---|---|---|
| 匿名 + auth_enabled=true | 被重定向进入 | `allow_register` | `wechat_enabled` | 仅“登录” | 重定向登录 | 重定向登录 |
| 匿名 + auth_enabled=false | 可直接访问 | `allow_register` | `wechat_enabled` | “账号入口” | 可访问（未登录占位） | 可访问（本机管理员） |
| 已登录 + user | 自动跳走 | — | — | 完整菜单 | 可访问 | 路由不注册，`*` 回落 `/` |
| 已登录 + admin | 自动跳走 | — | — | 完整菜单 + 用户管理 | 可访问 | 可访问 |

---

## 4. 页面设计

> **线框约定**：沿用 [UI_DESIGN.md](UI_DESIGN.md) §8 图例（`[按钮]`、`(○)单选`、`[x]复选`、
> `▾下拉`、`⌕搜索`、`⋯更多`、`*必填`、`~截断`、`→跳转`）。本册补充 `[____ 占位文案]` 表示文本输入框，
> `⚠` 表示提示/错误条。线框只表达层级与位置。

### 4.1 登录页

```text
[登录页 · 桌面 ≥960px · 居中 400px 卡片]
┌ StockMaster · A股日线量化分析工作台
├ ⚠ 服务器未开启登录校验（SM_AUTH_ENABLED=false）……      ← 仅门禁关闭
├ ⚠ 登录失败次数过多，请 N 分钟后再试                     ← 仅 429
├ [  登录  ] [  注册  ]                                   ← 仅 allow_register
├ 用户名 / 邮箱 *
│  [____ ⌕ 字母或数字开头，3-64 位                     ]
├ 密码 *
│  [____ ••••••••                             ] [显示]    ← [显示]【增量】
├ ⚠ 用户名或密码不正确                                    ← role="alert"
├ [                   登录                   ]            ← .btn-primary
├ ───────────────  或  ───────────────
├ [  微信扫码登录  ]                                      ← 仅 wechat_enabled
│  当前为模拟微信授权（SM_WECHAT_LOGIN_MODE=mock）……      ← 仅 mock
├ 系统还没有账号，第一个注册的用户将成为管理员。           ← 仅 !has_users
└ 本工作台仅供研究参考，不构成投资建议
```

```text
[登录页 · 移动 ≈390px]
┌ StockMaster · A股日线量化分析工作台
├ ⚠ 门禁关闭提示（最多 3 行）
├ [ 登录 ] [ 注册 ]
├ 用户名 / 邮箱 *
│  [____ 16px 字号防 iOS 缩放              ]
├ 密码 *
│  [____ ••••••••                  ] [显示]
├ ⚠ 错误提示
├ [             登录             ]
├ ─────  或  ─────
├ [        微信扫码登录         ]
└ 免责声明
```

**字段与校验**

| 字段 | 控件 | 现状约束 | 【增量】交互校验 | 失败文案 |
|---|---|---|---|---|
| 用户名 / 邮箱 `*` | text，`autocomplete=username` | 前端必填、min 3、max 64；后端 `LoginIn` 1–255，按用户名或邮箱匹配 | trim；邮箱形态校验基本格式；登录模式 `maxLength=255` | 空：请输入用户名或邮箱；格式：邮箱格式不正确 |
| 密码 `*` | password，`autocomplete=current-password` | 必填、max 128 | 显示/隐藏（`aria-pressed`）；提交中禁用 | 空：请输入密码；401：用户名或密码不正确 |
| 记住我 | — | 无（token 7 天 + localStorage） | **不做**（是否改会话级见 §10） | — |

**按钮与反馈**

| 控件 | 类型 | 行为 | 忙碌/禁用 | 反馈 |
|---|---|---|---|---|
| 登录 | `.btn-primary` 通栏 | `auth.login()` → `navigate(next)` | “登录中…”，disabled | 失败：字段下方 `role="alert"`；429 附等待时间 |
| 登录/注册 Tab | 分段控件 | 切换视图并清错误；保留用户名/密码 | — | — |
| 微信扫码登录 | 绿色 `#07c160` 通栏 | `api.wechatUrl(next)` → `location.assign(url)` | “跳转中…”，disabled | 失败：错误条；off 时不渲染 |
| 直接进入工作台 → | 文本链接（仅门禁关） | 跳 `/` | — | — |

**状态**

| 状态 | 触发 | 表现【增量】 |
|---|---|---|
| 加载中 | `App.init()` 未完成 | 全屏“正在加载…”，不闪出空表单 |
| 配置获取失败 | `/auth/config` 异常 | 黄条“无法获取登录配置，请检查后端连接 [重试]”；仍可密码登录 |
| 门禁关闭 | `auth_enabled=false` | amber 横幅 + 页脚直达；仍显示登录表单 |
| 注册关闭 | `allow_register=false` | Tab 隐藏；若 `has_users=false` 追加“无账号且注册关闭，请用 `SM_AUTH_ADMIN_USERNAME` 或 CLI 创建管理员” |
| 无账号 | `has_users=false && auth_enabled` | 首用户管理员提示；默认选中注册 |
| 微信号回调错误 | `/login?error=…` | 错误条展示；取消（中性）与失败（错误色）区分 |

**文案**：门禁关闭“服务器未开启登录校验（SM_AUTH_ENABLED=false），可直接进入工作台。登录后仍可使用
账号相关功能。”；无账号“系统还没有账号，第一个注册的用户将成为管理员。”；通用失败“登录失败：用户名或
密码不正确。请检查后重试。”；禁用“该账号已被禁用，请联系管理员。”；限流“登录尝试过于频繁，请 N 分钟后
再试。”；注册关闭“本系统已关闭自助注册，请联系管理员开通账号。”；不可达“无法连接服务器，请确认后端已
启动后重试。”；微信取消/失败“已取消微信授权。/ 微信登录失败：<原因>，可改用用户名密码登录。”

### 4.2 注册页

**推荐方案【增量】**：保持同一路由 `/login`，用 Tab 切换，但注册是**字段集独立的视图**（不是在同一表单
上加字段），支持深链 `/login?mode=register`：首次部署（`has_users=false && allow_register=true`）默认落在
注册视图；切 Tab 保留“用户名/密码”，清空注册专有字段错误；不新增 `/register` 路由（避免与 `next` 两套
逻辑重复）。理由：现有习惯与文档链接不变；深链可被部署文档引用；后端无需新增端点。

```text
[注册视图 · 桌面 ≥960px]
┌ StockMaster
├ ⚠ 首个账号将成为管理员，请妥善保管密码            ← 仅 has_users=false
├ [  登录  ] [  注册  ]                               ← 注册选中
├ 用户名 *        [____ 3-64 位，字母/数字开头]
│  仅字母、数字、点、下划线、连字符
├ 昵称（可选）    [____ 默认同用户名]
├ 邮箱（可选）    [____ 用于登录，不用于找回]        ← 文案修正【增量】
├ 密码 *          [____ 至少 8 位，需含字母和数字] [显示]
│  规则：≥8 位 · 含字母 · 含数字 · 无首尾空白
├ 确认密码 *      [____ 再次输入]                    ← 【增量】
├ ⚠ 字段级错误贴在对应字段下方
├ [           注册并登录           ]                 ← .btn-primary
└ 已有账号？直接登录
```

```text
[注册视图 · 移动 ≈390px]
├ [ 登录 ] [ 注册 ]
├ 用户名 * [____]    昵称（可选）[____]
├ 邮箱（可选）[____] 密码 * [____][显示]
│  规则清单（两行）
├ 确认密码 * [____]
├ ⚠ 错误提示
├ [        注册并登录        ]
└ 已有账号？直接登录
```

**字段与真实规则**（来源：`schemas/auth.py::RegisterIn`、`passwords.py`、`service.py`）

| 字段 | 现状约束 | 服务端错误（真实） | 【增量】前端校验 |
|---|---|---|---|
| 用户名 `*` | 3–64；正则 `^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$`；保留名 admin/root/system/stockmaster（仅库中有用户时） | 格式：400 `auth_error`“用户名需 3-64 位，以字母或数字开头，仅含字母、数字、点、下划线和连字符”；重名：409 `username_taken`“用户名 X 已存在” | 即时校验正则与长度；保留名提前提示 |
| 昵称 | ≤64，服务端默认取用户名并截断 | 无 | 提示“留空则使用用户名” |
| 邮箱 | 可空；≤255；含 `@`、不以 `@` 开头/结尾；转小写 | 400 `auth_error`“邮箱格式不正确”；409 `email_taken`“邮箱 X 已被使用” | 格式校验；说明仅用于登录 |
| 密码 `*` | Pydantic 8–128；策略：8–128、无首尾空白、同时含字母与数字 | 长度：422 英文；策略：**注册路径未捕获 → 500**（§10.2） | 规则清单实时校验，不满足不提交 |
| 确认密码 `*` | 现状无此字段 | — | 新增前端字段，不传后端 |

422 现状：`api.ts` 拼成 `username: String should have at least 3 characters`。【增量】按 `loc` 映射字段并译中文。

**冲突与关闭态**

| 场景 | HTTP / code | 位置 | 文案 |
|---|---|---|---|
| 用户名重复 | 409 `username_taken` | 用户名字段 | 该用户名已被使用，请换一个。 |
| 邮箱重复 | 409 `email_taken` | 邮箱字段 | 该邮箱已绑定其他账号，请直接登录或换一个邮箱。 |
| 注册关闭 | 403（无 code） | 替换表单 | 本系统已关闭自助注册，请联系管理员开通账号。[返回登录] |
| 保留名 | 400 `auth_error` | 用户名字段 | 该名称为系统保留名，请换一个。 |
| 弱密码 | 422 / 500（现状） | 密码字段 | 密码至少 8 位，且需同时包含字母和数字。 |
| 网络失败 | — | 表单顶部 | 无法连接服务器，请稍后重试。输入已保留。 |

**成功后**：201 返回 `TokenOut`，前端 `applyToken` 并 `navigate(next)`；首账号 `role=admin`，菜单出现“用户管理”。

**状态**：提交中按钮“注册中…”并禁用；失败字段级展示；成功后立即跳转，无需二次确认。

### 4.3 微信扫码登录与绑定

| 模式 | 授权地址（代码常量） | scope | 终端 | 界面现状 | 【增量】提示 |
|---|---|---|---|---|---|
| `open` | `open.weixin.qq.com/connect/qrconnect` | snsapi_login | PC 扫码 | 整页跳转微信二维码页 | 按钮下“新页面扫码，完成后自动返回” |
| `mp` | `open.weixin.qq.com/connect/oauth2/authorize` | snsapi_userinfo | 微信内 H5 | 直接跳转 | 非微信 UA 提示“请在微信中打开本页” |
| `mock` | `/api/v1/auth/wechat/mock/authorize`（后端 HTML） | — | 本地开发 | 按钮 + 一行提示 | 黄字“不会访问真实微信；不同昵称=不同账号” |
| `off` | — | — | — | 按钮不渲染 | 账号页“服务端未配置微信登录” |

模式取 `GET /auth/config` 的 `wechat_mode`（已按凭据校验后的有效值）；`open`/`mp` 缺凭据时实际为 `off`，
前端只判断 `wechat_enabled` 与 `wechat_mode`。

**登录流程（现状）**：① 点击 → `GET /auth/wechat/url?redirect=<next>&bind=false`；② 后端建 `kind=oauth`
state（600s）返回 `{url,state,mode}`；③ `location.assign(url)`；④ 微信回调消费 state → 换 profile →
按 openid/unionid 找号或新建 → 建 `kind=ticket`（120s）→ 302 `/auth/callback?ticket=…&next=…`；
⑤ 前端 `useRef` 保证只交换一次 → `POST /auth/ticket` → 保存 token → `next`。
安全点：**token 从不进 URL**，URL 里只有一次性 ticket；回调页不加载第三方资源。

**绑定流程（已登录，`bind=true`）**

```text
/account 点“绑定微信” → api.wechatUrl("/account", true)（带 Authorization）
   ▼ GET /auth/wechat/url?redirect=/account&bind=true
   ├─ 无 token → 401“绑定微信前请先登录”
   └─ state.payload.bind_user_id = 当前账号
   ▼ 微信授权 → /auth/wechat/callback
   ├─ openid 已绑别人 → 409 identity_taken → /login?error=该微信已绑定其他账号
   ├─ 当前账号已绑同 openid → 幂等返回
   └─ 成功 → ticket + next=/account + bound=1
   ▼ /auth/callback → POST /auth/ticket → /account?bound=1
      └─ 绿色提示“微信已绑定，下次可直接扫码登录”并 refreshUser()
```

现状缺陷：绑定成功也换新 token（旧 token 未吊销）；绑定失败回 `/login` 丢失绑定语境 → 见 §10.1。

**跳转中 / 取消 / 失败**

| 阶段 | 现状 | 【增量】 |
|---|---|---|
| 点击到跳转前 | 按钮“跳转中…”，禁用 | 2 秒未离开提示“若未跳转，请检查浏览器是否拦截” |
| 微信侧取消 | `error=access_denied` → `/login?error=已取消微信授权` | 中性提示 + “重新扫码”，不标红 |
| state 失效/重放 | `/login?error=登录会话已失效…` 或“已被使用” | 追加“请重新点击微信登录” |
| 微信接口失败 | `/login?error=<errcode 说明>`（如 40029/42003/48001） | 归为“可重试 / 部署配置问题（联系管理员）” |
| ticket 交换失败 | 回调页红字 + 原因 + [返回登录] | 增加 [重新登录]；`replaceState` 清掉 URL 中 ticket |
| AppID/回调域配错 | 微信页报错，回不到 SPA | 管理员登录后提示检查 `SM_WECHAT_*` |

```text
[微信回调页 · 桌面/移动通用 · 居中 380px 卡片]
┌
│  ( spinner )   正在完成登录…                    ← loading
└
[失败态]
┌
│  ⚠ 登录失败                                     ← red-400
│  该登录链接已被使用，请重新登录
│  [ 重新登录 ]  [ 返回登录页 ]
└
```

**文案**：按钮“微信扫码登录”；跳转中“跳转中…”；mock“当前为模拟微信授权（SM_WECHAT_LOGIN_MODE=mock），
不会访问真实微信”；回调 loading“正在完成登录…”；缺 ticket“登录回调缺少 ticket 参数，请重新发起登录”；
ticket 失效“登录链接已失效或已被使用，请重新扫码。”；取消“已取消微信授权。”；
绑定成功“微信已绑定，下次可直接扫码登录。”；已绑他人“该微信已绑定其他账号，请先在原账号解绑。”

### 4.4 账号设置页

**【增量】信息架构**：身份摘要 → 基本资料 → 安全（密码）→ 第三方账号 → 我的数据概览。
先回答“我是谁”，再改资料与安全，最后只读统计。

```text
[账号设置 · 桌面 ≥1024px]
┌ 身份摘要
│ (头像) 昵称 [管理员]
│        username · email · 注册于 YYYY-MM-DD              [退出登录]
├ ⚠ 页内反馈区（成功/失败，aria-live="polite"）
├ ┌ 基本资料 ──────────────┐ ┌ 安全 ─────────────────────┐
│ │ 昵称 *   [____]        │ │ 当前方式：密码 / 微信      │
│ │ 邮箱     [____]        │ │ 原密码     [____]（有密码）│
│ │ [ 保存 ]               │ │ 新密码     [____] [显示]   │
│ └────────────────────────┘ │ 确认新密码 [____]          │
│                            │ 规则清单；[ 更新密码 ]     │
│                            └───────────────────────────┘
├ 第三方账号：微信 [已绑定 昵称 · 绑定于 …]            [解绑]
│             或 尚未绑定微信                          [绑定微信]
├ 我的数据概览（只读 · 仅 workspace_enabled）
│ [参与工作区 N 个] [个人工作区数据 N 条] [可访问数据合计 N 条]
│ 工作区 | 类型 | 我的角色 | 成员 | 数据量
└
```

```text
[账号设置 · 移动 ≈390px]
├ 身份摘要（头像 + 昵称 + 角色 + 退出）
├ ⚠ 反馈
├ 基本资料卡：昵称 [____] / 邮箱 [____] / [保存]
├ 安全卡：当前方式：微信（未设密码）
│   新密码 [____] / 确认新密码 [____] / [设置密码]
├ 第三方账号卡：微信 · 已绑定              [解绑]
├ 我的数据概览卡：badge 行 + 表格（.table-wrap 横滚）
└
```

**字段与校验**

| 区块 | 字段 | 现状约束 | 【增量】 |
|---|---|---|---|
| 基本资料 | 昵称 | ≤64，服务端 trim 截断；空串合法（显示回 username） | 不强制必填；保存成功页内提示 |
| 基本资料 | 邮箱 | 可空；≤255；格式；重复 409“邮箱已被使用”（无 code） | 字段级错误；说明“用于登录，不承诺找回密码” |
| 安全 | 原密码 | 仅 `has_password=true` 出现且必填；错 400“原密码不正确” | 失败聚焦原密码 |
| 安全 | 新密码 | 8–128 + 字母数字 + 无首尾空白；弱密码 422（中文） | 规则清单实时校验；与确认一致 |
| 安全 | 确认新密码 | 前端比对，不一致提示“两次输入的新密码不一致” | 保持；失焦即时提示 |
| 第三方 | 微信绑定 | 唯一登录方式时解绑 400 `last_login_method` | 绑定前说明；解绑确认见下 |
| 数据概览 | — | 只读；`workspace_enabled=false` 不查询 | 保持；空态文案 |

**数据概览状态**：加载显示骨架；失败“暂不可用 [重试]”；空态“还没有加入任何工作区。”；隔离关闭时说明全站共享。

**改密后的 token 语义（界面必须说明）**：`POST /auth/me/password` 递增 `token_version` 并返回**新 token**，
前端 `applyToken` 保持本标签页在线；**其他设备的旧 token 立即失效**（401 `token_revoked`）。
成功提示：“密码已更新。其他设备已退出登录，本页面保持在线。”

**微信绑定/解绑**

| 操作 | 前置 | 确认 | 成功 | 失败 |
|---|---|---|---|---|
| 绑定微信 | `wechat_enabled=true` | 跳转微信授权，无需二次确认 | 回 `/account?bound=1`，绿色提示 + 刷新 | 401/409/取消 → 现状回登录页；【增量】回账号页保留语境 |
| 解绑微信 | 存在 wechat identity | `window.confirm` →【增量】自定义确认框：“解绑后不能用该微信登录；若未设密码将无法登录” | 204，刷新列表，提示“已解绑” | 400 `last_login_method`：提示“这是唯一的登录方式，请先设置密码” |

【增量】当 `!has_password && identities.length <= 1` 时解绑按钮直接 disabled 并给原因；服务端仍需校验。

### 4.5 管理员用户管理页（仅账号生命周期增量）

> 工作区成员/通道/配额等见 [UI_WORKSPACE.md](UI_WORKSPACE.md)；这里只写创建账号、重置密码、
> 启用/禁用、角色、统计列、审计的 UI 增量。

```text
[用户管理 · 桌面 ≥960px]
┌ 用户管理 [共 N 个账号]        [⌕ 搜索用户名/昵称/邮箱] [刷新] [+ 新建账号]
├ ⚠ 页内反馈（已创建账号 X / 已重置密码 / 错误）
├ 表格（.table-wrap）
│ 用户          角色        状态     登录方式   最近登录  工作区  个人数据量  操作
│ 张三 我      [普通用户▾] [启用]   密码/微信  …         2 个    128 条     [⋯]
│ 李四         [管理员▾]  [已禁用]  微信       …         1 个    0 条       [⋯]
├ 分页 ‹ 1 2 3 ›  每页 20 ▾                        ← 【增量】现状固定 100 条无分页
└ 最近操作（审计）：时间 | action | 账号 | detail | IP        [按类型筛选 ▾]【增量】
```

```text
[用户管理 · 移动 ≈390px]
├ 标题 + 计数；[⌕ 搜索……] [+ 新建]
├ 卡片式列表（宽表也可横滚）
│ ┌ 张三 我 [管理员]
│ │ 用户名 · 邮箱
│ │ 状态 [启用]   角色 [普通用户 ▾]
│ │ 登录方式 密码/微信   最近登录 …
│ │ 工作区 2 · 个人数据 128
│ │ [重置密码] [⋯ 禁用/删除]
│ └
├ 分页 ‹ 1 2 ›；审计（折叠）
└
```

| 操作 | 现状 | 【增量】交互 | 文案 / 校验 |
|---|---|---|---|
| 新建账号 | 内联 6 列表单，密码 `type="text"` 明文 | 抽屉/弹层：用户名、昵称、邮箱、角色、密码模式 (○)手动/(○)自动生成；成功后“初始密码仅显示一次”可复制 | 用户名同注册规则；密码 8–128 字母+数字；重复 409；弱密码 422 |
| 重置密码 | 弹层明文输入 | 自动生成强密码（可复制）或手动；确认后强制全部会话失效 | “已重置 X 的密码，该账号需重新登录。” |
| 启用/禁用 | 状态 badge 直接切换，无确认 | 禁用确认：“禁用后 X 将立即退出登录且无法再登录；可随时恢复。”启用直接生效 | 服务端禁用即 `token_version += 1`；改自己 400“不能修改自己的角色或状态” |
| 角色 | 行内 select 直接提交 | 改动前确认；说明“管理员可管理全部账号与工作区”；自己不可改 | 最后一名启用管理员保护 400“至少需要保留一名启用的管理员” |
| 删除 | `window.confirm` | 二次确认 + 输入用户名；说明“不可撤销” | 不能删自己/最后管理员（服务端 400） |
| 统计列 | `workspace_count`/`owned_workspace_count`/`personal_data_total` 已返回 | 保持；`title` 解释口径；隔离关闭显示“—”而非 0 | 口径见 `docs/WORKSPACE.md` §5.2 |
| 审计 | 最近 30 条，无筛选 | 增加 action 筛选与“加载更多” | action 清单见 §7 |
| 搜索/分页 | 每次按键即请求；固定 100 条 | 300ms 防抖 + 清除；补分页（后端已支持 `page`/`page_size` ≤200） | 无结果空态“没有匹配的账号” |
| 加载/错误态 | 查询无占位、失败无展示 | 加载用骨架行；失败“用户列表加载失败 [重试]”；审计区独立失败 | — |

**安全提示**：【现状】门禁关闭时匿名即可打开 `/users`（服务端 `local_guest_admin`，id=0）。
【增量】顶部 amber 条：“当前未开启登录校验，任何能访问本机的人都可以管理账号。对外暴露前请设置
`SM_AUTH_ENABLED=true`。”

---

## 5. 状态与开关矩阵

矩阵按「配置开关 × 登录态 × 角色」组合；前端只按 `GET /auth/config` 与 `user` 派生，不各自判断。

| # | auth_enabled | allow_register | wechat_mode | has_users | 登录态 | 角色 | 页面表现 / 可用操作 |
|---|---|---|---|---|---|---|---|
| 1 | false | true | off | false | 匿名 | — | 工作台直接可用；登录页黄条 + 双 Tab；首个注册者成 admin；`/users` 匿名可管账号 |
| 2 | false | false | off | false | 匿名 | — | 工作台可用；无 Tab 仅登录；提示“无账号且注册关闭，请用 `SM_AUTH_ADMIN_USERNAME`/CLI 创建” |
| 3 | false | true | mock | true | 匿名 | — | 工作台可用；可登录/注册；微信按钮 + mock 提示；右上角“账号入口” |
| 4 | true | true | off | false | 匿名 | — | 强制跳 `/login?next=…`；默认注册视图；首用户管理员提示；无微信按钮 |
| 5 | true | true | off | true | 匿名 | — | 强制登录；登录/注册双 Tab；无首用户提示 |
| 6 | true | false | off | true | 匿名 | — | 强制登录；仅登录；注册接口 403；不显示注册入口 |
| 7 | true | false | off | false | 匿名 | — | **死锁态**：红色说明 + 部署指引，不能只留一个必然失败的登录框 |
| 8 | true | true | open/mp（有凭据） | 任意 | 匿名 | — | 密码 + 微信；open 提示扫码，mp 提示微信内打开 |
| 9 | true | true | mock | 任意 | 匿名 | — | 微信按钮 + 黄色 mock 说明；可创建 mock 账号 |
| 10 | true | 任意 | 任意 | 任意 | 已登录 | user | 工作台；无“用户管理”；直接 `/users` 被 `*` 回落 `/`；接口 403 `forbidden` |
| 11 | true | 任意 | 任意 | 任意 | 已登录 | admin | 菜单含“用户管理”；`/users` 全部操作可用 |
| 12 | false | 任意 | 任意 | 任意 | 已登录 | user | 工作台 + 账号设置；无用户管理（`canManageUsers` false） |
| 13 | false | 任意 | 任意 | 任意 | 已登录 | admin | 完整菜单；`/users` 正常 |
| 14 | true | 任意 | 任意 | 任意 | token 过期 | — | 401 `expired` → 清 token → `/login?next=…&reason=expired`【增量】 |
| 15 | true | 任意 | 任意 | 任意 | token 被改密/登出撤销 | — | 401 `token_revoked` → 提示“登录状态已失效，请重新登录”【增量】 |
| 16 | true | 任意 | 任意 | 任意 | 账号被禁用 | — | 旧 token 因 `token_version` 递增表现为 401 `token_revoked`；重新登录才见 403 `user_disabled` |
| 17 | 任意 | 任意 | 任意 | 任意 | 已登录 | 任意 | `/account` 且 `workspace_enabled=false`：数据概览显示“未启用数据隔离”，不发请求 |
| 18 | 任意 | 任意 | off | 任意 | 已登录 | 任意 | `/account` 第三方账号卡“服务端未配置微信登录”，绑定按钮 disabled |

**初始化降级【增量】**：`/auth/config` 失败 → 黄条提示，工作台仍渲染只读壳，写请求失败给统一提示；
有 token 但 `/auth/me` 失败 → 区分 `expired`/`token_revoked`/`user_disabled`，跳登录并展示一次性原因；
任一业务请求 401 → 记录 reason 后跳登录。

---

## 6. API 映射与错误码

### 6.1 端点 → 前端调用点

| 方法 + 路径 | 认证 | 成功 | 失败（HTTP / code） | 前端调用点 |
|---|---|---|---|---|
| `GET /auth/config` | 公开 | 200 `AuthConfig` | 网络/5xx | `auth.ts::init` → `api.authConfig()` |
| `POST /auth/register` | 公开 | 201 `TokenOut` | 400 `auth_error`；403（无 code）；409 `username_taken`/`email_taken`；422 英文；500 弱密码缺陷 | `Login.tsx` → `api.register()` |
| `POST /auth/login` | 公开 | 200 `TokenOut` | 401 `bad_credentials`；403 `user_disabled`；429 `too_many_attempts` | `Login.tsx` → `auth.login()` |
| `POST /auth/logout` | 可选 Bearer | 204 | token 已失效也返回 204（`current_user` 可选） | `UserMenu`、`Account.tsx` → `auth.logout()` |
| `GET /auth/me` | Bearer | 200 `UserOut` | 401 `expired`/`token_revoked`/`user_missing`/`bad_signature` | `auth.ts::init/refreshUser` |
| `PATCH /auth/me` | Bearer | 200 `UserOut` | 401；409（无 code）；422 | `Account.tsx::saveProfile` |
| `POST /auth/me/password` | Bearer | 200 `TokenOut`（新 token） | 400 无 code；401；422 无 code | `Account.tsx::changePassword` |
| `GET /auth/me/identities` | Bearer | 200 `IdentityOut[]` | 401 | `api.identities()`（现状未使用） |
| `DELETE /auth/me/identities/{id}` | Bearer | 204 | 400 `last_login_method`；401；404 `not_found` | `Account.tsx::unbind` |
| `GET /auth/wechat/url` | 公开；bind 需 Bearer | 200 `WechatUrlOut` | 400（off，无 code）；401（bind 未登录） | `Login.tsx::startWechat`、`Account.tsx::bindWechat` |
| `GET /auth/wechat/mock/authorize` | 公开 | 200 HTML | 404（非 mock） | 浏览器整页跳转（非 XHR） |
| `GET /auth/wechat/callback` | 公开 | 302 → SPA | 302 → `/login?error=…`（导航呈现，非 JSON） | 浏览器整页跳转 |
| `POST /auth/ticket` | 公开 | 200 `TokenOut` | 400 `flow_state_invalid`；401（账号不存在，无 code）；422 长度 | `AuthCallback.tsx` → `api.exchangeTicket()` |
| `GET /users` | 管理员 | 200 `UserPage` | 401/403 `forbidden` | `Users.tsx` → `api.users()` |
| `POST /users` | 管理员 | 201 `UserOut` | 400 `auth_error`；401/403；409；422 中文 | `Users.tsx::CreateForm` |
| `PATCH /users/{id}` | 管理员 | 200 `UserOut` | 400 无 code；401/403；404；409 无 code；422 | `Users.tsx::patchUser` |
| `POST /users/{id}/password` | 管理员 | 204 | 401/403；404；422 无 code | `Users.tsx::PasswordDialog` |
| `DELETE /users/{id}` | 管理员 | 204 | 400 无 code；401/403；404 | `Users.tsx::removeUser` |
| `GET /users/audit-logs` | 管理员 | 200 `AuditLogOut[]` | 401/403 | `Users.tsx::logs` → `api.auditLogs(30)` |

`TokenOut` = `{token, token_type:"bearer", expires_at, user}`；`UserOut` 含
`id/username/email/nickname/avatar_url/role/status/last_login_at/created_at/has_password/identities`。

### 6.2 错误 → 用户文案映射

| 来源 | 识别 | 展示文案【增量】 | 处理动作 |
|---|---|---|---|
| 401 `bad_credentials` | 响应头 | 用户名或密码不正确，请检查后重试。 | 保留用户名，聚焦密码 |
| 403 `user_disabled` | 响应头 | 该账号已被禁用，请联系管理员。 | 禁用提交 + 联系管理员说明 |
| 429 `too_many_attempts` | 响应头 | 登录尝试过于频繁，请 N 分钟后再试。 | 按钮倒计时禁用（N 取 detail） |
| 409 `username_taken` / `email_taken` | 响应头 | 该用户名已被使用。/ 该邮箱已绑定其他账号。 | 聚焦对应用户名/邮箱 |
| 400 `flow_state_invalid` | 响应头 | 登录链接已失效或已被使用，请重新登录。 | 回登录页并清 ticket |
| 409 `identity_taken` | 响应头 | 该微信已绑定其他账号，请先在原账号解绑。 | 账号页提示 |
| 400 `last_login_method` | 响应头 | 这是账号唯一的登录方式，请先设置密码再解绑。 | 高亮“设置密码” |
| 404 `not_found` | 响应头 | 绑定不存在或已解绑，请刷新后重试。 | 刷新 identities |
| 401 `unauthorized` | 响应头 | 请先登录。 | 跳登录带 next |
| 401 `token_revoked` / `expired` / `user_missing` / 其余 token 原因 | 响应头 | 登录状态已失效，请重新登录。/ 登录已过期，请重新登录。/ 账号不存在，可能已被删除。/ 登录凭证无效。 | 清 token；带 reason 跳登录 |
| 403 `forbidden` | 响应头 | 需要管理员权限。 | 回首页，不显示用户管理 |
| 403 注册关闭（无 code） | 状态码 | 本系统已关闭自助注册，请联系管理员开通账号。 | 切换登录视图 |
| 409 改邮箱（无 code） | 状态码 | 该邮箱已被其他账号使用。 | 字段级 |
| 400 原密码（无 code） | 状态码 | 原密码不正确。 | 聚焦原密码 |
| 400 `auth_error` | 响应头 | 直接展示后端中文 detail。 | 字段级定位 |
| 422 | 状态码 | 按 `loc` 映射字段并译中文；否则“输入有误，请检查后重试”。 | 字段级 |
| 401 中间件拦截 | body `code` | 按 code 走同表；读不到则用 detail | 清 token 跳登录 |
| 网络/5xx | fetch/状态码 | 无法连接服务器，请稍后重试。 | 保留输入，提供重试 |

**实现要点【增量】**：`ApiError` 增加 `code`/`fieldErrors`；优先读响应头 `X-Auth-Error`，其次读 body `code`
（中间件形态），都没有回退 `detail`。开发跨端口时自定义响应头默认不可读，后端 `CORSMiddleware` 需补
`expose_headers=["X-Auth-Error"]`（【拟新增】，仅影响开发联调）。

### 6.3 拟新增错误码（`X-Auth-Error` 只加头，不影响既有客户端）

| code | HTTP | 场景 | 现状 |
|---|---|---|---|
| `registration_disabled`【拟新增】 | 403 | 注册关闭 | 403 无 code |
| `old_password_wrong`【拟新增】 | 400 | 改密原密码错 | 400 无 code |
| `weak_password`【拟新增】 | 422 | 注册/改密/重置弱密码 | 422 无 code；注册路径 500（需同时修复） |
| `email_taken`【拟新增】 | 409 | `PATCH /auth/me` 与管理员改邮箱 | 409 无 code；复用已有名称只补头 |

---

## 7. 权限与安全

**边界原则**：前端隐藏不是安全边界，`canManageUsers`/disabled 只减少误操作；所有端点以
`require_user`/`require_admin`/中间件为准，403/404 必须处理。`/users` 路由级依赖 `Depends(require_admin)`，
新增端点自动受保护。门禁关闭 ≠ 无账号体系：匿名可管理账号是本机模式的已知取舍，界面必须明示（§4.5）。

**令牌存储与失效**

| 项 | 现状 | 风险/说明 |
|---|---|---|
| 存储 | `localStorage["sm.auth.token"]` | 非 httpOnly，XSS 可窃取；选它是为避免 Cookie CSRF |
| 传输 | `Authorization: Bearer <token>`，生产仅 HTTPS | 不得进 URL/查询串/日志 |
| 有效期 | 默认 7 天；无刷新令牌 | 过期即重登 |
| 前端失效 | 仅 401 清 token；403 不清 | 【增量】纳入 403 `user_disabled` |
| CSRF | 无 Cookie，天然免疫 | 保持 |
| 签名 | 自签 HS256，密钥 `.auth_secret` 或环境变量 | 多实例须同密钥；备份含密钥等同凭据 |

| 动作 | 机制 | 其他设备 | 当前设备 |
|---|---|---|---|
| 退出登录 | `token_version += 1`（所有设备） | 401 `token_revoked` | 前端清 token |
| 自助改密 | `token_version += 1` + 返回新 token | 401 `token_revoked` | 应用新 token 保持在线 |
| 管理员重置密码 | `set_password()` → `token_version += 1` | 401 `token_revoked` | 不改变管理员自身 |
| 禁用账号 | `token_version += 1` | 401 `token_revoked` | 重登得 403 `user_disabled` |
| 修改角色 | **不递增** | token 仍有效；服务端按 DB 角色鉴权 | 前端 `user.role` 可能陈旧，需 `refreshUser` |
| 删除账号 | 记录消失 | 401 `user_missing` | 同上 |

【增量】401 记录 `expired`/`token_revoked`/`user_missing` 并在登录页提示；403 `user_disabled` 按会话失效处理。

**微信 state / ticket 一次性**

| 对象 | 生成 | 有效期 | 消费 | 重放结果 |
|---|---|---|---|---|
| OAuth `state` | `GET /auth/wechat/url` | 600s | `take_flow_state(kind="oauth")` | 400 `flow_state_invalid`“已被使用” |
| 登录 ticket | 回调成功后创建 | 120s | `POST /auth/ticket`，前端 `useRef` 一次 | 400 `flow_state_invalid` |
| 绑定目标 | state `payload.bind_user_id` | 同 state | 回调按 id 绑定 | 目标不存在 → `/login?error=绑定目标账号不存在` |

`redirect` 经 `safe_redirect()`：只接受站内 `/…`，拒绝绝对 URL、`//`、反斜杠，默认 `/`；回调失败一律重定向
回 SPA 带可读 `error`，不返回 JSON；启动时 `purge_flow_states()` 清理 24h 前记录。

**审计与防暴破**：真实审计 action 有 `user.register`、`login.success`、`login.failed`、`login.ticket`、`logout`、
`password.change`、`wechat.register`、`wechat.bind`、`wechat.unbind`、`user.create`、`user.update`、
`user.password_reset`、`user.delete`、`user.bootstrap`；用户页只展示最近 30 条。登录限流为进程内滑动窗口，
键 `(identifier.lower(), ip)`，多 worker 不共享、重启清零，只是防线之一。防枚举：未知用户与错密码同文案且
总执行一次哈希比对；但注册 409 会暴露用户名/邮箱存在（可用性权衡）。

---

## 8. 响应式与可达性

| 断点 | 登录/注册 | 账号设置 | 用户管理 |
|---|---|---|---|
| `<640px` | 单列，卡片 100%，输入 16px | 单列卡片；数据表 `.table-wrap` 横滚 | 卡片列表（推荐）或表格横滚 |
| `≥640px` | 卡片 400px 居中 | 单列加宽 | 表格 |
| `≥1024px` | 同上 | 资料/安全双栏 | 表格 + 工具条单行 |
| `≥1280px` | 同上 | 双栏 + 数据概览通栏 | 同上 |

| 主题 | 要求 | 现状 | 目标 |
|---|---|---|---|
| 表单 label | `<label htmlFor>` 或 `aria-label` | 登录页有；账号页/用户页多数缺失 | 全部补齐 |
| 错误播报 | `role="alert"`/`aria-live="polite"` | 无 | 表单级 + 字段级 |
| 焦点 | 失败聚焦首个错误字段；弹层打开进首控件、关闭回触发元素；`Esc` 关闭 | 重置弹层仅 autoFocus | 统一实现；用户菜单改 `button`+`aria-expanded` |
| 键盘 | 全流程可 Tab；Enter 提交；Esc 取消 | Tab 可用；Esc 未实现 | 补齐 |
| 自动填充 | username / current-password / new-password / email | 登录页已设；账号页部分 | 资料邮箱 `email`；改密三框 `new-password` |
| 输入字号 | 移动端 ≥16px | `index.css` 全局处理 | 保持 |
| 触控 | ≥32px，主按钮 ≥40px | 行内按钮偏小 | 操作改 `⋯` 菜单 + 大触控区 |
| 对比度/颜色 | 正文与错误 ≥4.5:1；状态不只用颜色 | slate 组合基本满足 | 错误用 red-300；禁用加文字 |
| 密码可见/弹层语义 | `aria-pressed`；`role="dialog"` + 标题关联 | 无 | 新增 |
| 动效 | 尊重 `prefers-reduced-motion` | 仅 spinner | 新动效加保护 |

---

## 9. 实施拆分与验收

### 9.1 拆分步骤

| 步骤 | 主要文件 | 改动点 | 依赖 |
|---|---|---|---|
| **S1 错误归一化** | `frontend/src/api.ts`、`types.ts`、`src/stockmaster/main.py`（CORS expose） | `ApiError` 加 `code/fieldErrors`；解析 header 与 body `code`；422 loc 映射 | 无 |
| **S2 登录/注册拆分** | `pages/Login.tsx`（可拆 `pages/auth/LoginForm.tsx`、`RegisterForm.tsx`）、`auth.ts` | 独立字段集；`?mode=register`；字段级校验；门禁/注册关闭/首用户/配置失败态；密码显示；文案落地 | S1 |
| **S3 微信链路** | `Login.tsx`、`AuthCallback.tsx`、`Account.tsx` | 三模式提示；取消/失败分流；回调页错误分类与“重新登录”；清 URL ticket | S1 |
| **S4 账号设置页** | `pages/Account.tsx` | 信息架构重排；labels/焦点/aria-live；改密影响面；解绑前置禁用；数据概览三态 | S1 |
| **S5 用户管理增量** | `pages/Users.tsx`、`App.tsx` | 分页、防抖、错误态；创建/重置弹层（生成+一次性展示）；启停/角色/删除确认；本机模式提示；用户菜单可访问性 | S1 |
| **S6 后端错误码补丁【拟新增】** | `routers/auth.py`、`routers/users.py`、`services/service.py` | 捕获 `WeakPasswordError` → 422 `weak_password`；注册关闭加码；改密/邮箱冲突补 code；CORS expose | 可与 S1 并行 |

### 9.2 验收标准

**后端（`tests/test_auth.py` 新增用例）**

| 用例名（建议） | 断言 |
|---|---|
| `test_register_weak_policy_password_returns_422` | `password="12345678"` → 422 且 `X-Auth-Error: weak_password`（现状 500） |
| `test_registration_disabled_exposes_error_code` | 关闭注册 → 403 且头为 `registration_disabled` |
| `test_error_codes_are_readable_from_header` | `bad_credentials`/`too_many_attempts`/`username_taken`/`email_taken` 均有头 |
| `test_disabled_account_existing_token_returns_token_revoked` | 禁用后旧 token 401 `token_revoked`（固定现状语义） |
| `test_wechat_ticket_expired_message` | 过期/复用 ticket → 400 `flow_state_invalid` 且文案含“重新登录” |
| `test_password_change_returns_new_token_and_revokes_old` | 旧 token 401、新 token 200（现有用例补头断言） |
| `test_cors_exposes_auth_error_header` | 预检响应含 `Access-Control-Expose-Headers: X-Auth-Error` |

**前端**：仓库当前无前端测试运行器（`frontend/package.json` 无 test 脚本），P0 以手工清单验收；若引入
Vitest + Testing Library（**新增依赖，待决策**），建议用例名：`login.spec.tsx::失败保留用户名并聚焦密码`、
`login.spec.tsx::注册关闭时不渲染注册 Tab 并提示联系管理员`、`register.spec.tsx::弱密码前端拦截不发起请求`、
`callback.spec.tsx::ticket 仅交换一次且失败展示返回登录`、
`account.spec.tsx::改密成功后应用新 token 并提示其他设备下线`、`users.spec.tsx::禁用前必须确认且不能操作自己`。

**手工走查（每条须可观察）**

1. 门禁关闭：`/` 可用，右上角“账号入口”，`/login` 有黄条。
2. 空库 + 门禁开：访问 `/` 被带到注册视图，注册后进入工作台且菜单有“用户管理”。
3. 关闭注册：无注册入口；直接调接口得到 403 中文提示。
4. 弱密码（`12345678`、`abcdefgh`、带首尾空格）：前端拦截；绕过前端得到 422 中文而非 500。
5. mock 微信：确认 → 回调 loading → 进入工作台；取消 → 回登录页显示“已取消微信授权”。
6. 改密后其他标签页下一次请求被踢并说明原因，本页仍在线。
7. 仅微信账号解绑按钮禁用并提示先设密码；设密后可解绑。
8. 纯键盘完成登录与改密；错误后焦点在首个错误字段；Esc 关闭重置弹层。
9. 390px 视口：三页均无页面级横向滚动，输入框 16px。

---

## 10. 未决问题与已发现的不一致

### 10.1 待产品/工程决策

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| 1 | 是否做邮箱找回密码 | 登录页“忘记密码”体验；需邮件模板、重置 token 表、限流与审计 | 首版明确“联系管理员重置”，不放假流程；P2 再评估 |
| 2 | 登录限流的前端呈现 | 倒计时、是否允许换账号重试、是否暴露剩余秒数 | 显示“约 N 分钟”并禁用提交；后端已返回可读文案 |
| 3 | 管理员创建“仅微信”账号 | `UserCreateIn.password` 可为空，现状 UI 强制密码 | 增加“不设密码（仅微信登录）”需产品确认可发现性 |
| 4 | 初始密码手工 vs 自动生成 | 手工简单但易弱；生成安全但需一次性展示 | 默认自动生成，可切手工；只显示一次 |
| 5 | 注册是否独立路由 `/register` | 影响深链、埋点与文档 | 保持 `/login?mode=register`（推荐） |
| 6 | token 升级为 httpOnly Cookie + CSRF token | 抗 XSS；需改登录/登出接口与 CORS 凭据策略 | 独立安全议题，不混入本次 UI 改造 |
| 7 | 角色修改后是否强制重登 | 现状不递增 `token_version`，UI 可能短暂陈旧 | 服务端鉴权已正确；被改者下次 `/auth/me` 刷新即可 |
| 8 | 门禁关闭时是否允许匿名管理账号 | 本机便利 vs 误暴露 | 保留现状 + 显著警示；部署文档强调开门禁 |
| 9 | 绑定失败是否回账号页而非登录页 | 现状失败回 `/login?error=`，丢失绑定语境 | 回调支持 `next=/account` 并透传错误（【拟新增】小改动） |

### 10.2 已发现的代码/文档不一致（供 Lead 汇总）

| # | 不一致 | 证据 | 影响 |
|---|---|---|---|
| 1 | 管理员引导环境变量名对不上 | `config.py:100` 为 `auth_admin_username`（=`SM_AUTH_ADMIN_USERNAME`），但 `.env.example:76`、`README.md:416/448`、`docs/DEPLOY_TENCENT.md` 写 `SM_ADMIN_USERNAME` | 按文档配置不创建管理员，门禁开启后无人可登录 |
| 2 | 注册弱密码可能 500 | `auth.py::register` 只捕获 `AuthError`；`create_user()` 内 `validate_password` 抛 `WeakPasswordError(ValueError)` 未捕获 | 8 位纯数字等策略失败返回 500 而非 422 |
| 3 | [UI_DESIGN.md](UI_DESIGN.md) §6.4 示例 code 不存在 | 示例写 `invalid_credentials`/`registration_disabled`；实际为 `bad_credentials`，注册关闭 403 无 code | 实施以 §6.2 真实清单为准（总纲已注明“以分册核实为准”） |
| 4 | 邮箱用途文案与能力不符 | `Login.tsx:140`、`Account.tsx:158` 写“用于找回”；README §8 明确“无邮件/短信找回” | 用户误以为可自助找回；本册已改文案 |
| 5 | 文档写 React 18，实际 19 | [UI_DESIGN.md](UI_DESIGN.md) §2.1 写 React 18；`frontend/package.json` 为 `react@^19.3.0`、`react-router-dom@^7` | 影响升级/API 假设，建议总纲修正 |
| 6 | README 已知限制残留旧语义 | `README.md:547` 写“数据不按用户隔离”，但 `SM_DATA_ISOLATION` 与 `docs/WORKSPACE.md` 已提供隔离 | 与工作区分册冲突，建议更新 README |
| 7 | `X-Auth-Error` 前端从未使用、开发环境不可读 | `api.ts` 只读 `detail`；CORS 未 `expose_headers` | 错误无法分类；§6.2/§9 S1 覆盖 |
| 8 | 登录输入框长度与后端不一致 | `Login.tsx:119` `maxLength=64`；`LoginIn.username` 允许 255（邮箱登录） | 长邮箱输不全；§4.1 已修正 |
