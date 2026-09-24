#!/usr/bin/env python3
"""Run a case through a decision client and apply the routing policy.

Usage:
  python3 harness.py run cases/incident-triage.json            # offline stub
  python3 harness.py run cases/incident-triage.json --live     # real API (TYPESAFE_API_KEY)
  python3 harness.py shadow shadow/sample-decisions.jsonl      # agreement report

`run` prints the raw answers and the policy decision. `shadow` reads a JSONL log
of past decisions (model choice + confidence + what the human did) and reports
agreement overall and per confidence bucket, which is how thresholds get tuned.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jevops.client import make_client
from jevops.policy import POLICIES


def run(path: str, live: bool) -> int:
    case = json.loads(Path(path).read_text())
    name = Path(path).stem
    client = make_client(live)
    response = client.evaluate(case["state"], case["questions"], case.get("model"))
    print(f"# case: {name}  model: {response['model']}")
    print(json.dumps(response["answers"], indent=2))
    policy = POLICIES.get(name)
    if policy is None:
        print(f"no policy registered for {name}", file=sys.stderr)
        return 1
    decision = policy(response["answers"])
    print(f"# decision: {decision.outcome}  ({decision.reason})")
    print(f"# handoff: {decision.handoff}")
    return 0


def shadow(path: str) -> int:
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    if not rows:
        print("empty log", file=sys.stderr)
        return 1
    buckets = {"<0.5": [], "0.5-0.8": [], ">=0.8": []}
    for row in rows:
        c = row["model_confidence"]
        key = "<0.5" if c < 0.5 else "0.5-0.8" if c < 0.8 else ">=0.8"
        buckets[key].append(row["model_choice"] == row["human_choice"])
    total = sum(len(v) for v in buckets.values())
    agreed = sum(sum(v) for v in buckets.values())
    print(f"# shadow report: {total} decisions, {agreed} agreed ({100 * agreed / total:.0f}%)")
    for key, values in buckets.items():
        if values:
            print(f"  confidence {key:>7}: {len(values):3d} decisions, {100 * sum(values) / len(values):3.0f}% agreement")
        else:
            print(f"  confidence {key:>7}:   0 decisions")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("case")
    p_run.add_argument("--live", action="store_true", help="call the real API instead of the stub")
    p_shadow = sub.add_parser("shadow")
    p_shadow.add_argument("log")
    args = parser.parse_args()
    if args.cmd == "run":
        return run(args.case, args.live)
    return shadow(args.log)


if __name__ == "__main__":
    sys.exit(main())
