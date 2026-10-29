"""Command-line demo of controlled failure handling.

    python scripts/simulate_failure.py               # all scenarios
    python scripts/simulate_failure.py "AI model unavailable"

Each scenario runs the real pipeline with an injected fault and prints the
resulting status and step log; results are appended to runs/failure_log.jsonl.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_intel.core.config import ALLOWED_TEST_RECIPIENT, Settings  # noqa: E402
from market_intel.core.faults import SCENARIOS  # noqa: E402
from market_intel.core.models import ProductInput, SalesMessage  # noqa: E402
from market_intel.messaging.email import send_test_email  # noqa: E402
from market_intel.pipeline import Pipeline  # noqa: E402
from market_intel.storage import append_failure_log  # noqa: E402

PRODUCT = ProductInput(name="Industrial Battery Energy Storage System (BESS)", target_region="Middle East")


def main(selected: list[str]) -> None:
    settings = Settings()
    for name, faults in SCENARIOS.items():
        if not faults.active or (selected and name not in selected):
            continue
        print(f"\n=== {name} ===")
        if faults.smtp_failure:
            msg = SalesMessage(company_name="Demo Co", subject="Demo", body="Test body " * 10)
            res = send_test_email(settings, msg, ALLOWED_TEST_RECIPIENT, approved=True, faults=faults)
            print(f"email sent: {res.ok} – {res.detail}")
            continue
        result = Pipeline(settings, faults).run(PRODUCT)
        for s in result.steps:
            print(f"  [{s.status:8}] {s.name}: {s.message[:110]}")
        print(f"  => run status: {result.status.upper()} (sources kept: {len(result.sources)})")
        append_failure_log(settings.runs_dir, {"at": result.created_at, "run_id": result.run_id,
                                               "scenario": name, "status": result.status, "errors": result.errors})


if __name__ == "__main__":
    main(sys.argv[1:])
