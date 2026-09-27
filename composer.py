"""compose() -- the composition contract from challenge-brief.md §5/§7.1.

Architecture
------------
A deterministic composer is the backbone; a local open-source model is an
optional phrasing layer on top.

    facts ──► kind spec ──► deterministic assembly ──► validate ──► body
                                     │                     ▲
                                     └─► local LLM rephrase ┘ (strict gate)

Why this way round, rather than prompting a model with the four contexts:

* **Fabrication is structurally impossible in the backbone.** It can only emit
  values that came out of ``build_factset``, each carrying a provenance path.
  Hallucination is the most heavily penalised failure in the rubric (§11) and
  the adaptive-injection phase is designed to catch exactly it.
* **Determinism is free.** No sampling, no cache needed to guarantee the brief's
  "deterministic given the same inputs" requirement.
* **It always meets the time budget.** Assembly is sub-millisecond, so /v1/tick
  can never time out waiting on a model.

The LLM layer only ever *rephrases* an already-valid message, is handed a
restricted fact list, and its output is re-validated in strict mode -- if it
introduces any number it wasn't given, the deterministic body is kept instead.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from facts import Fact, FactSet, build_factset, resolve_digest_item, validate
from kinds import (
    ASK_MERCHANT,
    CURIOSITY,
    EFFORT_EXTERNALIZATION,
    GENERIC_HOOKS,
    GENERIC_HOOKS_CUSTOMER,
    HOOKS,
    LOSS_AVERSION,
    RECIPROCITY,
    SOCIAL_PROOF,
    KindSpec,
    reframed_concept,
    reframed_work,
    spec_for,
)
from voice import LanguagePlan, VoicePack, build_voice, plan_language

CTA_OPEN_ENDED = "open_ended"
CTA_BINARY_YES_NO = "binary_yes_no"
CTA_BINARY_CONFIRM_CANCEL = "binary_confirm_cancel"
CTA_MULTI_CHOICE_SLOT = "multi_choice_slot"
CTA_NONE = "none"

SEND_AS_VERA = "vera"
SEND_AS_MERCHANT = "merchant_on_behalf"

# The LLM phrasing layer is opt-in, so the bot's behaviour under the judge is
# the deterministic behaviour unless explicitly enabled.
USE_LLM = os.getenv("VERA_USE_LLM", "0") not in ("0", "", "false", "False")


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

_OPT_RE = re.compile(r"\[\[(.*?)\]\]", re.S)
_KEY_RE = re.compile(r"\{([a-z0-9_.]+)\}")


def starts_with_fact(template: str) -> bool:
    """True when a template opens with a substituted value rather than prose.

    Hooks that begin with a fact must keep that value's own capitalisation:
    lowercasing to join it after a salutation turns "DC vs MI" into "dC vs MI"
    and "Diwali" into "diwali".
    """
    return template.lstrip().startswith("{")


def render(template: str, fs: FactSet) -> tuple[str, list[Fact]]:
    """Render a hook template, dropping optional clauses with missing facts.

    Returns the text plus the facts actually cited, which is what the strict
    validation gate and the rationale are built from.
    """
    cited: list[Fact] = []

    # Required slots are resolved first so an optional clause can be dropped when
    # the fact it would add is already stated. Digest titles often embed their own
    # date ("...effective 2026-12-15"), and appending "— deadline 15 Dec" after one
    # says the same thing twice. Both the rendered text and the raw value are
    # checked, because the two spellings of a date differ.
    required_text = _KEY_RE.sub(
        lambda m: (fs.get(m.group(1)).text if fs.get(m.group(1)) else ""),
        _OPT_RE.sub("", template),
    ).lower()

    def _optional(m: re.Match) -> str:
        inner = m.group(1)
        keys = _KEY_RE.findall(inner)
        facts = [fs.get(k) for k in keys]
        if any(f is None for f in facts):
            return ""
        if facts and all(
            (f.text and f.text.lower() in required_text)
            or (isinstance(f.value, str) and f.value and f.value.lower() in required_text)
            for f in facts
        ):
            return ""
        return inner

    text = _OPT_RE.sub(_optional, template)

    def _substitute(m: re.Match) -> str:
        f = fs.get(m.group(1))
        if f is None:
            return "\x00"  # marks an unsatisfiable required slot
        cited.append(f)
        return f.text

    text = _KEY_RE.sub(_substitute, text)
    if "\x00" in text:
        return "", []
    return _tidy(text), cited


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,.;:])(?=[^\s\d])", r"\1 ", text)
    text = re.sub(r"\.\s*\.", ".", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\s+—\s*\.", ".", text)
    return text.strip(" ,;—-")


# ---------------------------------------------------------------------------
# The public contract
# ---------------------------------------------------------------------------

def compose(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
    previous_bodies: Optional[list[str]] = None,
    use_llm: Optional[bool] = None,
) -> dict[str, Any]:
    """Compose the next outbound message from the 4 contexts.

    Deterministic given identical inputs. Returns body, cta, send_as,
    suppression_key and rationale, plus template_name/template_params for the
    WhatsApp first-outbound template requirement (brief §5 constraint 1).
    """
    kind = trigger.get("kind", "unknown")
    spec = spec_for(kind)
    fs = build_factset(category, merchant, trigger, customer)
    voice = build_voice(category)
    lang = plan_language(merchant, customer, voice)

    is_customer_facing = bool(customer) or trigger.get("scope") == "customer"
    send_as = SEND_AS_MERCHANT if is_customer_facing else SEND_AS_VERA

    parts = _assemble(spec, fs, voice, lang, category, merchant, trigger, customer)
    body = parts["body"]
    cta = parts["cta"]
    cited = parts["cited"]

    # Deterministic output is checked against the closed world.
    v = validate(body, cta, fs, category, merchant, trigger, customer, previous_bodies)
    if not v.ok:
        # Fall back to the safest possible construction rather than emit a body
        # that trips a hard rule.
        body, cta, cited = _safe_fallback(fs, voice, lang, spec, is_customer_facing)
        v = validate(body, cta, fs, category, merchant, trigger, customer, previous_bodies)

    # Optional phrasing pass -- never allowed to introduce a new fact.
    if (USE_LLM if use_llm is None else use_llm) and body:
        polished = _llm_rephrase(
            body, cited, spec, voice, lang, fs,
            category, merchant, trigger, customer, previous_bodies,
        )
        if polished:
            body = polished

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key")
        or f"{kind}:{merchant.get('merchant_id', '')}",
        "rationale": _rationale(spec, fs, cited, lang, parts, trigger, category, v),
        "template_name": f"{'merchant' if is_customer_facing else 'vera'}_{kind}_v1",
        "template_params": _template_params(fs, parts),
        # Not part of the judge's schema; used by the local eval harness.
        "_meta": {
            "levers": list(parts["levers_fired"]),
            "cited": [f"{f.key}={f.text}" for f in cited],
            "provenance": [f.source for f in cited],
            "language": lang.code,
            "validation_ok": v.ok,
            "validation_errors": v.errors,
            "validation_warnings": v.warnings,
            "kind_known": kind in HOOKS,
        },
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _assemble(
    spec: KindSpec,
    fs: FactSet,
    voice: VoicePack,
    lang: LanguagePlan,
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict],
) -> dict[str, Any]:
    cited: list[Fact] = []
    levers_fired: list[str] = []

    # 1. Addressee -- owner first name where present (case-studies #3).
    addressee_fact = fs.get("cust.name") if customer else fs.get("owner")
    addressee = addressee_fact.text if addressee_fact else ""
    if addressee_fact:
        cited.append(addressee_fact)
    salutation = voice.salutation(addressee, formal_hint=not customer)

    # 2. Hook -- the why-now, carrying the specificity anchor.
    is_customer_facing = bool(customer) or trigger.get("scope") == "customer"
    hook, hook_cited = render(HOOKS.get(spec.kind, ""), fs)
    hook_opens_on_fact = bool(hook) and starts_with_fact(HOOKS.get(spec.kind, ""))

    # A placeholder perf trigger names no metric, but the merchant's own
    # delta_7d does. Deriving it turns "a metric dropped week-over-week" into
    # "your calls are down 27% week-on-week" using data already pushed -- a real
    # specificity gain on the sparse pairs, with no invention.
    if not hook and spec.kind in ("perf_dip", "perf_spike", "seasonal_perf_dip"):
        derived, derived_cited = _derive_perf_hook(fs, spec.kind)
        if derived:
            hook, hook_cited = derived, derived_cited
            hook_opens_on_fact = False

    if not hook:
        # 75 of the 100 generated triggers carry {"placeholder": true} with no
        # usable payload, so the kind's own template renders empty. The trigger's
        # *kind* is still real information the judge pushed, so the why-now is
        # stated from the spec and anchored on whatever fact is available. Losing
        # the reason entirely would forfeit Trigger relevance on 14 of the 30
        # canonical pairs.
        ladder = GENERIC_HOOKS_CUSTOMER if is_customer_facing else GENERIC_HOOKS
        anchor = ""
        for generic in ladder:
            anchor, hook_cited = render(generic, fs)
            if anchor:
                break
        # A reframed kind supplies its own why-now, so adding the spec's would
        # reintroduce the very vocabulary the reframe exists to remove
        # ("your regular course is about to run out" to a dentist's patient).
        why = (
            ""
            if (reframed_concept(spec, category.get("slug", "")) or is_customer_facing)
            else spec.why_now.strip()
        )
        if why and anchor:
            hook = f"{why[0].upper()}{why[1:]} — {anchor[0].lower()}{anchor[1:]}"
        else:
            hook = anchor or (f"{why[0].upper()}{why[1:]}." if why else "")
    cited.extend(hook_cited)

    # A kind that doesn't fit this vertical gets its concept restated in the
    # category's own terms rather than importing foreign vocabulary.
    reframe = reframed_concept(spec, category.get("slug", ""))
    if reframe:
        lowered = hook[0].lower() + hook[1:] if hook else ""
        hook = f"{reframe} — {lowered}".rstrip(" —")

    # 3. Relevance -- the lever sentence, tying the hook to this merchant.
    # Suppressed for customer-facing sends: peer benchmarks and cohort counts are
    # things Vera tells the merchant, never things the merchant tells a customer.
    if is_customer_facing:
        relevance, rel_cited, rel_levers = "", [], []
    else:
        relevance, rel_cited, rel_levers = _relevance(
            spec, fs, voice, category, merchant, customer
        )
    cited.extend(rel_cited)
    levers_fired.extend(rel_levers)

    # 4. The ask -- single CTA, lands last.
    ask, ask_cited, cta, ask_levers = _ask(
        spec, fs, voice, lang, customer, category.get("slug", "")
    )
    cited.extend(ask_cited)
    levers_fired.extend(ask_levers)

    # 5. Trailing citation when the claim is research/regulatory.
    citation = ""
    src = fs.get("digest.source")
    if src and spec.kind in (
        "research_digest", "regulation_change", "cde_opportunity", "supply_alert",
    ):
        if src.text not in hook:
            citation = f" — {src.text}"
            cited.append(src)

    # 6. Code-mix, applied to the ask and connectives only. Facts stay in their
    # source form so numbers, prices and citations remain verifiable -- which is
    # also how Indian merchants actually write Hinglish.
    if lang.use_code_mix:
        ask = _code_mix_ask(ask, cta, lang)

    segments = [s for s in (hook, relevance, ask) if s]
    body = " ".join(segments)
    if salutation and body:
        # Only downcase prose. A hook opening on a fact value keeps its case.
        opens_on_fact = (not reframe) and hook_opens_on_fact
        lead = body if opens_on_fact else body[0].lower() + body[1:]
        body = f"{salutation}, {lead}"
    elif salutation:
        body = salutation
    body = _tidy(body) + citation

    # Dedupe cited facts, preserving order.
    seen: set[str] = set()
    uniq: list[Fact] = []
    for f in cited:
        if f.key not in seen:
            seen.add(f.key)
            uniq.append(f)

    return {
        "body": body,
        "cta": cta,
        "cited": uniq,
        "levers_fired": sorted(set(levers_fired)),
        "hook": hook,
        "relevance": relevance,
        "ask": ask,
        "salutation": salutation,
    }


# Kinds whose subject is the merchant's own numbers, where a peer benchmark
# reads as a natural next sentence.
_COHORT_TOPIC_KINDS = frozenset({
    "winback_eligible", "dormant_with_vera", "renewal_due", "perf_dip",
    "seasonal_perf_dip", "review_theme_emerged", "research_digest",
    "category_seasonal", "curious_ask_due", "scheduled_recurring",
    "festival_upcoming", "gbp_unverified",
})

_PERF_TOPIC_KINDS = frozenset({
    "perf_dip", "perf_spike", "seasonal_perf_dip", "gbp_unverified",
    "competitor_opened", "renewal_due", "winback_eligible",
    "review_theme_emerged", "milestone_reached", "dormant_with_vera",
    "scheduled_recurring",
})


def _relevance(
    spec: KindSpec,
    fs: FactSet,
    voice: VoicePack,
    category: dict,
    merchant: dict,
    customer: Optional[dict],
) -> tuple[str, list[Fact], list[str]]:
    """Build the lever sentence: why this matters to *this* merchant.

    Social proof is preferred where a peer benchmark is genuinely available,
    because the brief names it as production Vera's biggest miss (§10).
    """
    cited: list[Fact] = []
    fired: list[str] = []

    # -- social proof via an attributed peer benchmark ------------------
    # Only where the merchant's own performance is the subject of the message.
    # After a seasonal or festival hook a peer-CTR line is a non-sequitur, so
    # those kinds fall through to a lever that actually connects.
    if SOCIAL_PROOF in spec.levers and spec.kind in _PERF_TOPIC_KINDS:
        pairs = (
            ("perf.ctr", "peer.ctr", "click-through"),
            ("perf.views", "peer.views", "views"),
            ("perf.calls", "peer.calls", "calls"),
        )
        for mine_key, peer_key, label in pairs:
            mine, peer = fs.get(mine_key), fs.get(peer_key)
            if not (mine and peer):
                continue
            try:
                behind = float(mine.value) < float(peer.value)
            except (TypeError, ValueError):
                continue
            cited.extend([mine, peer])
            fired.append(SOCIAL_PROOF)
            scope = (category.get("peer_stats") or {}).get("scope", "peers")
            # "metro_solo_practices_2026" -> "metro solo practices"; the trailing
            # year is dataset bookkeeping, not something to say out loud.
            scope_label = re.sub(r"[\s_]*(19|20)\d{2}$", "", str(scope)).replace("_", " ")
            if behind:
                return (
                    f"You're at {mine.text} {label} against {peer.text} for {scope_label}.",
                    cited, fired,
                )
            return (
                f"That puts you at {mine.text} {label} against {peer.text} for {scope_label}.",
                cited, fired,
            )

    # -- loss aversion on the merchant's own cohort ---------------------
    # Only where the customer base is the subject. After a regulatory or supply
    # alert, "78 of your customers haven't been back" is a non-sequitur.
    if LOSS_AVERSION in spec.levers and spec.kind in _COHORT_TOPIC_KINDS:
        for key, phrase in (
            ("agg.lapsed_180d_plus", "of your customers haven't been back in 180 days"),
            ("agg.high_risk_adult_count", "of your patients sit in exactly that group"),
            ("signal.stale_days", "days since your last Google post"),
        ):
            f = fs.get(key)
            if f:
                cited.append(f)
                fired.append(LOSS_AVERSION)
                return f"{f.text} {phrase}.", cited, fired

    # -- reciprocity / curiosity from the merchant's own catalogue -------
    if RECIPROCITY in spec.levers or CURIOSITY in spec.levers:
        offer, is_theirs = _preferred_offer(fs)
        if offer and spec.kind not in ("curious_ask_due",):
            cited.append(offer)
            fired.append(RECIPROCITY if RECIPROCITY in spec.levers else CURIOSITY)
            if is_theirs:
                return f"Sits naturally next to your {offer.text}.", cited, fired
            # The title came from the category's template list, not this
            # merchant's catalogue. Saying "your X" would credit them with an
            # offer they don't run -- a fabrication about their own account.
            return (
                f"You've no active offer running — {offer.text} is the usual "
                f"starter in your category.",
                cited, fired,
            )

    return "", cited, fired


def _derive_perf_hook(fs: FactSet, kind: str) -> tuple[str, list[Fact]]:
    """Name the metric that actually moved, from merchant.performance.delta_7d.

    Used when a perf trigger arrives with a placeholder payload. The direction
    must agree with the trigger's kind -- a "dip" trigger is only described as a
    dip if a metric genuinely fell, otherwise nothing is asserted.
    """
    want_drop = kind in ("perf_dip", "seasonal_perf_dip")
    best: Optional[tuple[str, Fact, float]] = None
    for metric in ("calls", "views", "ctr"):
        f = fs.get(f"delta.{metric}")
        if not f:
            continue
        try:
            raw = float(f.value)
        except (TypeError, ValueError):
            continue
        if want_drop and raw >= 0:
            continue
        if not want_drop and raw <= 0:
            continue
        if best is None or abs(raw) > abs(best[2]):
            best = (metric, f, raw)

    if best is None:
        return "", []
    metric, delta_fact, _raw = best
    direction = "down" if want_drop else "up"
    cited = [delta_fact]

    level = fs.get(f"perf.{metric}") or fs.get("perf.views")
    tail = ""
    if level:
        cited.append(level)
        tail = f" — {level.text} {level.label}"
    return f"Your {metric} are {direction} {delta_fact.text} week-on-week{tail}.", cited


_DISCOUNT_RE = re.compile(r"\b\d+\s*%\s*(off|discount)|\bflat\s+\d+", re.I)


def _preferred_offer(fs: FactSet) -> tuple[Optional[Fact], bool]:
    """Pick a service@price offer over a flat-discount one.

    The brief is explicit that discount framings rarely engage Indian merchants
    while service+price does (§3.3, §5.5), so "Haircut @ ₹99" is preferred to
    "Flat 30% OFF" even when the discount offer is the merchant's active one.

    Returns (fact, is_the_merchants_own). The flag matters: a title taken from
    the category catalogue must not be described as theirs.
    """
    own = [f for f in (fs.get("offer.active"), fs.get("offer.lapsed")) if f]
    for f in own:
        if "₹" in f.text and not _DISCOUNT_RE.search(f.text):
            return f, True

    catalog = fs.get("catalog.all")
    if catalog and isinstance(catalog.value, list):
        for title in catalog.value:
            if "₹" in str(title) and not _DISCOUNT_RE.search(str(title)):
                return Fact(
                    key="catalog.service_at_price",
                    value=title,
                    text=str(title),
                    label="category offer template",
                    source="category.offer_catalog[]",
                ), False

    if own:
        return own[0], True
    first = fs.get("catalog.first")
    return (first, False) if first else (None, False)


def _ask(
    spec: KindSpec,
    fs: FactSet,
    voice: VoicePack,
    lang: LanguagePlan,
    customer: Optional[dict],
    category_slug: str = "",
) -> tuple[str, list[Fact], str, list[str]]:
    """The single call-to-action. Always the last thing in the body."""
    cited: list[Fact] = []
    fired: list[str] = []
    # A reinterpreted kind needs its offer of work reinterpreted too, otherwise
    # the original vertical's vocabulary sneaks back in via the ask.
    work = (
        reframed_work(spec, category_slug)
        or spec.offer_of_work
        or "take the next step off your plate"
    )

    if ASK_MERCHANT in spec.levers and spec.kind in ("curious_ask_due", "scheduled_recurring"):
        fired.append(ASK_MERCHANT)
        # The question *is* the CTA, so the promise goes first and the question
        # lands last -- brief §11 penalises a buried call-to-action.
        return (
            f"I'll {work}. What's the one service people ask you for most this week?",
            cited, CTA_OPEN_ENDED, fired,
        )

    if spec.cta == CTA_MULTI_CHOICE_SLOT:
        # Booking flows may offer slots -- brief Appendix B treats this as correct
        # for customer-facing recall, and the single-CTA rule targets asks that
        # compete with each other, not two times for the same action.
        slots = fs.get("trg.available_slots") or fs.get("trg.next_session_options")
        fired.append(EFFORT_EXTERNALIZATION)
        labels = _slot_labels(slots)
        if labels:
            cited.append(slots)
            opts = " or ".join(f"{i+1} for {l}" for i, l in enumerate(labels[:2]))
            return f"Reply {opts}, or tell us a time that suits.", cited, CTA_MULTI_CHOICE_SLOT, fired
        pref = fs.get("cust.slots")
        if pref:
            cited.append(pref)
            return (
                f"Reply YES and we'll hold a {pref.text} slot for you.",
                cited, CTA_BINARY_YES_NO, fired,
            )
        return "Reply YES and we'll hold a slot for you.", cited, CTA_BINARY_YES_NO, fired

    if spec.cta == CTA_BINARY_CONFIRM_CANCEL:
        fired.append(EFFORT_EXTERNALIZATION)
        return "Reply CONFIRM to keep it, or CANCEL to drop it.", cited, CTA_BINARY_CONFIRM_CANCEL, fired

    if spec.cta == CTA_BINARY_YES_NO:
        fired.append(EFFORT_EXTERNALIZATION)
        return f"Reply YES and I'll {work}.", cited, CTA_BINARY_YES_NO, fired

    fired.append(EFFORT_EXTERNALIZATION)
    return f"Want me to {work}?", cited, CTA_OPEN_ENDED, fired


def _code_mix_ask(ask: str, cta: str, lang: LanguagePlan) -> str:
    """Recast the ask in Hinglish, keeping every factual token untouched.

    Only the ask is code-mixed. Translating the hook would mean restating the
    numbers and citations, and the moment a fact is restated it can be restated
    wrongly -- so the factual sentence stays in its verified form.
    """
    if not ask:
        return ask

    # The rule that keeps this grammatical: an English verb phrase is never
    # embedded inside a Hindi verb frame. The work stays an intact English
    # clause; the imperative that follows it is Hindi. That is how Hinglish is
    # actually written, and it keeps the work description verifiable.
    replacements = [
        # asks that carry an English work phrase -> English clause + Hindi tail
        (r"^Want me to (.+?)\?$", r"Want me to \1? Bas bata dijiye."),
        (r"^Reply YES and I'll (.+?)\.$", r"I'll \1 — iske liye bas YES bhej dijiye."),
        (r"^Reply YES and we'll (.+?)\.$", r"We'll \1 — iske liye bas YES bhej dijiye."),
        # asks with no embedded work phrase translate cleanly and fully
        (r"^Reply CONFIRM to keep it, or CANCEL to drop it\.$",
         "Rakhna hai to CONFIRM bhejiye, cancel karna hai to CANCEL."),
        (r"^Reply (\d+) for (.+?) or (\d+) for (.+?), or tell us a time that suits\.$",
         r"\2 ke liye \1 bhejiye, \4 ke liye \3 — ya apna time bata dijiye."),
        (r"^Reply (\d+) for (.+?), or tell us a time that suits\.$",
         r"\2 ke liye \1 bhejiye — ya jo time suit kare wo bata dijiye."),
        (r"^Reply YES and we'll hold a (.+?) slot for you\.$",
         r"\1 ka slot hold kar dete hain — YES bhej dijiye."),
        (r"^Reply YES and we'll hold a slot for you\.$",
         "Aapke liye slot hold kar dete hain — YES bhej dijiye."),
    ]
    out = ask
    for pattern, repl in replacements:
        new = re.sub(pattern, repl, out, flags=re.S)
        if new != out:
            out = new
            break

    # The merchant-ask variant: Hindi question, English work clause after it.
    out = re.sub(
        r"^I'll (.+?)\. What's the one service people ask you for most this week\?$",
        r"I'll \1. Is hafte log aapse sabse zyada kaunsi service maang rahe hain?",
        out, flags=re.S,
    )
    return _tidy(out)


def _slot_labels(slots_fact: Optional[Fact]) -> list[str]:
    """Pull human slot labels out of a trigger payload's slot list."""
    if slots_fact is None:
        return []
    raw = slots_fact.value
    if isinstance(raw, str):
        return [raw]
    labels: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict) and item.get("label"):
                labels.append(str(item["label"]))
            elif isinstance(item, str):
                labels.append(item)
    return labels


