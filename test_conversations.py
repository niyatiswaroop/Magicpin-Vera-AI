#!/usr/bin/env python3
"""Multi-turn transcript tests for conversation_handlers.respond().

Invented transcripts, run in-process against the handler -- no server, no model.
Covers the three replay scenarios in testing brief §4 Phase 4 plus the patterns
in challenge-brief.md §12.

    .venv/bin/python test_conversations.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import conversation_handlers as ch

D = Path(__file__).parent / "dataset" / "expanded"

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label, detail))
    print(f"    [{PASS if ok else FAIL}] {label}" + (f"  — {detail}" if detail else ""))


def load_bundle(merchant_file: str) -> dict:
    m = json.loads((D / "merchants" / merchant_file).read_text())
    c = json.loads((D / "categories" / f"{m['category_slug']}.json").read_text())
    trg = next(
        (json.loads(p.read_text()) for p in (D / "triggers").glob("*.json")
         if json.loads(p.read_text()).get("merchant_id") == m["merchant_id"]),
        None,
    )
    return {"merchant": m, "category": c, "trigger": trg, "customer": None}


def new_conv(merchant_id: str, bot_turns: int = 1) -> dict:
    return {
        "conversation_id": f"conv_{merchant_id}",
        "merchant_id": merchant_id,
        "customer_id": None,
        "trigger_id": None,
        "turns": [{"from": "bot", "body": f"outbound {i}"} for i in range(bot_turns)],
        "bodies_sent": [f"outbound {i}" for i in range(bot_turns)],
        "ended": False,
        "nudges_unanswered": 0,
    }


BUNDLE = load_bundle("m_001_drmeera_dentist_delhi.json")
MID = BUNDLE["merchant"]["merchant_id"]


# ---------------------------------------------------------------------------

def scenario_auto_reply_hell() -> None:
    """Phase 4 #1 -- same canned text 4x. Must probe once, then exit.

    Reproduces the harness exactly: a *different* conversation_id each turn.
    """
    print("\nScenario 1 — auto-reply hell (4x identical, rotating conversation_id)")
    ch.reset()
    auto = "Thank you for contacting us! Our team will respond shortly."
    actions = []
    for i in range(1, 5):
        conv = new_conv(MID)
        conv["conversation_id"] = f"conv_auto_{i}"
        r = ch.respond(conv, auto, BUNDLE)
        actions.append(r["action"])
        print(f"      turn {i}: {r['action']}"
              + (f" — \"{r.get('body','')[:64]}...\"" if r["action"] == "send" else ""))
    check(actions.count("send") <= 1, "at most one probe sent",
          f"sends={actions.count('send')}")
    check("end" in actions, "ended rather than looping", f"actions={actions}")
    check(actions.index("end") <= 1, "ended by turn 2 at the latest",
          f"ended at turn {actions.index('end') + 1}")

    print("    Hindi canned auto-reply (brief Pattern B wording)")
    ch.reset()
    hi_auto = ("Aapki jaankari ke liye bahut-bahut shukriya. Main aapki yeh sabhi "
               "baatein hamari team tak pahuncha deti hoon.")
    r1 = ch.respond(new_conv(MID), hi_auto, BUNDLE)
    r2 = ch.respond(new_conv(MID), hi_auto, BUNDLE)
    check(r1["action"] == "send" and r2["action"] == "end",
          "Hindi auto-reply: probe then exit", f"{r1['action']} -> {r2['action']}")
    check(ch.detect_language(hi_auto) in ("hi", "hi-en"),
          "probe answers in the merchant's language")


def scenario_intent_transition() -> None:
    """Phase 4 #2 -- commitment must route to action, never re-qualify."""
    print("\nScenario 2 — intent transition")
    ch.reset()
    # The judge's own assertions, lifted from judge_simulator._intent
    qualifying = ["would you", "do you", "can you tell", "what if", "how about"]
    actioning = ["done", "sending", "draft", "here", "confirm", "proceed", "next"]

    for msg in ("Ok lets do it. Whats next?",
                "yes I want to join",
                "go ahead",
                "haan kar do",
                "Mujhe magicpin judrna hai"):
        ch.reset()
        r = ch.respond(new_conv(MID, bot_turns=2), msg, BUNDLE)
        body = (r.get("body") or "").lower()
        ok_action = r["action"] == "send" and any(w in body for w in actioning)
        no_qual = not any(w in body for w in qualifying)
        check(ok_action and no_qual, f"\"{msg[:34]}\" -> action mode",
              f"action={r['action']} body={body[:70]!r}")


def scenario_hostile_and_offtopic() -> None:
    """Phase 4 #3 -- abuse then an unrelated question."""
    print("\nScenario 3 — hostile, then off-topic")
    ch.reset()
    r = ch.respond(new_conv(MID), "Stop messaging me. This is useless spam.", BUNDLE)
    check(r["action"] == "end", "hostile -> end", f"got {r['action']}")

    ch.reset()
    r = ch.respond(new_conv(MID, bot_turns=1),
                   "can you also help me file my GST?", BUNDLE)
    body = (r.get("body") or "")
    check(r["action"] == "send", "off-topic -> stays engaged", f"got {r['action']}")
    check("gst" not in body.lower() or "outside" in body.lower()
          or "scope" in body.lower(),
          "declines the off-mission ask rather than attempting it")
    check("listing" in body.lower() or "magicpin" in body.lower(),
          "redirects back to mission")
    print(f"      \"{body[:110]}\"")


