#!/usr/bin/env python3
"""Contract tests against the exact payloads in examples/api-call-examples.md.

Verifies the HTTP surface only -- composition quality is not asserted here.

    .venv/bin/python test_contract.py            # against a running bot
    BOT_URL=http://localhost:8080 ... test_contract.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

BOT_URL = os.getenv("BOT_URL", "http://localhost:8080").rstrip("/")
DATA = Path(__file__).parent / "dataset" / "expanded"

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((ok, label, detail))
    print(f"  [{PASS if ok else FAIL}] {label}" + (f"  — {detail}" if detail and not ok else ""))
    return ok


def load(rel: str) -> dict:
    return json.loads((DATA / rel).read_text())


def main() -> int:
    c = httpx.Client(timeout=30.0)

    # ---- Example 1.1 -- healthz before any push -----------------------
    print("\nExample 1.1 — GET /v1/healthz (pre-warmup)")
    r = c.get(f"{BOT_URL}/v1/healthz")
    check(r.status_code == 200, "200 OK", str(r.status_code))
    j = r.json()
    check(j.get("status") == "ok", "status == 'ok'", repr(j.get("status")))
    check(isinstance(j.get("uptime_seconds"), int), "uptime_seconds is int")
    cl = j.get("contexts_loaded", {})
    check(
        set(cl) == {"category", "merchant", "customer", "trigger"},
        "contexts_loaded has all 4 scopes",
        repr(sorted(cl)),
    )
    check(all(v == 0 for v in cl.values()), "all counts zero pre-warmup", repr(cl))

    # ---- Example 1.2 -- metadata --------------------------------------
    print("\nExample 1.2 — GET /v1/metadata")
    r = c.get(f"{BOT_URL}/v1/metadata")
    check(r.status_code == 200, "200 OK", str(r.status_code))
    j = r.json()
    for key in ("team_name", "team_members", "model", "approach",
                "contact_email", "version", "submitted_at"):
        check(key in j, f"has '{key}'")
    check(isinstance(j.get("team_members"), list), "team_members is a list")

    # ---- Example 1.3 -- push CategoryContext --------------------------
    print("\nExample 1.3 — POST /v1/context (category)")
    cat = load("categories/dentists.json")
    r = c.post(f"{BOT_URL}/v1/context", json={
        "scope": "category", "context_id": "dentists", "version": 1,
        "payload": cat, "delivered_at": "2026-04-26T10:00:00Z",
    })
    check(r.status_code == 200, "200 OK", str(r.status_code))
    j = r.json()
    check(j.get("accepted") is True, "accepted == true", repr(j))
    check("ack_id" in j and "stored_at" in j, "has ack_id + stored_at")

    # ---- Example 1.4 -- push MerchantContext --------------------------
    print("\nExample 1.4 — POST /v1/context (merchant)")
    mer = load("merchants/m_001_drmeera_dentist_delhi.json")
    mid = mer["merchant_id"]
    r = c.post(f"{BOT_URL}/v1/context", json={
        "scope": "merchant", "context_id": mid, "version": 1,
        "payload": mer, "delivered_at": "2026-04-26T10:00:05Z",
    })
    check(r.status_code == 200 and r.json().get("accepted") is True, "accepted == true")

    # ---- Example 1.5 -- idempotency: same version re-pushed -----------
    print("\nExample 1.5 — POST /v1/context (same version → 409 stale_version)")
    r = c.post(f"{BOT_URL}/v1/context", json={
        "scope": "merchant", "context_id": mid, "version": 1,
        "payload": mer, "delivered_at": "2026-04-26T10:00:06Z",
    })
    check(r.status_code == 409, "409 Conflict", str(r.status_code))
    j = r.json()
    check(j.get("accepted") is False, "accepted == false", repr(j))
    check(j.get("reason") == "stale_version", "reason == 'stale_version'", repr(j.get("reason")))
    check(j.get("current_version") == 1, "current_version == 1", repr(j.get("current_version")))

    # ---- Example 1.6 -- version bump replaces -------------------------
    print("\nExample 1.6 — POST /v1/context (version 2 replaces, views→2580)")
    bumped = json.loads(json.dumps(mer))
    bumped["performance"]["views"] = 2580
    r = c.post(f"{BOT_URL}/v1/context", json={
        "scope": "merchant", "context_id": mid, "version": 2,
        "payload": bumped, "delivered_at": "2026-04-26T10:30:00Z",
    })
    check(r.status_code == 200 and r.json().get("accepted") is True, "accepted == true")

    # a stale push after the bump must also 409, reporting the higher version
    r = c.post(f"{BOT_URL}/v1/context", json={
        "scope": "merchant", "context_id": mid, "version": 1,
        "payload": mer, "delivered_at": "2026-04-26T10:31:00Z",
    })
    check(
        r.status_code == 409 and r.json().get("current_version") == 2,
        "lower version after bump → 409 current_version=2",
        repr(r.json()),
    )

    # ---- malformed scope ---------------------------------------------
    print("\nMalformed — POST /v1/context (bad scope → 400 invalid_scope)")
    r = c.post(f"{BOT_URL}/v1/context", json={
        "scope": "nonsense", "context_id": "x", "version": 1,
        "payload": {"a": 1}, "delivered_at": "2026-04-26T10:00:00Z",
    })
    check(r.status_code == 400, "400 Bad Request", str(r.status_code))
    check(r.json().get("reason") == "invalid_scope", "reason == 'invalid_scope'")

    # ---- Example 1.7 -- healthz reflects the pushes -------------------
    print("\nExample 1.7 — GET /v1/healthz (post-warmup counts)")
    cl = c.get(f"{BOT_URL}/v1/healthz").json()["contexts_loaded"]
    check(cl["category"] == 1, "category == 1", repr(cl))
    check(cl["merchant"] == 1, "merchant == 1", repr(cl))

    # ---- Example 2.1 -- incremental trigger push ----------------------
    print("\nExample 2.1 — POST /v1/context (trigger)")
    trg = next(
        t for t in (json.loads(p.read_text()) for p in (DATA / "triggers").glob("*.json"))
        if t["merchant_id"] == mid and t["kind"] == "research_digest"
    ) if any(
        json.loads(p.read_text()).get("kind") == "research_digest"
        and json.loads(p.read_text()).get("merchant_id") == mid
        for p in (DATA / "triggers").glob("*.json")
    ) else next(
        t for t in (json.loads(p.read_text()) for p in (DATA / "triggers").glob("*.json"))
        if t["merchant_id"] == mid
    )
    tid = trg["id"]
    r = c.post(f"{BOT_URL}/v1/context", json={
        "scope": "trigger", "context_id": tid, "version": 1,
        "payload": trg, "delivered_at": "2026-04-26T10:34:00Z",
    })
    check(r.status_code == 200 and r.json().get("accepted") is True,
          f"trigger accepted ({trg['kind']})")

    # ---- Example 2.2 -- tick returns an action -----------------------
    print("\nExample 2.2 — POST /v1/tick (bot decides to send)")
    r = c.post(f"{BOT_URL}/v1/tick", json={
        "now": "2026-04-26T10:35:00Z", "available_triggers": [tid],
    })
    check(r.status_code == 200, "200 OK", str(r.status_code))
    j = r.json()
    check(isinstance(j.get("actions"), list), "actions is a list")
    check(len(j["actions"]) == 1, "exactly 1 action", f"got {len(j.get('actions', []))}")
    if j.get("actions"):
        a = j["actions"][0]
        for key in ("conversation_id", "merchant_id", "customer_id", "send_as",
                    "trigger_id", "template_name", "template_params", "body",
                    "cta", "suppression_key", "rationale"):
            check(key in a, f"action has '{key}'")
        check(bool(a.get("body")), "body is non-empty")
        check(a.get("send_as") in ("vera", "merchant_on_behalf"),
              "send_as in enum", repr(a.get("send_as")))
        check(a.get("trigger_id") == tid, "trigger_id echoes the trigger")
        check(a.get("merchant_id") == mid, "merchant_id correct")
        check("http://" not in a["body"] and "https://" not in a["body"],
              "no URL in body (F.4 = -3)")
        print(f"      body: {a['body'][:110]}")

    # ---- Example 2.3 -- tick with nothing to do ----------------------
    print("\nExample 2.3 — POST /v1/tick (nothing worth sending)")
    r = c.post(f"{BOT_URL}/v1/tick", json={
        "now": "2026-04-26T10:40:00Z", "available_triggers": [],
    })
    check(r.status_code == 200 and r.json() == {"actions": []},
          "returns {'actions': []}", repr(r.json()))

    print("\nSuppression — same trigger re-offered must not re-send")
    r = c.post(f"{BOT_URL}/v1/tick", json={
        "now": "2026-04-26T10:45:00Z", "available_triggers": [tid],
    })
    check(r.json().get("actions") == [], "suppressed on second tick", repr(r.json()))

    print("\nUnknown trigger id — must be skipped, not error")
    r = c.post(f"{BOT_URL}/v1/tick", json={
        "now": "2026-04-26T10:50:00Z", "available_triggers": ["trg_does_not_exist"],
    })
    check(r.status_code == 200 and r.json().get("actions") == [],
          "unknown trigger → empty actions")

    # ---- Example 2.4 -- reply ----------------------------------------
    print("\nExample 2.4 — POST /v1/reply (engaged merchant)")
    conv_id = f"conv_{mid}__{tid}"
    r = c.post(f"{BOT_URL}/v1/reply", json={
        "conversation_id": conv_id, "merchant_id": mid, "customer_id": None,
        "from_role": "merchant", "message": "Yes, send me the abstract",
        "received_at": "2026-04-26T10:45:00Z", "turn_number": 2,
    })
    check(r.status_code == 200, "200 OK", str(r.status_code))
    j = r.json()
    check(j.get("action") in ("send", "wait", "end"),
          "action in {send, wait, end}", repr(j.get("action")))
    check("rationale" in j, "has rationale")
    if j.get("action") == "send":
        check(bool(j.get("body")), "send has non-empty body")
    if j.get("action") == "wait":
        check(isinstance(j.get("wait_seconds"), int), "wait has wait_seconds int")

    # ---- teardown ----------------------------------------------------
    print("\nOptional — POST /v1/teardown (state wipe, §11)")
    r = c.post(f"{BOT_URL}/v1/teardown", json={})
    check(r.status_code == 200, "200 OK", str(r.status_code))
    cl = c.get(f"{BOT_URL}/v1/healthz").json()["contexts_loaded"]
    check(all(v == 0 for v in cl.values()), "all contexts wiped", repr(cl))

    # ---- summary -----------------------------------------------------
    total = len(results)
    failed = [r for r in results if not r[0]]
    print(f"\n{'=' * 62}")
    print(f"  {total - len(failed)}/{total} checks passed")
    if failed:
        print(f"\n  Failures:")
        for _, label, detail in failed:
            print(f"    - {label}  {detail}")
    print(f"{'=' * 62}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
