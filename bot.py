"""Vera bot -- HTTP surface for the magicpin AI Challenge judge harness.

Implements the 5 endpoints in challenge-testing-brief.md §2, plus the optional
POST /v1/teardown (§11 state wipe). Adapted from the reference skeleton in §7.

Design notes
------------
* The HTTP layer never composes. It resolves contexts, enforces the operational
  rules (suppression, anti-repetition, per-tick caps, time budget) and delegates
  to compose() in composer.py so composition stays independently testable.
* Every handler is wall-clock budgeted. The brief allows 30s but the bundled
  judge_simulator.py uses a 15s client timeout, so the budget is set below that
  and a tick that runs out returns the actions it already has rather than
  blocking (§5 "return an empty/immediate response rather than hanging").
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import conversation_handlers
from composer import compose
from conversation_handlers import respond
from store import VALID_SCOPES, ContextStore, ConversationStore

app = FastAPI(title="Vera Bot", version="1.0.0")
START = time.time()

contexts = ContextStore()
conversations = ConversationStore()

# Operational limits from challenge-testing-brief.md §5
MAX_ACTIONS_PER_TICK = 20
TICK_BUDGET_S = float(os.getenv("VERA_TICK_BUDGET_S", "12.0"))
REPLY_BUDGET_S = float(os.getenv("VERA_REPLY_BUDGET_S", "12.0"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# GET /  -- not part of the judge contract, which only calls /v1/*. It exists so
# that a human opening the submitted URL sees a live service rather than a bare
# 404, which reads as a broken deployment.
# ---------------------------------------------------------------------------

@app.get("/")
async def index() -> dict[str, Any]:
    return {
        "service": "Vera — magicpin AI Challenge bot",
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": contexts.counts(),
        "endpoints": {
            "GET /v1/healthz": "liveness + context counts",
            "GET /v1/metadata": "bot identity",
            "POST /v1/context": "receive a context push (idempotent on scope+id+version)",
            "POST /v1/tick": "periodic wake-up; may return proactive actions",
            "POST /v1/reply": "receive a merchant/customer reply",
            "POST /v1/teardown": "wipe all state",
        },
        "docs": "/docs",
    }


# ---------------------------------------------------------------------------
# 2.4  GET /v1/healthz
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": contexts.counts(),
    }


# ---------------------------------------------------------------------------
# 2.5  GET /v1/metadata
# ---------------------------------------------------------------------------

@app.get("/v1/metadata")
async def metadata() -> dict[str, Any]:
    return {
        "team_name": os.getenv("VERA_TEAM_NAME", "Niyati"),
        "team_members": [os.getenv("VERA_TEAM_MEMBER", "Niyati")],
        "model": os.getenv("VERA_MODEL_LABEL", "deterministic-composer + qwen2.5:7b-instruct (local)"),
        "approach": (
            "Deterministic per-category/per-trigger-kind composer with slot-filled "
            "facts (fabrication-proof by construction), optional local-LLM phrasing "
            "layer behind a disk cache, rubric self-check before send, rule-based "
            "multi-turn handling (auto-reply detection, intent pivot, graceful exit)."
        ),
        "contact_email": os.getenv("VERA_CONTACT_EMAIL", "niyati.swaroop@gmail.com"),
        "version": "1.0.0",
        "submitted_at": os.getenv("VERA_SUBMITTED_AT", "2026-09-27T00:00:00Z"),
    }


# ---------------------------------------------------------------------------
# 2.1  POST /v1/context
# ---------------------------------------------------------------------------

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
async def push_context(body: CtxBody) -> JSONResponse:
    if body.scope not in VALID_SCOPES:
        return JSONResponse(
            status_code=400,
            content={
                "accepted": False,
                "reason": "invalid_scope",
                "details": f"scope must be one of {list(VALID_SCOPES)}, got {body.scope!r}",
            },
        )
    if not isinstance(body.payload, dict) or not body.payload:
        return JSONResponse(
            status_code=400,
            content={
                "accepted": False,
                "reason": "invalid_payload",
                "details": "payload must be a non-empty object",
            },
        )

    result = contexts.put(body.scope, body.context_id, body.version, body.payload)
    if not result["accepted"]:
        # Example 1.5: re-posting a version we already hold answers 409.
        return JSONResponse(status_code=409, content=result)

    return JSONResponse(
        status_code=200,
        content={
            "accepted": True,
            "ack_id": f"ack_{body.context_id}_v{body.version}",
            "stored_at": _now_iso(),
        },
    )


# ---------------------------------------------------------------------------
# 2.2  POST /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: Optional[str] = None
    available_triggers: list[str] = Field(default_factory=list)


@app.post("/v1/tick")
async def tick(body: TickBody) -> dict[str, Any]:
    deadline = time.time() + TICK_BUDGET_S
    actions: list[dict[str, Any]] = []
    # One action per (merchant_id, conversation_id) per tick -- testing brief FAQ.
    claimed: set[str] = set()

    for trigger_id in body.available_triggers:
        if len(actions) >= MAX_ACTIONS_PER_TICK or time.time() >= deadline:
            break

        bundle = contexts.resolve_bundle(trigger_id)
        if bundle is None:
            continue

        trigger = bundle["trigger"]
        merchant = bundle["merchant"]
        merchant_id = merchant.get("merchant_id")

        suppression_key = trigger.get("suppression_key", "")
        if conversations.is_suppressed(suppression_key):
            continue

        conversation_id = _conversation_id(merchant_id, trigger_id)
        if conversation_id in claimed or conversations.exists(conversation_id):
            # Continuing an existing conversation is /v1/reply's job, not tick's.
            continue

        try:
            composed = compose(
                bundle["category"], merchant, trigger, bundle["customer"]
            )
        except Exception:
            # A composition failure must never fail the whole tick.
            continue

        if not composed.get("body"):
            continue

        # Don't say the same words to the same merchant twice, even across
        # conversations -- distinct placeholder triggers of one kind compose
        # identically for a merchant whose state hasn't changed.
        if conversations.sent_to_merchant(merchant_id, composed["body"]):
            continue

        action = {
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": trigger.get("customer_id"),
            "send_as": composed["send_as"],
            "trigger_id": trigger_id,
            "template_name": composed.get("template_name", "vera_generic_v1"),
            "template_params": composed.get("template_params", []),
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": composed.get("suppression_key", suppression_key),
            "rationale": composed.get("rationale", ""),
        }
        actions.append(action)
        claimed.add(conversation_id)

        conversations.start(
            conversation_id, merchant_id, trigger.get("customer_id"), trigger_id
        )
        conversations.record_outbound(conversation_id, composed["body"], trigger_id=trigger_id)
        conversations.suppress(action["suppression_key"], conversation_id)

    return {"actions": actions}


def _conversation_id(merchant_id: Optional[str], trigger_id: str) -> str:
    """Decodable and resumable (case-studies.md cross-pattern #8)."""
    return f"conv_{merchant_id or 'unknown'}__{trigger_id}"


# ---------------------------------------------------------------------------
# 2.3  POST /v1/reply
# ---------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str = ""
    received_at: Optional[str] = None
    turn_number: int = 0


@app.post("/v1/reply")
async def reply(body: ReplyBody) -> dict[str, Any]:
    deadline = time.time() + REPLY_BUDGET_S

    # The judge may reply on a conversation we have no record of (e.g. the
    # replay scenarios in §4 Phase 4, which start cold). Treat it as real.
    conv = conversations.start(
        body.conversation_id, body.merchant_id, body.customer_id, None
    )
    conversations.record_inbound(
        body.conversation_id, body.message, turn_number=body.turn_number
    )

    bundle = None
    if conv.get("trigger_id"):
        bundle = contexts.resolve_bundle(conv["trigger_id"])
    if bundle is None and body.merchant_id:
        merchant = contexts.get("merchant", body.merchant_id)
        if merchant:
            bundle = {
                "category": contexts.get("category", merchant.get("category_slug")),
                "merchant": merchant,
                "trigger": None,
                "customer": contexts.get("customer", body.customer_id),
            }

    try:
        result = respond(
            conversation=conv,
            message=body.message,
            bundle=bundle,
            deadline=deadline,
        )
    except Exception:
        # Never return malformed JSON (-2 per the failure table); end cleanly.
        return {
            "action": "end",
            "rationale": "Internal error while composing the reply; ending rather than sending malformed output.",
        }

    if result.get("action") == "send":
        text = result.get("body") or ""
        if not text or conversations.already_sent(body.conversation_id, text):
            # Empty body counts as malformed; a verbatim repeat costs -2.
            return {
                "action": "end",
                "rationale": "Nothing new to add without repeating a message already sent; exiting gracefully.",
            }
        conversations.record_outbound(body.conversation_id, text)
    elif result.get("action") == "end":
        conversations.mark_ended(body.conversation_id)

    return result


# ---------------------------------------------------------------------------
# Optional POST /v1/teardown  (testing brief §11)
# ---------------------------------------------------------------------------

@app.post("/v1/teardown")
async def teardown() -> dict[str, Any]:
    n_ctx = contexts.wipe()
    n_conv = conversations.wipe()
    conversation_handlers.reset()
    return {
        "ok": True,
        "wiped": {"contexts": n_ctx, "conversations": n_conv},
        "wiped_at": _now_iso(),
    }
