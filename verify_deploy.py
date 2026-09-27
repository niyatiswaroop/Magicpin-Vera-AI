#!/usr/bin/env python3
"""Pre-flight check against a deployed bot (testing brief §12 checklist).

    BOT_URL=https://<user>-vera-bot.hf.space python verify_deploy.py

Runs the judge's actual lifecycle against the public URL: warmup with all 255
base contexts, the 255-context healthz gate, a trigger push, a tick, and the three
replay behaviours. Reports latencies against the limits the judge enforces.

Safe to run repeatedly -- it calls /v1/teardown first and again at the end, so it
leaves no state behind.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

BOT_URL = os.getenv("BOT_URL", "http://localhost:8080").rstrip("/")
D = Path(__file__).parent / "dataset" / "expanded"

PASS, FAIL, WARN = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m", "\033[93mWARN\033[0m"
results: list[tuple[str, str]] = []


def rec(state: str, label: str, detail: str = "") -> None:
    results.append((state, label))
    print(f"  [{state}] {label}" + (f"  — {detail}" if detail else ""))


def main() -> int:
    print(f"\nVerifying {BOT_URL}\n")
    if not D.exists():
        print(f"dataset not found at {D}; run dataset/generate_dataset.py first")
        return 2

    # Judge timeouts: 5s healthz/metadata, 15s tick/reply in the bundled simulator,
    # 30s per the brief. Use the generous bound and report the real latency.
    c = httpx.Client(timeout=35.0)

    # --- reachability -------------------------------------------------
    try:
        t0 = time.time()
        r = c.get(f"{BOT_URL}/v1/healthz")
        lat = (time.time() - t0) * 1000
    except Exception as e:
        rec(FAIL, "healthz unreachable", repr(e)[:90])
        return 1
    rec(PASS if r.status_code == 200 else FAIL, f"healthz reachable ({lat:.0f}ms)",
        f"HTTP {r.status_code}")
    if lat > 5000:
        rec(WARN, "healthz slower than the judge's 5s probe timeout", f"{lat:.0f}ms")
    if BOT_URL.startswith("http://") and "localhost" not in BOT_URL and "127.0.0.1" not in BOT_URL:
        rec(WARN, "URL is http:// not https://", "allowed, but https is expected in production")

    r = c.get(f"{BOT_URL}/v1/metadata")
    meta = r.json() if r.status_code == 200 else {}
    rec(PASS if r.status_code == 200 else FAIL, "metadata reachable")
    for k in ("team_name", "team_members", "model", "approach", "contact_email",
              "version", "submitted_at"):
        rec(PASS if k in meta else FAIL, f"metadata.{k}")
    if "example.com" in str(meta.get("contact_email", "")):
        rec(WARN, "contact_email still a placeholder", str(meta.get("contact_email")))

    # clean slate
    c.post(f"{BOT_URL}/v1/teardown", json={})

    # --- Phase 1 warmup: all 255 base contexts ------------------------
    print("\n  Phase 1 — warmup")
    t0 = time.time()
    counts = {"category": 0, "merchant": 0, "customer": 0}
    rejected = 0
    for scope, pat, key in (("category", "categories/*.json", "slug"),
                            ("merchant", "merchants/*.json", "merchant_id"),
                            ("customer", "customers/*.json", "customer_id")):
        for f in sorted(D.glob(pat.split("/")[0] + "/*.json")):
            payload = json.loads(f.read_text())
            resp = c.post(f"{BOT_URL}/v1/context", json={
                "scope": scope, "context_id": payload[key], "version": 1,
                "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})
            if resp.status_code == 200 and resp.json().get("accepted"):
                counts[scope] += 1
            else:
                rejected += 1
    elapsed = time.time() - t0
    total = sum(counts.values())
    rec(PASS if total == 255 else FAIL, f"pushed {total}/255 contexts in {elapsed:.1f}s",
        f"{rejected} rejected" if rejected else "")

    hz = c.get(f"{BOT_URL}/v1/healthz").json().get("contexts_loaded", {})
    gate = hz.get("category") == 5 and hz.get("merchant") == 50 and hz.get("customer") == 200
    rec(PASS if gate else FAIL, "healthz reports all 255 (the §4 warmup gate)", json.dumps(hz))

    # idempotency
    first = json.loads(next(iter(sorted((D / "merchants").glob("*.json")))).read_text())
    resp = c.post(f"{BOT_URL}/v1/context", json={
        "scope": "merchant", "context_id": first["merchant_id"], "version": 1,
        "payload": first, "delivered_at": "2026-04-26T10:00:01Z"})
    ok = resp.status_code == 409 and resp.json().get("reason") == "stale_version"
    rec(PASS if ok else FAIL, "re-push of same version → 409 stale_version",
        f"HTTP {resp.status_code}")

    # --- Phase 2: trigger push + tick ---------------------------------
    print("\n  Phase 2 — tick")
    trgs = [json.loads(p.read_text()) for p in sorted((D / "triggers").glob("*.json"))]
    picked = [t for t in trgs if t["kind"] in
              ("research_digest", "perf_dip", "competitor_opened", "recall_due")][:4]
    for t in picked:
        c.post(f"{BOT_URL}/v1/context", json={
            "scope": "trigger", "context_id": t["id"], "version": 1,
            "payload": t, "delivered_at": "2026-04-26T10:34:00Z"})

    t0 = time.time()
    resp = c.post(f"{BOT_URL}/v1/tick", json={
        "now": "2026-04-26T10:35:00Z",
        "available_triggers": [t["id"] for t in picked]})
    lat = (time.time() - t0) * 1000
    actions = resp.json().get("actions", []) if resp.status_code == 200 else []
    rec(PASS if resp.status_code == 200 else FAIL, f"tick 200 in {lat:.0f}ms")
    rec(PASS if lat < 15000 else FAIL, "tick within the simulator's 15s timeout",
        f"{lat:.0f}ms")
    rec(PASS if actions else WARN, f"tick returned {len(actions)} action(s)")

    required = ("conversation_id", "merchant_id", "customer_id", "send_as",
                "trigger_id", "template_name", "template_params", "body", "cta",
                "suppression_key", "rationale")
    for a in actions:
        missing = [k for k in required if k not in a]
        rec(PASS if not missing else FAIL, f"action schema ({a.get('trigger_id','?')[:26]})",
            f"missing {missing}" if missing else "")
        if "http://" in a.get("body", "") or "https://" in a.get("body", ""):
            rec(FAIL, "URL in body (−3 per F.4)", a["body"][:60])
    if actions:
        print(f"\n      sample: {actions[0]['body'][:150]}\n")

    # empty tick
    resp = c.post(f"{BOT_URL}/v1/tick", json={"now": "2026-04-26T10:40:00Z",
                                              "available_triggers": []})
    rec(PASS if resp.json() == {"actions": []} else FAIL,
        "empty available_triggers → {'actions': []}")

    # --- Phase 4 behaviours -------------------------------------------
    print("\n  Phase 4 — replay behaviours")
    mid = first["merchant_id"]
    auto = "Thank you for contacting us! Our team will respond shortly."
    seq = []
    for i in range(1, 5):
        rr = c.post(f"{BOT_URL}/v1/reply", json={
            "conversation_id": f"conv_verify_auto_{i}", "merchant_id": mid,
            "customer_id": None, "from_role": "merchant", "message": auto,
            "received_at": "2026-04-26T10:45:00Z", "turn_number": i + 1}).json()
        seq.append(rr.get("action"))
    rec(PASS if "end" in seq else FAIL, "auto-reply hell → ends", f"actions={seq}")

    t0 = time.time()
    rr = c.post(f"{BOT_URL}/v1/reply", json={
        "conversation_id": "conv_verify_intent", "merchant_id": mid,
        "customer_id": None, "from_role": "merchant",
        "message": "Ok lets do it. Whats next?",
        "received_at": "2026-04-26T10:50:00Z", "turn_number": 2}).json()
    lat = (time.time() - t0) * 1000
    body = (rr.get("body") or "").lower()
    qualifying = ["would you", "do you", "can you tell", "what if", "how about"]
    actioning = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]
    ok = rr.get("action") == "send" and any(w in body for w in actioning) \
        and not any(w in body for w in qualifying)
    rec(PASS if ok else FAIL, "intent transition → action mode", f"{lat:.0f}ms")
    rec(PASS if lat < 15000 else FAIL, "reply within 15s", f"{lat:.0f}ms")

    rr = c.post(f"{BOT_URL}/v1/reply", json={
        "conversation_id": "conv_verify_hostile", "merchant_id": mid,
        "customer_id": None, "from_role": "merchant",
        "message": "Stop messaging me. This is useless spam.",
        "received_at": "2026-04-26T10:55:00Z", "turn_number": 2}).json()
    rec(PASS if rr.get("action") == "end" else FAIL, "hostile → ends")

    rr = c.post(f"{BOT_URL}/v1/reply", json={
        "conversation_id": "conv_verify_offtopic", "merchant_id": mid,
        "customer_id": None, "from_role": "merchant",
        "message": "can you also help me file my GST?",
        "received_at": "2026-04-26T10:56:00Z", "turn_number": 2}).json()
    rec(PASS if rr.get("action") == "send" else FAIL, "off-topic → stays on mission")

    # --- restart durability hint --------------------------------------
    print("\n  Durability")
    hz = c.get(f"{BOT_URL}/v1/healthz").json()
    rec(PASS, "note: after any host restart, re-check healthz still reports 255",
        f"currently {json.dumps(hz.get('contexts_loaded', {}))}")

    c.post(f"{BOT_URL}/v1/teardown", json={})

    nf = sum(1 for s, _ in results if s == FAIL)
    nw = sum(1 for s, _ in results if s == WARN)
    print(f"\n{'=' * 66}")
    print(f"  {len(results) - nf - nw} passed, {nw} warnings, {nf} failures")
    if nf:
        print("\n  Failures:")
        for s, label in results:
            if s == FAIL:
                print(f"    - {label}")
    else:
        print("\n  Ready to submit this URL.")
    print(f"{'=' * 66}\n")
    return 1 if nf else 0


if __name__ == "__main__":
    sys.exit(main())
