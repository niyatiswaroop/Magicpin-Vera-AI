#!/usr/bin/env python3
"""Local eval loop for the composer.

Two layers, cheap first:

1. **Offline lint** -- zero cost, instant, deterministic. Hard-constraint checks
   from facts.validate plus structural rubric proxies. Run this on every change.
2. **LLM rubric scoring** -- reuses judge_simulator.LLMScorer.SYSTEM verbatim so
   the rubric wording matches the real harness, but at temperature 0 rather than
   the simulator's 0.2, because A/B comparisons across runs are meaningless if
   the judge itself wobbles.

Why not just run judge_simulator.py: its DatasetLoader reads only the *seed*
files (10 merchants / 15 customers / 25 triggers), so it never exercises the 40
generated merchants or the 75 placeholder triggers -- and 14 of the 30 graded
pairs are exactly that sparse case. This harness scores all 30 canonical pairs.

    .venv/bin/python eval_harness.py lint          # free, instant
    .venv/bin/python eval_harness.py score         # local LLM, all 30 pairs
    .venv/bin/python eval_harness.py score --n 8   # first 8 only
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent))

from composer import compose
from facts import build_factset, validate

ROOT = Path(__file__).parent
D = ROOT / "dataset" / "expanded"
STATE = ROOT / ".eval_state.json"

DIMENSIONS = ("specificity", "category_fit", "merchant_fit",
              "decision_quality", "engagement_compulsion")


# ---------------------------------------------------------------------------

def load_dataset() -> dict[str, Any]:
    cats = {json.loads(p.read_text())["slug"]: json.loads(p.read_text())
            for p in (D / "categories").glob("*.json")}
    mers = {json.loads(p.read_text())["merchant_id"]: json.loads(p.read_text())
            for p in (D / "merchants").glob("*.json")}
    custs = {json.loads(p.read_text())["customer_id"]: json.loads(p.read_text())
             for p in (D / "customers").glob("*.json")}
    trgs = {json.loads(p.read_text())["id"]: json.loads(p.read_text())
            for p in (D / "triggers").glob("*.json")}
    pairs = json.loads((D / "test_pairs.json").read_text())["pairs"]
    return {"categories": cats, "merchants": mers, "customers": custs,
            "triggers": trgs, "pairs": pairs}


def compose_pair(ds: dict, pair: dict, use_llm: bool = False) -> dict:
    t = ds["triggers"][pair["trigger_id"]]
    m = ds["merchants"][pair["merchant_id"]]
    c = ds["customers"].get(pair.get("customer_id")) if pair.get("customer_id") else None
    cat = ds["categories"][m["category_slug"]]
    out = compose(cat, m, t, c, use_llm=use_llm)
    out["_pair"] = pair
    out["_ctx"] = {"category": cat, "merchant": m, "trigger": t, "customer": c}
    return out


# ---------------------------------------------------------------------------
# Layer 1 -- offline lint
# ---------------------------------------------------------------------------

def lint(ds: dict, verbose: bool = True) -> dict[str, Any]:
    """Hard constraints plus structural proxies for the five dimensions."""
    rows = []
    for pair in ds["pairs"]:
        r = compose_pair(ds, pair)
        ctx = r["_ctx"]
        fs = build_factset(ctx["category"], ctx["merchant"], ctx["trigger"], ctx["customer"])
        v = validate(r["body"], r["cta"], fs, ctx["category"], ctx["merchant"],
                     ctx["trigger"], ctx["customer"])
        body = r["body"]

        # Structural proxies. Not scores -- signals that correlate with the rubric.
        numbers = len(re.findall(r"\d", body))
        cited = r["_meta"]["cited"]
        has_citation = any(
            k.startswith("digest.source") for k in
            (c.split("=")[0] for c in cited)
        )
        merchant_specific = sum(
            1 for c in cited
            if c.split("=")[0].startswith(("perf.", "delta.", "offer.", "agg.",
                                           "signal.", "review.", "subs.", "cust."))
        )
        trigger_specific = sum(1 for c in cited if c.split("=")[0].startswith("trg."))
        levers = len(r["_meta"]["levers"])

        rows.append({
            "test_id": pair["test_id"],
            "kind": ctx["trigger"]["kind"],
            "category": ctx["merchant"]["category_slug"],
            "placeholder": bool(ctx["trigger"].get("payload", {}).get("placeholder")),
            "sparse_merchant": not ctx["merchant"].get("signals"),
            "errors": v.errors,
            "warnings": v.warnings,
            "chars": len(body),
            "digits": numbers,
            "n_cited": len(cited),
            "merchant_facts": merchant_specific,
            "trigger_facts": trigger_specific,
            "has_citation": has_citation,
            "levers": levers,
            "cta": r["cta"],
            "send_as": r["send_as"],
            "body": body,
        })

    n = len(rows)
    failed = [r for r in rows if r["errors"]]
    warned = [r for r in rows if r["warnings"]]
    no_number = [r for r in rows if r["digits"] == 0]
    thin = [r for r in rows if r["n_cited"] < 2]
    one_lever = [r for r in rows if r["levers"] < 2]

    if verbose:
        print(f"\n{'OFFLINE LINT':=^74}\n")
        print(f"  pairs                     {n}")
        print(f"  hard-constraint failures  {len(failed)}")
        print(f"  with warnings             {len(warned)}")
        print(f"  no number in body         {len(no_number)}   <- Specificity risk")
        print(f"  fewer than 2 facts cited  {len(thin)}   <- Merchant fit risk")
        print(f"  fewer than 2 levers       {len(one_lever)}   <- Compulsion risk")
        print(f"  median length             {statistics.median(r['chars'] for r in rows):.0f} chars")
        print(f"  cite a source             {sum(1 for r in rows if r['has_citation'])}")
        print(f"  customer-facing           {sum(1 for r in rows if r['send_as'] != 'vera')}")
        for r in failed:
            print(f"\n  FAIL {r['test_id']} {r['kind']}: {r['errors']}")
            print(f"       {r['body'][:120]}")
        if warned:
            print()
            for r in warned:
                print(f"  warn {r['test_id']} {r['kind']}: {r['warnings']}")
        if one_lever:
            print("\n  single-lever pairs (weakest on Engagement compulsion):")
            for r in one_lever:
                print(f"    {r['test_id']} {r['kind']:24} levers={r['levers']}")
    return {"rows": rows, "failed": len(failed), "warned": len(warned),
            "no_number": len(no_number), "thin": len(thin), "one_lever": len(one_lever)}


# ---------------------------------------------------------------------------
# Layer 2 -- LLM rubric scoring
# ---------------------------------------------------------------------------

def _scorer_system() -> str:
    """The real harness's rubric wording, imported rather than paraphrased."""
    import judge_simulator
    return judge_simulator.LLMScorer.SYSTEM