def scenario_graceful_exit() -> None:
    print("\nScenario 4 — knowing when to stop (brief §12.5)")
    ch.reset()
    r = ch.respond(new_conv(MID), "not interested", BUNDLE)
    check(r["action"] == "end", "explicit disinterest -> end", f"got {r['action']}")

    ch.reset()
    conv = new_conv(MID, bot_turns=3)
    conv["nudges_unanswered"] = 3
    r = ch.respond(conv, "hmm", BUNDLE)
    check(r["action"] == "end", "3 unanswered nudges -> end", f"got {r['action']}")

    ch.reset()
    conv = new_conv(MID, bot_turns=6)
    r = ch.respond(conv, "hmm ok maybe", BUNDLE)
    check(r["action"] == "end", "turn ceiling -> end", f"got {r['action']}")

    ch.reset()
    conv = new_conv(MID)
    conv["ended"] = True
    r = ch.respond(conv, "hello?", BUNDLE)
    check(r["action"] == "end", "closed conversation is not reopened")


def scenario_defer_and_questions() -> None:
    print("\nScenario 5 — deferral and questions")
    ch.reset()
    r = ch.respond(new_conv(MID), "call me tomorrow, busy now", BUNDLE)
    check(r["action"] == "wait", "deferral -> wait", f"got {r['action']}")
    check(isinstance(r.get("wait_seconds"), int) and r["wait_seconds"] > 0,
          "wait carries wait_seconds", str(r.get("wait_seconds")))

    ch.reset()
    r = ch.respond(new_conv(MID), "how many views did I get?", BUNDLE)
    body = r.get("body", "")
    check("2,410" in body, "answers views from pushed context", body[:80])

    ch.reset()
    r = ch.respond(new_conv(MID), "what is my ctr?", BUNDLE)
    check("2.1%" in r.get("body", ""), "answers CTR from context",
          r.get("body", "")[:80])

    ch.reset()
    r = ch.respond(new_conv(MID), "how many followers on instagram?", BUNDLE)
    body = r.get("body", "").lower()
    check("don't have" in body or "won't guess" in body or "nahi hai" in body,
          "unanswerable question -> admits the gap, no invention", body[:90])


def scenario_language_switch() -> None:
    """Brief §12.4 -- the merchant may switch language mid-conversation."""
    print("\nScenario 6 — per-turn language switching")
    checks = [
        ("How many views this month?", "en"),
        ("kitne views aaye is mahine?", "hi-en"),
        ("मुझे यह पसंद है", "hi"),
    ]
    for msg, want in checks:
        got = ch.detect_language(msg)
        check(got == want, f"detect {want!r} in \"{msg[:30]}\"", f"got {got!r}")

    ch.reset()
    r = ch.respond(new_conv(MID, bot_turns=2), "haan kar do", BUNDLE)
    body = r.get("body", "")
    check(any(w in body.lower() for w in ("main", "bhej", "kar", "aapko")),
          "Hindi commitment gets a Hindi reply", body[:80])


def scenario_no_context() -> None:
    """The replay scenarios start cold -- no merchant context pushed."""
    print("\nScenario 7 — replay with no pushed context")
    ch.reset()
    r = ch.respond(new_conv("m_unknown"), "ok lets do it", None)
    check(r["action"] == "send", "still routes to action with no bundle",
          f"got {r['action']}")
    check(bool(r.get("body")), "non-empty body")
    ch.reset()
    r = ch.respond(new_conv("m_unknown"), "stop messaging me", None)
    check(r["action"] == "end", "still exits on hostility with no bundle")


def scenario_shapes() -> None:
    """Every return must satisfy the §2.3 response schema."""
    print("\nScenario 8 — response schema on every path")
    msgs = ["Thank you for contacting us!", "lets do it", "not interested",
            "call me later", "what is my ctr?", "help me with GST",
            "stop messaging me", "asdfgh", ""]
    for m in msgs:
        ch.reset()
        r = ch.respond(new_conv(MID), m, BUNDLE)
        ok = r.get("action") in ("send", "wait", "end") and bool(r.get("rationale"))
        if r.get("action") == "send":
            ok = ok and bool(r.get("body")) and bool(r.get("cta"))
        if r.get("action") == "wait":
            ok = ok and isinstance(r.get("wait_seconds"), int)
        check(ok, f"schema ok for {m[:28]!r}", json.dumps(r)[:70])


def main() -> int:
    for fn in (scenario_auto_reply_hell, scenario_intent_transition,
               scenario_hostile_and_offtopic, scenario_graceful_exit,
               scenario_defer_and_questions, scenario_language_switch,
               scenario_no_context, scenario_shapes):
        fn()

    failed = [r for r in results if not r[0]]
    print(f"\n{'=' * 62}")
    print(f"  {len(results) - len(failed)}/{len(results)} checks passed")
    for _, label, detail in failed:
        print(f"    FAILED: {label}  {detail}")
    print(f"{'=' * 62}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
