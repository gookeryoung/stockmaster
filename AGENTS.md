# 开发工作流

- 每次改完代码必须依次执行：`make check-fast` → `make test` → 项目文档更新 → `make push`
- 任一步失败就修，修完重跑，全过才算完成
- 禁止 `--no-verify`、禁止跳过 pre-commit
- commit message 用 conventional commits