def _score_prompt(r: dict) -> str:
    """Mirrors judge_simulator.LLMScorer.score's prompt, including the fact that
    it shows the judge only a narrow slice of context."""
    ctx = r["_ctx"]
    category, merchant, trigger, customer = (
        ctx["category"], ctx["merchant"], ctx["trigger"], ctx["customer"])
    body = r["body"]
    perf = merchant.get("performance", {})
    return f"""SCORE THIS MESSAGE:

=== CONTEXT PROVIDED TO BOT ===
Category: {category.get('slug', 'unknown')}
Voice: {category.get('voice', {}).get('tone', 'unknown')}
Taboos: {category.get('voice', {}).get('vocab_taboo', [])[:5]}

Merchant: {merchant.get('identity', {}).get('name', 'unknown')}
Owner: {merchant.get('identity', {}).get('owner_first_name', 'unknown')}
Locality: {merchant.get('identity', {}).get('locality', 'unknown')}
Languages: {merchant.get('identity', {}).get('languages', [])}
Performance: views={perf.get('views', '?')}, calls={perf.get('calls', '?')}, ctr={perf.get('ctr', '?')}
Signals: {merchant.get('signals', [])}
Active Offers: {[o.get('title') for o in merchant.get('offers', []) if o.get('status') == 'active']}

Trigger Kind: {trigger.get('kind', 'unknown')}
Trigger Payload: {json.dumps(trigger.get('payload', {}))}
Trigger Urgency: {trigger.get('urgency', '?')}

Customer: {json.dumps(customer.get('identity', {})) if customer else 'None (merchant-facing)'}

=== BOT'S MESSAGE ===
Body ({len(body)} chars): "{body}"
CTA: {r.get('cta', 'none')}
Send As: {r.get('send_as', 'vera')}

Score each dimension 0-10 with clear reasoning. Be STRICT."""