def _safe_fallback(
    fs: FactSet,
    voice: VoicePack,
    lang: LanguagePlan,
    spec: KindSpec,
    is_customer_facing: bool,
) -> tuple[str, str, list[Fact]]:
    """Minimal construction guaranteed to clear every hard rule."""
    cited: list[Fact] = []
    addressee = fs.get("cust.name") if is_customer_facing else fs.get("owner")
    lead = ""
    if addressee:
        cited.append(addressee)
        lead = f"{voice.salutation(addressee.text, formal_hint=not is_customer_facing)}, "

    anchor = fs.first("perf.views", "perf.calls", "offer.active", "locality")
    if anchor:
        cited.append(anchor)
        middle = f"quick one on your listing — {anchor.text} {anchor.label}."
    else:
        middle = "quick one on your listing."
    return f"{lead}{middle} Want me to take a look with you?", CTA_OPEN_ENDED, cited


# ---------------------------------------------------------------------------
# Rationale -- the judge reads this and cross-checks it against the body
# ---------------------------------------------------------------------------

def _rationale(
    spec: KindSpec,
    fs: FactSet,
    cited: list[Fact],
    lang: LanguagePlan,
    parts: dict,
    trigger: dict,
    category: dict,
    v: Any,
) -> str:
    anchors = [f"{f.text} ({f.source})" for f in cited[:3] if f.key != "owner"]
    bits = [
        f"Trigger {spec.kind} (urgency {trigger.get('urgency', '?')}, "
        f"{trigger.get('source', 'internal')}): {spec.why_now}.",
    ]
    if anchors:
        bits.append("Anchored on " + "; ".join(anchors) + ".")
    if parts["levers_fired"]:
        bits.append("Levers: " + ", ".join(parts["levers_fired"]) + ".")
    bits.append(
        f"Voice per category.voice.tone={(category.get('voice') or {}).get('tone', '?')}; "
        f"language {lang.label} from {lang.source}."
    )
    bits.append(f"Single {parts['cta']} CTA in the final sentence.")
    if v is not None and getattr(v, "warnings", None):
        bits.append("Known tradeoffs: " + "; ".join(v.warnings) + ".")
    return " ".join(bits)


