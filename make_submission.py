#!/usr/bin/env python3
"""Generate submission.jsonl from the 30 canonical test pairs (brief §7.2).

    .venv/bin/python make_submission.py

Writes one JSON object per line, in test_id order, with the keys the brief
specifies: test_id, body, cta, send_as, suppression_key, rationale. Also emits
template_name / template_params for the WhatsApp first-outbound template
requirement (§5 constraint 1), and re-runs the full validator over every line so
nothing ships that trips a hard rule.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from composer import compose
from facts import build_factset, validate

ROOT = Path(__file__).parent
D = ROOT / "dataset" / "expanded"
OUT = ROOT / "submission.jsonl"

# Keys the judge reads, in the order the brief lists them.
FIELDS = ("test_id", "body", "cta", "send_as", "suppression_key", "rationale",
          "template_name", "template_params")


def main() -> int:
    cats = {json.loads(p.read_text())["slug"]: json.loads(p.read_text())
            for p in (D / "categories").glob("*.json")}
    mers = {json.loads(p.read_text())["merchant_id"]: json.loads(p.read_text())
            for p in (D / "merchants").glob("*.json")}
    custs = {json.loads(p.read_text())["customer_id"]: json.loads(p.read_text())
             for p in (D / "customers").glob("*.json")}
    trgs = {json.loads(p.read_text())["id"]: json.loads(p.read_text())
            for p in (D / "triggers").glob("*.json")}
    pairs = json.loads((D / "test_pairs.json").read_text())["pairs"]

    lines: list[str] = []
    problems: list[str] = []
    seen_bodies: dict[str, str] = {}

    for pair in pairs:
        trigger = trgs[pair["trigger_id"]]
        merchant = mers[pair["merchant_id"]]
        customer = custs.get(pair.get("customer_id")) if pair.get("customer_id") else None
        category = cats[merchant["category_slug"]]

        composed = compose(category, merchant, trigger, customer, use_llm=False)

        fs = build_factset(category, merchant, trigger, customer)
        v = validate(composed["body"], composed["cta"], fs, category, merchant,
                     trigger, customer)
        if not v.ok:
            problems.append(f"{pair['test_id']}: {v.errors}")
        for w in v.warnings:
            problems.append(f"{pair['test_id']} (warning): {w}")

        if not composed["body"]:
            problems.append(f"{pair['test_id']}: empty body")
        if composed["body"] in seen_bodies:
            problems.append(
                f"{pair['test_id']}: body identical to {seen_bodies[composed['body']]}"
            )
        seen_bodies[composed["body"]] = pair["test_id"]

        row = {"test_id": pair["test_id"]}
        row.update({k: composed[k] for k in FIELDS if k != "test_id"})
        lines.append(json.dumps(row, ensure_ascii=False))

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"wrote {OUT.name} — {len(lines)} lines")
    if problems:
        print(f"\n{len(problems)} issue(s):")
        for p in problems:
            print(f"  {p}")
    else:
        print("validator clean on all lines")

    # Round-trip: the file the judge reads must parse and carry every key.
    bad = 0
    for i, line in enumerate(OUT.read_text(encoding="utf-8").splitlines(), 1):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"  line {i} is not valid JSON: {e}")
            bad += 1
            continue
        missing = [k for k in FIELDS if k not in obj]
        if missing:
            print(f"  line {i} missing {missing}")
            bad += 1
    print(f"round-trip: {len(lines) - bad}/{len(lines)} lines parse with all keys")

    chars = [len(json.loads(l)["body"]) for l in lines]
    print(f"body length: min={min(chars)} median={sorted(chars)[len(chars) // 2]} max={max(chars)}")
    return 1 if (problems or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
