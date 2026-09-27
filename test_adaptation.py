#!/usr/bin/env python3
"""Phase 3 adaptive-context-injection tests (testing brief §4 Phase 3).

The judge interleaves context updates the bot never saw during development and
scores whether later sends incorporate them:

    "Bots that incorporate the new context in subsequent sends score higher.
     Bots that ignore it (sending stale composition) score lower. Bots that
     hallucinate (invent context that wasn't pushed) score lowest."

Worth up to +5 per dimension. Runs against a live server so it exercises the real
version-replacement path, not just the composer.

    BOT_URL=http://localhost:8080 python test_adaptation.py
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path

import httpx

BOT_URL = os.getenv("BOT_URL", "http://localhost:8080").rstrip("/")
D = Path(__file__).parent / "dataset" / "expanded"

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results: list[tuple[bool, str]] = []
c = httpx.Client(timeout=35.0)


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    print(f"  [{PASS if ok else FAIL}] {label}" + (f"  — {detail}" if detail else ""))


def push(scope: str, cid: str, version: int, payload: dict) -> dict:
    r = c.post(f"{BOT_URL}/v1/context", json={
        "scope": scope, "context_id": cid, "version": version,
        "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})
    return r.json()


def tick(trigger_ids: list[str], now: str = "2026-04-26T11:00:00Z") -> list[dict]:
    r = c.post(f"{BOT_URL}/v1/tick", json={"now": now, "available_triggers": trigger_ids})
    return r.json().get("actions", [])


def load(rel: str) -> dict:
    return json.loads((D / rel).read_text())


def numbers(text: str) -> set[str]:
    return {m.replace(",", "") for m in re.findall(r"\d+(?:[.,]\d+)*", text)}


# ---------------------------------------------------------------------------

def test_updated_performance() -> None:
    """10 merchants get new perf numbers mid-test. Later sends must use them."""
    print("\nInjection 1 — updated performance snapshot")
    c.post(f"{BOT_URL}/v1/teardown", json={})

    cat = load("categories/dentists.json")
    mer = load("merchants/m_001_drmeera_dentist_delhi.json")
    push("category", "dentists", 1, cat)
    push("merchant", mer["merchant_id"], 1, mer)

    trg_v1 = {"id": "trg_adapt_perf_a", "scope": "merchant", "kind": "perf_dip",
              "source": "internal", "merchant_id": mer["merchant_id"],
              "customer_id": None, "urgency": 4,
              "payload": {"metric": "calls", "delta_pct": -0.5, "window": "7d"},
              "suppression_key": "adapt:perf:a", "expires_at": "2026-06-30T00:00:00Z"}
    push("trigger", trg_v1["id"], 1, trg_v1)
    before = tick([trg_v1["id"]])
    check(len(before) == 1, "baseline send produced")
    if not before:
        return
    body_before = before[0]["body"]
    print(f"      before: {body_before[:120]}")
    check("2,410" in body_before or "2410" in numbers(body_before),
          "baseline cites the v1 view count (2,410)", body_before[:60])

    # The judge pushes v2 with different numbers.
    mer_v2 = copy.deepcopy(mer)
    mer_v2["performance"]["views"] = 7788
    mer_v2["performance"]["calls"] = 61
    mer_v2["performance"]["ctr"] = 0.049
    mer_v2["performance"]["delta_7d"] = {"views_pct": 0.31, "calls_pct": 0.44}
    ack = push("merchant", mer["merchant_id"], 2, mer_v2)
    check(ack.get("accepted") is True, "merchant v2 accepted")

    trg_v2 = dict(trg_v1, id="trg_adapt_perf_b",
                  payload={"metric": "calls", "delta_pct": 0.44, "window": "7d"},
                  kind="perf_spike", suppression_key="adapt:perf:b")
    push("trigger", trg_v2["id"], 1, trg_v2)
    after = tick([trg_v2["id"]], now="2026-04-26T11:05:00Z")
    check(len(after) == 1, "post-injection send produced")
    if not after:
        return
    body_after = after[0]["body"]
    print(f"      after:  {body_after[:120]}")

    nums = numbers(body_after)
    check("7788" in nums, "uses the NEW view count (7,788)", sorted(nums))
    check("2410" not in nums, "does not carry the stale view count (2,410)")
    check("61" in nums or "49" in nums or "4.9" in nums,
          "uses new calls/ctr", sorted(nums))
    check(body_after != body_before, "composition actually changed")


def test_new_digest_item() -> None:
    """5 new digest items per category, pushed as a new version."""
    print("\nInjection 2 — new digest items on the category context")
    cat = load("categories/dentists.json")
    mer = load("merchants/m_001_drmeera_dentist_delhi.json")

    cat_v2 = copy.deepcopy(cat)
    new_item = {
        "id": "d_2026W40_newtrial_silver",
        "kind": "research",
        "title": "Silver diamine fluoride arrests root caries in 81% of elderly patients",
        "source": "Indian Journal of Dental Research, Sep 2026, p.221",
        "trial_n": 640,
        "patient_segment": "elderly_root_caries",
        "summary": "640-patient multicentre trial; SDF arrested 81% of root caries "
                   "lesions at 12 months versus 46% for varnish alone.",
        "actionable": "Consider SDF for elderly patients with active root caries",
    }
    cat_v2["digest"] = [new_item] + cat_v2["digest"]
    ack = push("category", "dentists", 2, cat_v2)
    check(ack.get("accepted") is True, "category v2 accepted")

    trg = {"id": "trg_adapt_digest", "scope": "merchant", "kind": "research_digest",
           "source": "external", "merchant_id": mer["merchant_id"],
           "customer_id": None, "urgency": 2,
           "payload": {"category": "dentists", "top_item_id": new_item["id"]},
           "suppression_key": "adapt:digest", "expires_at": "2026-06-30T00:00:00Z"}
    push("trigger", trg["id"], 1, trg)
    actions = tick([trg["id"]], now="2026-04-26T11:10:00Z")
    check(len(actions) == 1, "send produced for the injected digest item")
    if not actions:
        return
    body = actions[0]["body"]
    print(f"      {body[:150]}")

    check("Indian Journal of Dental Research" in body,
          "cites the NEW source, not a pre-existing one")
    check("JIDA" not in body, "does not cite the stale JIDA item")
    check("640" in numbers(body), "cites the new trial size (640)", sorted(numbers(body)))
    check("81" in numbers(body) or "elderly" in body.lower(),
          "carries the new finding")


def test_midtest_customer() -> None:
    """A customer context arrives mid-test, with a recall_due trigger 2 min later."""
    print("\nInjection 3 — new customer context, then a customer-scoped trigger")
    mer = load("merchants/m_001_drmeera_dentist_delhi.json")
    cust = {
        "customer_id": "c_adapt_rohan_for_m001",
        "merchant_id": mer["merchant_id"],
        "identity": {"name": "Rohan", "phone_redacted": "<phone>",
                     "language_pref": "hi-en mix", "age_band": "40-50"},
        "relationship": {"first_visit": "2024-02-11", "last_visit": "2026-03-02",
                         "visits_total": 7,
                         "services_received": ["cleaning", "rct", "crown"],
                         "lifetime_value": 18400},
        "state": "lapsed_soft",
        "preferences": {"preferred_slots": "weekend_morning", "channel": "whatsapp",
                        "reminder_opt_in": True},
        "consent": {"opted_in_at": "2024-02-11",
                    "scope": ["recall_reminders", "appointment_reminders"]},
    }
    ack = push("customer", cust["customer_id"], 1, cust)
    check(ack.get("accepted") is True, "mid-test customer context accepted")

    trg = {"id": "trg_adapt_recall", "scope": "customer", "kind": "recall_due",
           "source": "internal", "merchant_id": mer["merchant_id"],
           "customer_id": cust["customer_id"], "urgency": 3,
           "payload": {"service_due": "6_month_cleaning",
                       "last_service_date": "2026-03-02", "due_date": "2026-09-02"},
           "suppression_key": "adapt:recall", "expires_at": "2026-06-30T00:00:00Z"}
    push("trigger", trg["id"], 1, trg)
    actions = tick([trg["id"]], now="2026-04-26T11:12:00Z")
    check(len(actions) == 1, "send produced for the new customer")
    if not actions:
        return
    a = actions[0]
    body = a["body"]
    print(f"      {body[:150]}")

    check(a["send_as"] == "merchant_on_behalf",
          "send_as is merchant_on_behalf, not vera", a["send_as"])
    check(a["customer_id"] == cust["customer_id"], "customer_id echoed on the action")
    check("Rohan" in body, "addresses the new customer by name")
    check("7" in numbers(body) or "crown" in body.lower() or "rct" in body.lower(),
          "uses the new relationship history", sorted(numbers(body)))
    # Merchant-side aggregates must not leak into a customer-facing message.
    for leak in ("2,410", "7,788", "peer", "click-through", "CTR"):
        check(leak.lower() not in body.lower(),
              f"no merchant-side data leaked to the customer ({leak!r})")


def test_no_hallucination_after_injection() -> None:
    """Every number in a post-injection send must trace to pushed context."""
    print("\nInjection 4 — closed-world check against the injected contexts")
    sys.path.insert(0, str(Path(__file__).parent))
    from facts import build_factset, validate

    cat = load("categories/dentists.json")
    cat_v2 = copy.deepcopy(cat)
    cat_v2["digest"] = [{
        "id": "d_x", "kind": "research", "title": "Test item 12345 effect",
        "source": "Test Journal, Oct 2026, p.9", "trial_n": 4321,
        "summary": "4321 patients, 67% improvement.", "actionable": "Do the thing",
    }] + cat["digest"]
    mer = load("merchants/m_001_drmeera_dentist_delhi.json")
    mer_v2 = copy.deepcopy(mer)
    mer_v2["performance"]["views"] = 7788
    trg = {"id": "trg_adapt_halluc", "scope": "merchant", "kind": "research_digest",
           "source": "external", "merchant_id": mer["merchant_id"], "customer_id": None,
           "urgency": 2, "payload": {"top_item_id": "d_x"},
           "suppression_key": "adapt:halluc", "expires_at": "2026-06-30T00:00:00Z"}

    push("category", "dentists", 3, cat_v2)
    push("merchant", mer["merchant_id"], 3, mer_v2)
    push("trigger", trg["id"], 1, trg)
    actions = tick([trg["id"]], now="2026-04-26T11:20:00Z")
    check(len(actions) == 1, "send produced")
    if not actions:
        return
    body = actions[0]["body"]
    print(f"      {body[:150]}")

    fs = build_factset(cat_v2, mer_v2, trg, None)
    v = validate(body, actions[0]["cta"], fs, cat_v2, mer_v2, trg, None)
    check(v.ok, "validator clean against the INJECTED contexts", str(v.errors))
    check("4321" in numbers(body) or "Test Journal" in body,
          "grounded in the injected item")


def test_version_monotonicity() -> None:
    """A late-arriving lower version must not overwrite newer context."""
    print("\nInjection 5 — out-of-order version arrival")
    mer = load("merchants/m_001_drmeera_dentist_delhi.json")
    stale = copy.deepcopy(mer)
    stale["performance"]["views"] = 11
    r = c.post(f"{BOT_URL}/v1/context", json={
        "scope": "merchant", "context_id": mer["merchant_id"], "version": 2,
        "payload": stale, "delivered_at": "2026-04-26T11:25:00Z"})
    check(r.status_code == 409, "stale version rejected with 409", f"HTTP {r.status_code}")
    check(r.json().get("current_version") == 3, "reports the newer version it holds",
          str(r.json().get("current_version")))

    trg = {"id": "trg_adapt_order", "scope": "merchant", "kind": "perf_spike",
           "source": "internal", "merchant_id": mer["merchant_id"], "customer_id": None,
           "urgency": 1, "payload": {"metric": "views", "delta_pct": 0.31},
           "suppression_key": "adapt:order", "expires_at": "2026-06-30T00:00:00Z"}
    push("trigger", trg["id"], 1, trg)
    actions = tick([trg["id"]], now="2026-04-26T11:26:00Z")
    if actions:
        nums = numbers(actions[0]["body"])
        check("11" not in nums or "7788" in nums,
              "composition still uses v3 data, not the rejected payload", sorted(nums))


def main() -> int:
    try:
        c.get(f"{BOT_URL}/v1/healthz")
    except Exception as e:
        print(f"bot unreachable at {BOT_URL}: {e}")
        return 2

    for fn in (test_updated_performance, test_new_digest_item, test_midtest_customer,
               test_no_hallucination_after_injection, test_version_monotonicity):
        fn()

    c.post(f"{BOT_URL}/v1/teardown", json={})
    failed = [l for ok, l in results if not ok]
    print(f"\n{'=' * 66}")
    print(f"  {len(results) - len(failed)}/{len(results)} checks passed")
    for l in failed:
        print(f"    FAILED: {l}")
    print(f"{'=' * 66}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
