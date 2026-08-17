"""Run five reproducible end-to-end Stamford property activations.

The results are a presentation-safe, machine-readable validation artifact used
by the demo.  Each activation uses its own database; the municipality-wide
precomputed store is read only.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from property_intel.municipality import validate_sample


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "stamford_ct_pilot.json"
OUTPUT_ROOT = ROOT / "pilots" / "stamford_ct" / "sample_validation"
RESULT_PATH = ROOT / "pilots" / "stamford_ct" / "reports" / "stamford_ct_random_validation.json"
SEEDS = (17, 29, 43, 61, 79)


def compact(result: dict) -> dict:
    activation = result["activation"]
    return {
        "seed": result["sample_seed"],
        "candidate_count": result["candidate_count"],
        "property": result["selected"],
        "passed": bool(activation["validation"].get("passed")),
        "contradictions": activation["contradictions"],
        "semantic_stages": [
            {"stage": item.get("stage_key", item.get("stage")), "status": item.get("coverage_status", item.get("status"))}
            for item in activation["semantic_stages"]
        ],
        "on_demand_stages": [
            {"stage": item.get("stage"), "status": item.get("status")}
            for item in activation["on_demand_stages"]
        ],
        "report": activation["reports"]["json"],
    }


def main() -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    results = []
    for seed in SEEDS:
        try:
            result = validate_sample(
                CONFIG, seed=seed, force=True, skip_live=False,
                output_root=OUTPUT_ROOT,
            )
            results.append(compact(result))
        except Exception as exc:  # retain every attempted sample in the artifact
            results.append({"seed": seed, "passed": False, "error": str(exc)})
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "stamford_ct",
        "mode": "five reproducible randomized full activations",
        "samples": results,
        "passed": all(item.get("passed") for item in results),
    }
    RESULT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