def _template_params(fs: FactSet, parts: dict) -> list[str]:
    """Params for the pre-approved first-outbound template (brief §5.1)."""
    return [
        parts["salutation"] or fs.text_of("owner") or fs.text_of("biz"),
        parts["hook"],
        parts["ask"],
    ]


# ---------------------------------------------------------------------------
# Optional LLM phrasing layer
# ---------------------------------------------------------------------------

_LLM_SYSTEM = """You rewrite one WhatsApp message for an Indian local-business owner.

You will be given a DRAFT and the exact FACTS it is built from.

Rules, all absolute:
1. Do NOT introduce any number, date, price, percentage, name or citation that is
   not already in the FACTS. Copy them exactly as written.
2. Do NOT remove any fact from the draft.
3. Keep exactly one call-to-action, and keep it as the last sentence.
4. No URLs. No links. No domain names.
5. No promotional language. No exclamation marks. Never use: guaranteed, miracle,
   cure, best in city, amazing.
6. No greeting preamble. Never say "I hope you are well" or "I am reaching out".
7. Keep it under 60 words.
8. Match the requested TONE and LANGUAGE.

Reply with the rewritten message only. No preamble, no quotes, no explanation."""


def _llm_rephrase(
    draft: str,
    cited: list[Fact],
    spec: KindSpec,
    voice: VoicePack,
    lang: LanguagePlan,
    fs: FactSet,
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict],
    previous_bodies: Optional[list[str]],
) -> Optional[str]:
    """Rephrase for fluency. Returns None whenever the result isn't strictly safe.

    The model is handed only the facts already in the draft, and its output is
    validated in strict mode -- so a fabricated figure can't survive, it just
    causes the deterministic draft to be kept.
    """
    try:
        from llm import generate
    except ImportError:
        return None

    facts_block = "\n".join(f"- {f.text}   [{f.label}]" for f in cited)
    tone = f"{voice.tone} / {voice.register}".strip(" /")
    prompt = (
        f"TONE: {tone}\n"
        f"LANGUAGE: {lang.label}"
        + (" — use natural Hinglish for the connecting words but keep every "
           "number, price and citation exactly as given in Latin script.\n"
           if lang.use_code_mix else "\n")
        + f"CATEGORY: {category.get('slug', '')}\n"
        f"AVOID THESE WORDS: {', '.join(voice.vocab_taboo[:6])}\n\n"
        f"FACTS (the only facts you may use):\n{facts_block}\n\n"
        f"DRAFT:\n{draft}\n\n"
        "Rewritten message:"
    )

    out = generate(prompt, system=_LLM_SYSTEM)
    if not out:
        return None

    candidate = _tidy(out.strip().strip('"').strip())
    if not candidate or len(candidate) > 700:
        return None

    # Strict gate: only the facts handed over are permissible.
    v = validate(
        candidate, spec.cta, fs, category, merchant, trigger, customer,
        previous_bodies, cited_facts=cited,
    )
    if not v.ok:
        return None

    # Every fact in the draft must survive the rewrite.
    for f in cited:
        if f.key in ("owner", "cust.name"):
            continue
        if f.text and f.text not in candidate:
            return None

    # No-material-loss guard. Measured behaviour of a 7B local model on this task
    # is to compress by deleting the attributed and specific parts -- the peer
    # scope ("for metro solo practices") and the offer of work ("I'll run the
    # diagnostic and tell you which of the three usual causes it is") -- which is
    # precisely what Specificity and Engagement compulsion are scored on. A
    # rewrite that loses substance is rejected in favour of the draft.
    if len(candidate) < 0.85 * len(draft):
        return None

    work = (spec.offer_of_work or "").split()
    if len(work) >= 3:
        head = " ".join(work[:3]).lower()
        if head in draft.lower() and head not in candidate.lower():
            return None

    return candidate
