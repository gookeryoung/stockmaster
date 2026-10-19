更新日志
========

完整版本历史与分发包见 GitHub Releases：
https://github.com/gookeryoung/stockmaster/releases

v0.4.0
------

- 修复移除策略配置后预测页「即时自定义」链路中断：超参白名单校验
  （``PARAM_SPECS`` / ``validate_algo_params``）迁回
  ``plugins/predict/services/models.py``，``/predict/train`` 对未知或越界超参仍返回
  422；前端 view store 补回 ``features`` / ``params`` / ``testFraction`` 草稿字段，
  旧本地草稿缺字段时回落默认值。
- 修复日线数据拉取失败的两个根因：``job_sync_daily`` 默认宇宙不再要求「已有日线」
  （``_default_universe(require_bars=False)``），全新安装不再因空宇宙而永远拉不到
  第一根日线；日线任务固定 ``resume=False``，增量保持按标的自身最新日期续拉，
  避免断点游标把新交易日永久跳过。
- 统一各同步环节的结果提示：``SyncReport.summary()`` 输出中文「完成 / 部分完成 /
  失败」，交易日历、证券主数据、指数日线、个股日线任务返回 ``message`` 字段；
  空宇宙等可操作场景给出明确的下一步指引。
- 设置页新增「一键拉取日线数据」：按「日历 → 主数据 → 指数日线 → 个股日线」顺序
  逐环节调用现有任务接口，逐步实时显示每步的进行中 / 完成 / 失败与结果；单个环节
  失败不中断后续环节，便于一次定位问题。
- 版本号与 ``uv.lock`` 保持同步。

v0.3.0
------

- 工作区级通知通道：Webhook / Bark / 邮件（SMTP）按工作区路由投递，owner 管理、
  成员只读（地址脱敏）；全局 webhook 只服务未归属数据或显式 fallback。
- 按工作区消息模板：``workspaces.message_template``，支持
  ``{title} {body} {code} {rule_id} {level}``，非法模板 422 拒绝。
- 工作区用量统计与可选配额：``GET /workspaces/{id}/stats``、``SM_WORKSPACE_MAX_*``
  写路径超限 409；用户页数据概览、管理员用户统计列与「全部工作区」总览。
- 工作区级预警参数覆盖（只收紧：排除 ST / 最低成交额 / 冷却 / 每日一次），
  owner 配置、扫描与去重生效。
- 用量趋势：每日 ``workspace_usage`` 快照、``/stats/history`` 与 CSV ``/stats/export``。
- 明确取消「共享清单可见性细分」与「回测分享链接」两个方向。
- 版本号与 ``uv.lock`` 保持同步。

v0.2.0
------

- 账号体系：管理员/普通用户、PBKDF2 密码、自签 JWT（改密即失效）、
  微信登录（网站应用扫码 / 公众号 / mock 三模式）、用户管理页。
- 引入 Alembic 迁移链：存量库自动 stamp + 增量升级，``db`` 命令组运维入口。
- 系统备份与恢复：应用级 tar.gz 归档 + manifest、在线备份协议、
  恢复前自动预备份、CLI 与 Web 双通道。
- 工作区隔离（默认关闭）：``SM_DATA_ISOLATION=true`` 时数据按工作区归属，
  成员角色 owner/editor/viewer，viewer 只读（写操作 403 + 页面按钮禁用）；
  存量数据按用户自动回填个人工作区，无主数据用 ``workspace claim`` 移交。
- 版本号与 ``uv.lock`` 保持同步。

v0.1.4
------

- 控制台中文输出固定为 UTF-8，不再运行时探测代码页（CLI 子进程有回归测试守护）。
- 统一仪表盘指数清单；指数同步改走公共服务方法，消除重复实现。
- 修复 404 可操作性缺失与指数成分快照残留问题。
- 版本号与 ``uv.lock`` 保持同步。

v0.1.0
------

- 项目初始化：数据源抽象（baostock / 腾讯 / 新浪）、增量同步、技术指标、
  横截面因子、模型训练与回测、预警规则引擎、FastAPI 接口与 React 工作台。