def score(ds: dict, limit: Optional[int] = None, verbose: bool = True) -> dict[str, Any]:
    import llm

    if not llm.available():
        print("Ollama is not reachable -- start it with:\n"
              "  OLLAMA_FLASH_ATTENTION=1 ollama serve")
        return {}

    system = _scorer_system()
    pairs = ds["pairs"][: limit or len(ds["pairs"])]
    scored: list[dict] = []
    t0 = time.time()

    print(f"\n{'LLM RUBRIC SCORING':=^74}")
    print(f"  judge: {llm.MODEL} (local, temperature 0)\n")

    for pair in pairs:
        r = compose_pair(ds, pair)
        raw = llm.generate(_score_prompt(r), system=system)
        parsed = _parse(raw)
        if parsed is None:
            print(f"  {pair['test_id']}  UNPARSEABLE judge output")
            continue
        parsed["test_id"] = pair["test_id"]
        parsed["kind"] = r["_ctx"]["trigger"]["kind"]
        parsed["total"] = sum(parsed[d] for d in DIMENSIONS)
        parsed["body"] = r["body"]
        scored.append(parsed)
        bars = "  ".join(f"{d[:4]}={parsed[d]:2}" for d in DIMENSIONS)
        print(f"  {pair['test_id']} {parsed['kind'][:22]:22} {bars}  total={parsed['total']:2}/50")

    if not scored:
        return {}

    print(f"\n  {len(scored)} pairs scored in {time.time() - t0:.0f}s")
    print(f"\n{'PER-DIMENSION MEANS':-^74}")
    means = {}
    for d in DIMENSIONS:
        vals = [s[d] for s in scored]
        means[d] = statistics.mean(vals)
        lo = min(vals)
        worst = [s["test_id"] for s in scored if s[d] == lo]
        bar = "#" * int(round(means[d]))
        print(f"  {d:22} {means[d]:5.2f}/10  {bar:<10}  min={lo} ({', '.join(worst[:4])})")
    total_mean = statistics.mean(s["total"] for s in scored)
    print(f"  {'TOTAL':22} {total_mean:5.2f}/50")

    weakest = min(means, key=means.get)
    print(f"\n  weakest dimension: {weakest} ({means[weakest]:.2f})")
    print(f"\n{'LOWEST-SCORING PAIRS':-^74}")
    for s in sorted(scored, key=lambda x: x["total"])[:5]:
        print(f"  {s['test_id']} {s['kind'][:20]:20} total={s['total']}/50")
        print(f"      {s['body'][:104]}")
        for d in DIMENSIONS:
            reason = s.get(f"{d}_reason") or s.get("engagement_reason", "")
            if s[d] <= 6 and reason:
                print(f"      {d}={s[d]}: {reason[:120]}")
    _save(means, total_mean, scored)
    return {"means": means, "total": total_mean, "scored": scored, "weakest": weakest}


def _parse(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return None
    out: dict[str, Any] = {}
    for d in DIMENSIONS:
        val = data.get(d, data.get("trigger_relevance") if d == "decision_quality" else None)
        try:
            out[d] = max(0, min(10, int(val)))
        except (TypeError, ValueError):
            out[d] = 5
        rk = f"{d}_reason"
        out[rk] = str(data.get(rk, data.get("engagement_reason", "")))[:400]
    out["hint"] = str(data.get("hint", ""))[:300]
    return out


def _save(means: dict, total: float, scored: list[dict]) -> None:
    history = []
    if STATE.exists():
        try:
            history = json.loads(STATE.read_text()).get("runs", [])
        except (json.JSONDecodeError, OSError):
            history = []
    history.append({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "means": means, "total": total, "n": len(scored),
    })
    try:
        STATE.write_text(json.dumps({"runs": history}, indent=2))
    except OSError:
        pass
    if len(history) > 1:
        prev = history[-2]
        print(f"\n{'VS PREVIOUS RUN':-^74}")
        for d in DIMENSIONS:
            delta = means[d] - prev["means"].get(d, 0)
            arrow = "+" if delta > 0 else ("-" if delta < 0 else "=")
            print(f"  {d:22} {prev['means'].get(d, 0):5.2f} -> {means[d]:5.2f}  {arrow}{abs(delta):.2f}")
        print(f"  {'TOTAL':22} {prev['total']:5.2f} -> {total:5.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["lint", "score", "both"], default="lint", nargs="?")
    ap.add_argument("--n", type=int, default=None)
    args = ap.parse_args()

    ds = load_dataset()
    if args.mode in ("lint", "both"):
        res = lint(ds)
        if args.mode == "lint":
            return 1 if res["failed"] else 0
    if args.mode in ("score", "both"):
        score(ds, limit=args.n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
