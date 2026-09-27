"""Per-category voice handling, derived from the pushed CategoryContext.

Nothing here is hardcoded per category: tone, allowed vocabulary, taboos and
salutation style are all read out of ``category.voice`` as pushed. That matters
because Phase 3 of the harness pushes updated category contexts mid-test
(testing brief §4), and a bot with baked-in voice rules would ignore them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class VoicePack:
    slug: str
    tone: str                       # e.g. "peer_clinical"
    register: str                   # e.g. "respectful_collegial"
    code_mix: str                   # e.g. "hindi_english_natural"
    vocab_allowed: list[str] = field(default_factory=list)
    vocab_taboo: list[str] = field(default_factory=list)
    salutation_examples: list[str] = field(default_factory=list)
    tone_examples: list[str] = field(default_factory=list)
    authorities: list[str] = field(default_factory=list)
    journals: list[str] = field(default_factory=list)

    @property
    def is_clinical(self) -> bool:
        """Clinical/regulated verticals must never take a promotional register."""
        return any(t in self.tone for t in ("clinical", "medical", "pharma", "precise"))

    @property
    def allows_code_mix(self) -> bool:
        return "hindi" in self.code_mix.lower() or "natural" in self.code_mix.lower()

    def salutation(self, addressee: str, formal_hint: bool = False) -> str:
        """Address line matched to the category's own salutation examples.

        The dentists pack shows "Dr. Meera"-style forms, so an owner name is
        prefixed accordingly; retail/hospitality packs use the bare first name.
        """
        if not addressee:
            return ""
        examples = " ".join(self.salutation_examples).lower()
        wants_title = "dr" in examples or self.is_clinical
        already_titled = bool(re.match(r"^(dr\.?|mr\.?|ms\.?|mrs\.?)\s", addressee, re.I))
        if wants_title and not already_titled and formal_hint:
            return f"Dr. {addressee}"
        return addressee


def build_voice(category: dict) -> VoicePack:
    v = category.get("voice") or {}
    return VoicePack(
        slug=category.get("slug", "unknown"),
        tone=str(v.get("tone", "")),
        register=str(v.get("register", "")),
        code_mix=str(v.get("code_mix", "")),
        vocab_allowed=[str(x) for x in (v.get("vocab_allowed") or [])],
        vocab_taboo=[str(x) for x in (v.get("vocab_taboo") or [])],
        salutation_examples=[str(x) for x in (v.get("salutation_examples") or [])],
        tone_examples=[str(x) for x in (v.get("tone_examples") or [])],
        authorities=[str(x) for x in (category.get("regulatory_authorities") or [])],
        journals=[str(x) for x in (category.get("professional_journals") or [])],
    )


# ---------------------------------------------------------------------------
# Language selection
# ---------------------------------------------------------------------------

@dataclass
class LanguagePlan:
    code: str            # "en" | "hi-en" | "hi"
    use_code_mix: bool
    source: str          # provenance, for the rationale

    @property
    def label(self) -> str:
        return {"en": "English", "hi-en": "Hindi-English mix", "hi": "Hindi"}.get(self.code, self.code)


def plan_language(
    merchant: dict,
    customer: Optional[dict],
    voice: VoicePack,
) -> LanguagePlan:
    """Decide the outgoing language.

    A customer's ``language_pref`` is an explicit instruction and wins outright.
    For merchants the brief's FAQ says to match ``identity.languages``, defaulting
    to English; code-mix is used when the category's own voice profile invites it
    and the merchant lists Hindi.
    """
    if customer:
        pref = str((customer.get("identity") or {}).get("language_pref") or "en").lower()
        if pref in ("hi", "hindi"):
            return LanguagePlan("hi", True, "customer.identity.language_pref")
        if "hi" in pref:  # "hi-en mix"
            return LanguagePlan("hi-en", True, "customer.identity.language_pref")
        return LanguagePlan("en", False, "customer.identity.language_pref")

    langs = [str(x).lower() for x in ((merchant.get("identity") or {}).get("languages") or [])]
    if "hi" in langs and "en" not in langs:
        return LanguagePlan("hi", True, "merchant.identity.languages")
    if "hi" in langs and voice.allows_code_mix:
        return LanguagePlan("hi-en", True, "merchant.identity.languages + category.voice.code_mix")
    return LanguagePlan("en", False, "merchant.identity.languages")


# A small, deliberately conservative code-mix lexicon. Used only for connective
# tissue -- never for the factual claims, which stay in their source form so the
# numbers and citations remain verifiable.
CODE_MIX_PHRASES = {
    "want_me_to": "Chahein to main",
    "shall_i": "Main",
    "for_you": "aapke liye",
    "your": "aapka",
    "is_ready": "ready hai",
    "two_minutes": "2 minute ka kaam hai",
    "ok": "Chalega?",
    "tell_me": "bataiye",
    "right_now": "abhi",
    "will_do": "kar deti hoon",
}
