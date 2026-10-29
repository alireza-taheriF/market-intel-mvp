"""Persist runs as JSON so collected data survives failures and app restarts."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from market_intel.core.models import AnalysisResult

log = logging.getLogger(__name__)


def save_run(result: AnalysisResult, runs_dir: str) -> Path | None:
    try:
        path = Path(runs_dir)
        path.mkdir(parents=True, exist_ok=True)
        file = path / f"run-{result.run_id}.json"
        file.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return file
    except OSError as exc:
        log.warning("could not save run: %s", exc)
        return None


def list_runs(runs_dir: str) -> list[Path]:
    path = Path(runs_dir)
    return sorted(path.glob("run-*.json"), reverse=True) if path.exists() else []


def load_run(file: Path) -> AnalysisResult | None:
    try:
        return AnalysisResult.from_dict(json.loads(file.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("could not load %s: %s", file, exc)
        return None


def append_failure_log(runs_dir: str, entry: dict) -> None:
    try:
        path = Path(runs_dir)
        path.mkdir(parents=True, exist_ok=True)
        with open(path / "failure_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
