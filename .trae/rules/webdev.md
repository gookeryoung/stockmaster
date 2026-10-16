---
alwaysApply: false
globs: frontend/**/*.ts, frontend/**/*.tsx, backend/**/*.py, frontend/**/*.d.ts
---
# Web 全栈开发规范

## 技术栈

- 包管理：前端 pnpm，后端 uv（**禁止** npm/yarn/pip 混用）。
- 前端：Vite + React + TypeScript strict；样式用 Tailwind CSS；状态用 zustand + React Query；图表用 ECharts。
- 后端：FastAPI, Python 3.13, Pydantic v2, SQLAlchemy 2.0, Alembic 迁移；DB: SQLite。
- 服务：uvicorn[standard] 启动（自带 uvloop）。

## 前端规则

- 优先用 Tailwind 原子类组合；复用组件放 `frontend/src/components/`，页面放 `frontend/src/pages/`。
- 需要自定义样式时写在 `frontend/src/index.css` 的类里，颜色/间距走主题变量，避免散落的硬编码色值。
- Vite dev server 用 proxy 代理 `/api` 与静态资源到后端；生产构建产物同步到后端 static 目录同源部署，无需 CORS。

## FastAPI 后端规则

- 全部使用 `async def`；必须声明 `response_model`；使用 `lifespan`，禁止 `@on_event`。
- 分层：api/routers/* → services/* → models/*；路由层禁止写 SQL / ORM 逻辑。
- 请求 / 响应全部用 Pydantic v2；配置用 `pydantic-settings`；字段名 / 必填 / 枚举以 FastAPI 为源。
- 鉴权：依赖注入认证后写入 `request.state.user_id`；支持 JWT / API Token（仅创建时明文展示，存储 SHA-256 哈希）。
- 日志用 `logging`，禁止 `print`；禁止裸 `except:`。
- 错误响应：业务校验失败返回 400 + 出错明细；服务层抛出的异常必须在路由层完成映射（避免 500）。
- 性能：DB 连接池；长任务用 BackgroundTasks / 任务表模式。

## 类型契约

- FastAPI 是类型真理源；修改 Pydantic schema 后必须同步更新前端 `frontend/src/types.ts` 的接口类型定义。
- 新增接口必须补：Pydantic schema + 前端类型 + API 测试。

> 目录约定：后端 `backend/app`，后端测试 `backend/tests`，前端 `frontend/`；与此不符时以项目内实际结构调整 glob 与路径。
