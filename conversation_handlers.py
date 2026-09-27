"""Multi-turn reply handling (challenge-brief.md §7.4, §12).

Stage 2: stub, so the HTTP contract can be verified on its own. Stage 4 builds
the real handler -- auto-reply detection, intent-pivot routing, graceful exit.
The signature below is final.
"""

from __future__ import annotations

from typing import Any, Optional


def respond(
    conversation: dict,
    message: str,
    bundle: Optional[dict] = None,
    deadline: Optional[float] = None,
) -> dict[str, Any]:
    """Given the conversation so far + the latest inbound, produce the next move.

    Returns one of:
      {"action": "send", "body": ..., "cta": ..., "rationale": ...}
      {"action": "wait", "wait_seconds": int, "rationale": ...}
      {"action": "end", "rationale": ...}
    """
    return {
        "action": "send",
        "body": f"[stub] received {len(message)} chars on turn {len(conversation.get('turns', []))}.",
        "cta": "open_ended",
        "rationale": "[stub] stage 4 replaces this with real intent routing.",
    }
