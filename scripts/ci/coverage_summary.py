#!/usr/bin/env python
"""把后端 / 前端的覆盖率产物汇总成一段 Markdown（用于 PR 评论与 Job Summary）。

用法：
    python scripts/ci/coverage_summary.py \
        --backend coverage.xml \
        --frontend frontend/coverage/coverage-summary.json \
        --output coverage-comment.md

只依赖标准库：CI 里不需要为「读两个数字」再装一个包。任何一侧缺失都降级为
「未生成」，而不是让整个评论任务失败——覆盖率反馈不该成为流水线的单点故障。
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MARKER = "<!-- coverage-report -->"


def format_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def backend_coverage(path: Path) -> dict[str, float] | None:
    """读取 coverage.py 生成的 Cobertura XML（含分行/分支统计）。"""
    if not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None

    # coverage.py 把总计写在根节点的属性上
    def rate(name: str) -> float | None:
        raw = root.get(name)
        try:
            return float(raw) * 100 if raw is not None else None
        except ValueError:
            return None

    return {
        "lines": rate("line-rate") or 0.0,
        "branches": rate("branch-rate") or 0.0,
    }


def frontend_coverage(path: Path) -> dict[str, float] | None:
    """读取 vitest（istanbul 格式）的 json-summary。"""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    total = data.get("total") or {}
    try:
        return {
            "lines": float((total.get("lines") or {}).get("pct", 0.0)),
            "branches": float((total.get("branches") or {}).get("pct", 0.0)),
        }
    except (TypeError, ValueError):
        return None


def build(backend: dict[str, float] | None, frontend: dict[str, float] | None) -> str:
    rows = [
        "| 范围 | 行覆盖 | 分支覆盖 |",
        "| --- | --- | --- |",
        f"| 后端（pytest + coverage.py） | {format_pct(backend['lines'] if backend else None)} | "
        f"{format_pct(backend['branches'] if backend else None)} |",
        f"| 前端（vitest + v8） | {format_pct(frontend['lines'] if frontend else None)} | "
        f"{format_pct(frontend['branches'] if frontend else None)} |",
    ]
    lines = [
        MARKER,
        "## 测试覆盖率",
        "",
        *rows,
        "",
        "> 后端报告见 `backend-coverage` artifact（HTML），前端见 `frontend-coverage` artifact。",
        "> 覆盖率门槛：后端 80%（`--cov-fail-under`），前端核心模块见 `frontend/vitest.config.ts`。",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default="coverage.xml", help="coverage.py 的 XML 报告")
    parser.add_argument("--frontend", default="frontend/coverage/coverage-summary.json")
    parser.add_argument("--output", default="", help="输出文件；留空则打印到 stdout")
    args = parser.parse_args(argv)

    markdown = build(backend_coverage(Path(args.backend)), frontend_coverage(Path(args.frontend)))
    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
    else:
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
