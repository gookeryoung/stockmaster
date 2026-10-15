"""Remove build artifacts and caches, with an explicit allow-list of targets.

Written as an explicit list on purpose. A shell one-liner (`git clean -fdX` with
`-e` excludes) proved dangerous: the exclude patterns silently failed to apply and
the command deleted `.venv/`, `data/` (the research database), `frontend/dist/`
and `node_modules/`. Deleting the wrong thing here is expensive and irreversible,
so the target set is spelled out and anything not listed is never touched.

Usage:
    python scripts/clean.py [--dry-run]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Make `app` importable when run directly, then align output with the terminal so
# Chinese messages stay readable on a GBK console.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.console import prefer_console_encoding

prefer_console_encoding()

ROOT = Path(__file__).resolve().parents[1]

#: Build outputs and caches that are always safe to regenerate.
CACHE_DIRS = (
    "build",
    "dist",
    "wheels",
    "htmlcov",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".pyrefly_cache",
    ".coverage",
    "coverage.xml",
    "coverage.json",
    "backend/stockmaster.egg-info",
    "stockmaster.egg-info",
)

#: NEVER deleted, even though they match ignore rules — re-creating them is
#: costly (`data/` may hold gigabytes of synced bars and trained models).
PROTECTED = (
    ".venv",
    ".git",
    "data",
    "node_modules",
    "frontend/node_modules",
    "frontend/dist",
    "frontend/.vite",
    "uv.lock",
)


def _iter_pycache() -> list[Path]:
    """Every ``__pycache__`` directory under the source trees."""
    found: list[Path] = []
    for base in ("backend", "scripts"):
        root = ROOT / base
        if root.is_dir():
            found.extend(p for p in root.rglob("__pycache__") if p.is_dir())
    return found


def _is_protected(path: Path) -> bool:
    relative = path.relative_to(ROOT).as_posix()
    return any(relative == item or relative.startswith(f"{item}/") for item in PROTECTED)


def collect_targets() -> list[Path]:
    """Resolve the concrete paths to delete, skipping protected ones."""
    targets: list[Path] = []
    for entry in CACHE_DIRS:
        candidate = ROOT / entry
        if candidate.exists():
            targets.append(candidate)

    targets.extend(_iter_pycache())

    # .coverage* siblings (e.g. .coverage.hostname.pid)
    targets.extend(p for p in ROOT.glob(".coverage.*") if p.is_file())

    # Stray bytecode outside __pycache__.
    for base in ("backend", "scripts"):
        root = ROOT / base
        if root.is_dir():
            targets.extend(p for p in root.rglob("*") if p.is_file() and p.suffix in {".pyc", ".pyo"})

    unique: dict[str, Path] = {}
    for path in targets:
        if not _is_protected(path):
            unique[path.as_posix()] = path
    return sorted(unique.values())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="清理构建产物与缓存")
    parser.add_argument("--dry-run", action="store_true", help="仅列出将删除的路径")
    args = parser.parse_args(argv[1:])

    targets = collect_targets()
    if not targets:
        print("没有需要清理的内容")
        return 0

    total = 0
    for path in targets:
        if args.dry_run:
            print(f"  将删除 {path.relative_to(ROOT)}")
        else:
            shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
            print(f"  已删除 {path.relative_to(ROOT)}")
        total += 1

    verb = "可删除" if args.dry_run else "已清理"
    print(f"[clean] {verb} {total} 项；.venv / data / node_modules 始终保留")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
