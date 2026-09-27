"""Fact extraction with provenance, plus the output validator.

This module is the reason a small local model is safe to use here. Two ideas:

1. **Facts carry provenance.** Every citable value is pulled out of the 4 contexts
   with the path it came from, so the composer can only ever slot in something it
   actually read. Fabrication is structurally impossible in the deterministic path.

2. **The validator is a closed-world number check.** Every number appearing in a
   composed body must trace back to a number that appears somewhere in the four
   context objects (or to a tiny set of phrasing numerals). A model that invents
   "a 2,400-patient trial" when the context says 2,100 fails the gate and its
   output is discarded.

challenge-brief.md §11 penalizes hallucination the hardest of any anti-pattern,
and the judge's own rubric docks -2 for "fabricating data not in context".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# Numerals that legitimately appear as phrasing rather than as claims about the
# merchant: "2-min read", "reply 1 or 2", "one thing", a 3-step list.
PHRASING_NUMERALS = {"0", "1", "2", "3", "5", "10", "15", "20", "24", "30", "60", "90"}

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


@dataclass(frozen=True)
class Fact:
    """One verifiable value the message is allowed to cite."""

    key: str            # stable handle, e.g. "perf.views"
    value: Any          # raw value as stored in the context
    text: str           # render-ready, e.g. "2,410"
    label: str          # human phrase, e.g. "views in the last 30 days"
    source: str         # provenance path, e.g. "merchant.performance.views"

    def __str__(self) -> str:  # pragma: no cover - debugging convenience
        return f"{self.text} ({self.label}) <- {self.source}"


@dataclass
class FactSet:
    """Facts available for one composition, plus the closed-world number set."""

    facts: dict[str, Fact] = field(default_factory=dict)
    allowed_numbers: set[str] = field(default_factory=set)

    def add(self, key: str, value: Any, text: str, label: str, source: str) -> Optional[Fact]:
        if value is None or text in ("", "None"):
            return None
        f = Fact(key=key, value=value, text=text, label=label, source=source)
        self.facts[key] = f
        return f

    def get(self, key: str) -> Optional[Fact]:
        return self.facts.get(key)

    def text_of(self, key: str, default: str = "") -> str:
        f = self.facts.get(key)
        return f.text if f else default

    def has(self, *keys: str) -> bool:
        return all(k in self.facts for k in keys)

    def first(self, *keys: str) -> Optional[Fact]:
        """First present fact in priority order -- the anchor-selection primitive."""
        for k in keys:
            if k in self.facts:
                return self.facts[k]
        return None


# ---------------------------------------------------------------------------
# Number harvesting -- the closed world
# ---------------------------------------------------------------------------

def _variants(raw: str, loose: bool = True) -> set[str]:
    """Normalized forms of one numeric token.

    A context ratio of 0.021 is legitimately written "2.1%" in a message, and
    -0.5 as "50%", so ratio->percent forms are part of the closed world.

    ``loose`` additionally admits thousands-abbreviations (2,410 -> "2.4k").
    That is right for the closed-world gate but wrong for the strict gate: it
    would let a trial size of 2,100 legitimise a fabricated CTR of "2.1%".
    """
    out: set[str] = set()
    token = raw.replace(",", "").lstrip("-")
    if not token:
        return out
    out.add(token)
    try:
        val = float(token)
    except ValueError:
        return out

    # integral form: 2.0 -> "2"
    if val == int(val):
        out.add(str(int(val)))

    # ratio -> percent, but ONLY for genuine ratios. Scaling every integer by
    # 100 would let 48 legitimise "4800" and blow a hole in the closed world.
    if 0 < abs(val) <= 1:
        pct = val * 100
        if pct == int(pct):
            out.add(str(int(pct)))
        out.add(f"{pct:.1f}".rstrip("0").rstrip("."))
        out.add(str(round(pct)))
        out.add(str(round(pct, 1)))

    # percent -> ratio (a context percentage cited as a fraction)
    if abs(val) > 1:
        out.add(f"{val / 100:.3f}".rstrip("0").rstrip("."))

    # rounding of the value itself (2410 -> "2.4" for "2.4k", 0.38 -> "38")
    out.add(str(round(val)))
    out.add(f"{val:.1f}".rstrip("0").rstrip("."))
    if loose and val >= 1000:
        out.add(f"{val / 1000:.1f}".rstrip("0").rstrip("."))
        out.add(str(round(val / 1000)))
    return {v for v in out if v}


def harvest_numbers(
    obj: Any, into: Optional[set[str]] = None, loose: bool = True
) -> set[str]:
    """Every number reachable anywhere in a context object, plus derived forms.

    Walks dict keys as well as values, because signals arrive as strings like
    "stale_posts:22d" where 22 is the citable fact.
    """
    acc: set[str] = into if into is not None else set()
    if obj is None:
        return acc
    if isinstance(obj, bool):
        return acc
    if isinstance(obj, (int, float)):
        acc |= _variants(str(obj), loose)
        return acc
    if isinstance(obj, str):
        for m in _NUM_RE.findall(obj):
            acc |= _variants(m, loose)
        return acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            harvest_numbers(k, acc, loose)
            harvest_numbers(v, acc, loose)
        return acc
    if isinstance(obj, (list, tuple, set)):
        for v in obj:
            harvest_numbers(v, acc, loose)
        return acc
    return acc


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _fmt_int(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def _fmt_pct(ratio: Any, digits: int = 1) -> str:
    """Render a ratio as a percentage.

    Trailing zeros are only stripped after a decimal point -- a bare rstrip("0")
    turns 30% into 3%, which silently misstates the merchant's own numbers.
    """
    try:
        s = f"{float(ratio) * 100:.{digits}f}"
    except (TypeError, ValueError):
        return str(ratio)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s + "%"


_SNAKE_RE = re.compile(r"^[a-z0-9][a-z0-9]*(?:_[a-z0-9]+)+$")
_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def humanize(value: Any) -> str:
    """Render a context value as something a merchant can read.

    Trigger payloads carry machine slugs ("corporate_bulk_thali_package") and raw
    ISO timestamps ("2026-04-28T00:00:00+05:30"). Emitting either verbatim reads
    as leaked internals and the judge docks for exposing jargon, so both are
    normalised here -- once, at the point facts are built.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        parts = [humanize(v) for v in value if v is not None]
        parts = [p for p in parts if p]
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    if not isinstance(value, str):
        return str(value)

    text = value.strip()

    m = _ISO_RE.match(text)
    if m:
        y, mo, d, hh, mm = m.groups()
        try:
            out = f"{int(d)} {_MONTHS[int(mo) - 1]}"
        except (ValueError, IndexError):
            return text
        if hh is not None:
            hour, minute = int(hh), int(mm)
            # 00:00 is a date-only value that happens to carry a time component;
            # rendering it as "12am" implies a precision that isn't there.
            if (hour, minute) != (0, 0):
                suffix = "am" if hour < 12 else "pm"
                h12 = hour % 12 or 12
                out += f", {h12}:{minute:02d}{suffix}" if minute else f", {h12}{suffix}"
        return out

    if _SNAKE_RE.match(text):
        return text.replace("_", " ")
    return text


def build_factset(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
) -> FactSet:
    """Extract every citable fact from the 4 contexts, with provenance."""
    fs = FactSet()

    # Closed world spans all four contexts as pushed.
    for obj in (category, merchant, trigger, customer):
        harvest_numbers(obj, fs.allowed_numbers)
    fs.allowed_numbers |= PHRASING_NUMERALS

    identity = merchant.get("identity") or {}
    perf = merchant.get("performance") or {}
    subs = merchant.get("subscription") or {}
    agg = merchant.get("customer_aggregate") or {}
    peer = category.get("peer_stats") or {}
    payload = trigger.get("payload") or {}

    # --- identity ------------------------------------------------------
    fs.add("owner", identity.get("owner_first_name"), str(identity.get("owner_first_name") or ""),
           "owner first name", "merchant.identity.owner_first_name")
    fs.add("biz", identity.get("name"), str(identity.get("name") or ""),
           "business name", "merchant.identity.name")
    fs.add("locality", identity.get("locality"), str(identity.get("locality") or ""),
           "locality", "merchant.identity.locality")
    fs.add("city", identity.get("city"), str(identity.get("city") or ""),
           "city", "merchant.identity.city")
    if identity.get("established_year"):
        fs.add("established", identity["established_year"], str(identity["established_year"]),
               "year established", "merchant.identity.established_year")

    # --- performance ---------------------------------------------------
    window = perf.get("window_days", 30)
    if perf.get("views") is not None:
        fs.add("perf.views", perf["views"], _fmt_int(perf["views"]),
               f"views in the last {window} days", "merchant.performance.views")
    if perf.get("calls") is not None:
        fs.add("perf.calls", perf["calls"], _fmt_int(perf["calls"]),
               f"calls in the last {window} days", "merchant.performance.calls")
    if perf.get("directions") is not None:
        fs.add("perf.directions", perf["directions"], _fmt_int(perf["directions"]),
               f"direction requests in the last {window} days", "merchant.performance.directions")
    if perf.get("leads") is not None:
        fs.add("perf.leads", perf["leads"], _fmt_int(perf["leads"]),
               f"leads in the last {window} days", "merchant.performance.leads")
    if perf.get("ctr") is not None:
        fs.add("perf.ctr", perf["ctr"], _fmt_pct(perf["ctr"]),
               "listing click-through rate", "merchant.performance.ctr")

    delta = perf.get("delta_7d") or {}
    for metric in ("views", "calls", "ctr"):
        val = delta.get(f"{metric}_pct")
        if val is not None:
            fs.add(f"delta.{metric}", val, _fmt_pct(abs(val), 0),
                   f"week-over-week change in {metric}", f"merchant.performance.delta_7d.{metric}_pct")

    # --- peer benchmarks (attributed, per the fabrication-risk decision) --
    if peer.get("avg_ctr") is not None:
        fs.add("peer.ctr", peer["avg_ctr"], _fmt_pct(peer["avg_ctr"]),
               f"peer median CTR ({peer.get('scope','peers')})", "category.peer_stats.avg_ctr")
    if peer.get("avg_views_30d") is not None:
        fs.add("peer.views", peer["avg_views_30d"], _fmt_int(peer["avg_views_30d"]),
               "peer average 30d views", "category.peer_stats.avg_views_30d")
    if peer.get("avg_calls_30d") is not None:
        fs.add("peer.calls", peer["avg_calls_30d"], _fmt_int(peer["avg_calls_30d"]),
               "peer average 30d calls", "category.peer_stats.avg_calls_30d")
    if peer.get("avg_rating") is not None:
        fs.add("peer.rating", peer["avg_rating"], str(peer["avg_rating"]),
               "peer average rating", "category.peer_stats.avg_rating")
    if peer.get("avg_review_count") is not None:
        fs.add("peer.reviews", peer["avg_review_count"], _fmt_int(peer["avg_review_count"]),
               "peer average review count", "category.peer_stats.avg_review_count")
    if peer.get("avg_photos") is not None:
        fs.add("peer.photos", peer["avg_photos"], _fmt_int(peer["avg_photos"]),
               "peer average photo count", "category.peer_stats.avg_photos")
    if peer.get("avg_post_freq_days") is not None:
        fs.add("peer.post_freq", peer["avg_post_freq_days"], _fmt_int(peer["avg_post_freq_days"]),
               "peer posting cadence in days", "category.peer_stats.avg_post_freq_days")

    # --- subscription --------------------------------------------------
    if subs.get("days_remaining"):
        fs.add("subs.days", subs["days_remaining"], str(subs["days_remaining"]),
               "days left on the plan", "merchant.subscription.days_remaining")
    if subs.get("days_since_expiry"):
        fs.add("subs.expired_days", subs["days_since_expiry"], str(subs["days_since_expiry"]),
               "days since the plan expired", "merchant.subscription.days_since_expiry")
    fs.add("subs.plan", subs.get("plan"), str(subs.get("plan") or ""),
           "plan name", "merchant.subscription.plan")
    fs.add("subs.status", subs.get("status"), str(subs.get("status") or ""),
           "subscription status", "merchant.subscription.status")

    # --- customer aggregate --------------------------------------------
    for k, label in (
        ("total_unique_ytd", "unique customers year-to-date"),
        ("lapsed_180d_plus", "customers lapsed over 180 days"),
        ("high_risk_adult_count", "patients flagged high-risk"),
        ("active_members", "active members"),
        ("chronic_rx_count", "chronic-prescription customers"),
    ):
        if agg.get(k) is not None:
            fs.add(f"agg.{k}", agg[k], _fmt_int(agg[k]), label, f"merchant.customer_aggregate.{k}")
    if agg.get("retention_6mo_pct") is not None:
        fs.add("agg.retention", agg["retention_6mo_pct"], _fmt_pct(agg["retention_6mo_pct"], 0),
               "6-month retention", "merchant.customer_aggregate.retention_6mo_pct")

    # --- offers --------------------------------------------------------
    offers = merchant.get("offers") or []
    active = [o for o in offers if o.get("status") == "active" and o.get("title")]
    inactive = [o for o in offers if o.get("status") != "active" and o.get("title")]
    if active:
        fs.add("offer.active", active[0]["title"], active[0]["title"],
               "active offer", "merchant.offers[status=active].title")
        fs.add("offer.active_all", [o["title"] for o in active],
               ", ".join(o["title"] for o in active), "all active offers", "merchant.offers")
    if inactive:
        fs.add("offer.lapsed", inactive[0]["title"], inactive[0]["title"],
               f"{inactive[0].get('status','inactive')} offer", "merchant.offers[status!=active].title")

    # Category catalog is the fallback when the merchant runs no offer of their
    # own -- service@price beats "X% off" (brief §5.5, §11).
    catalog = [o.get("title") for o in (category.get("offer_catalog") or []) if o.get("title")]
    if catalog:
        fs.add("catalog.first", catalog[0], catalog[0],
               "category offer template", "category.offer_catalog[0].title")
        fs.add("catalog.all", catalog, ", ".join(catalog),
               "category offer templates", "category.offer_catalog")

    # --- signals + review themes ---------------------------------------
    signals = merchant.get("signals") or []
    if signals:
        fs.add("signals", signals, ", ".join(signals), "derived signals", "merchant.signals")
    for sig in signals:
        m = re.match(r"stale_posts:(\d+)d", str(sig))
        if m:
            fs.add("signal.stale_days", int(m.group(1)), m.group(1),
                   "days since the last Google post", "merchant.signals[stale_posts]")

    themes = merchant.get("review_themes") or []
    neg = [t for t in themes if t.get("sentiment") == "neg"]
    pos = [t for t in themes if t.get("sentiment") == "pos"]
    if neg:
        t = neg[0]
        fs.add("review.neg_theme", t.get("theme"), str(t.get("theme") or "").replace("_", " "),
               "negative review theme", "merchant.review_themes[neg].theme")
        if t.get("occurrences_30d") is not None:
            fs.add("review.neg_count", t["occurrences_30d"], str(t["occurrences_30d"]),
                   "mentions in the last 30 days", "merchant.review_themes[neg].occurrences_30d")
        if t.get("common_quote"):
            fs.add("review.neg_quote", t["common_quote"], t["common_quote"],
                   "representative review quote", "merchant.review_themes[neg].common_quote")
    if pos:
        fs.add("review.pos_theme", pos[0].get("theme"),
               str(pos[0].get("theme") or "").replace("_", " "),
               "positive review theme", "merchant.review_themes[pos].theme")

    # --- resolved digest item (research/regulation/CDE/supply kinds) -----
    digest_item = resolve_digest_item(category, trigger)
    if digest_item:
        fs.add("digest.title", digest_item.get("title"), str(digest_item.get("title") or ""),
               "digest headline", "category.digest[].title")
        fs.add("digest.source", digest_item.get("source"), str(digest_item.get("source") or ""),
               "digest citation", "category.digest[].source")
        fs.add("digest.summary", digest_item.get("summary"), str(digest_item.get("summary") or ""),
               "digest summary", "category.digest[].summary")
        fs.add("digest.actionable", digest_item.get("actionable"),
               str(digest_item.get("actionable") or ""),
               "digest recommended action", "category.digest[].actionable")
        if digest_item.get("trial_n") is not None:
            fs.add("digest.trial_n", digest_item["trial_n"], _fmt_int(digest_item["trial_n"]),
                   "trial size", "category.digest[].trial_n")
        if digest_item.get("patient_segment"):
            fs.add("digest.segment", digest_item["patient_segment"],
                   str(digest_item["patient_segment"]).replace("_", " "),
                   "relevant patient segment", "category.digest[].patient_segment")
        if digest_item.get("credits") is not None:
            fs.add("digest.credits", digest_item["credits"], str(digest_item["credits"]),
                   "CDE credits", "category.digest[].credits")

    # --- seasonal beats + trend signals --------------------------------
    beats = category.get("seasonal_beats") or []
    if beats:
        fs.add("beat.note", beats[0].get("note"), str(beats[0].get("note") or ""),
               "seasonal note", "category.seasonal_beats[0].note")
        fs.add("beat.months", beats[0].get("month_range"), str(beats[0].get("month_range") or ""),
               "seasonal window", "category.seasonal_beats[0].month_range")
    trends = category.get("trend_signals") or []
    if trends:
        t0 = trends[0]
        fs.add("trend.query", t0.get("query"), str(t0.get("query") or ""),
               "trending search", "category.trend_signals[0].query")
        if t0.get("delta_yoy") is not None:
            fs.add("trend.delta", t0["delta_yoy"], _fmt_pct(t0["delta_yoy"], 0),
                   "year-on-year search growth", "category.trend_signals[0].delta_yoy")

    # --- trigger payload (flattened, so kind handlers can slot directly) --
    for k, v in payload.items():
        if k in ("placeholder", "metric_or_topic"):
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (str, int, float)):
            if isinstance(v, float) and abs(v) <= 1 and k.endswith("_pct"):
                text = _fmt_pct(abs(v), 0)
            elif isinstance(v, (int, float)):
                text = _fmt_int(v) if abs(v) >= 1000 else str(v)
            else:
                text = humanize(v)
            fs.add(f"trg.{k}", v, text, k.replace("_", " "), f"trigger.payload.{k}")
        elif isinstance(v, (list, tuple)) and v:
            # Slot lists stay structured (the CTA builder reads their labels);
            # flat lists of strings render as prose.
            if all(isinstance(x, dict) for x in v):
                fs.add(f"trg.{k}", v, "", k.replace("_", " "), f"trigger.payload.{k}")
            else:
                fs.add(f"trg.{k}", v, humanize(v), k.replace("_", " "),
                       f"trigger.payload.{k}")

    # --- customer ------------------------------------------------------
    if customer:
        cid = customer.get("identity") or {}
        rel = customer.get("relationship") or {}
        fs.add("cust.name", cid.get("name"), str(cid.get("name") or ""),
               "customer name", "customer.identity.name")
        fs.add("cust.lang", cid.get("language_pref"), str(cid.get("language_pref") or ""),
               "customer language preference", "customer.identity.language_pref")
        fs.add("cust.state", customer.get("state"), str(customer.get("state") or "").replace("_", " "),
               "relationship state", "customer.state")
        if rel.get("visits_total") is not None:
            fs.add("cust.visits", rel["visits_total"], str(rel["visits_total"]),
                   "visits so far", "customer.relationship.visits_total")
        fs.add("cust.last_visit", rel.get("last_visit"), humanize(rel.get("last_visit")),
               "last visit date", "customer.relationship.last_visit")
        services = rel.get("services_received") or []
        if services:
            fs.add("cust.last_service", services[-1], str(services[-1]).replace("_", " "),
                   "most recent service", "customer.relationship.services_received[-1]")
        prefs = customer.get("preferences") or {}
        fs.add("cust.slots", prefs.get("preferred_slots"),
               str(prefs.get("preferred_slots") or "").replace("_", " "),
               "preferred slot window", "customer.preferences.preferred_slots")

    return fs


def resolve_digest_item(category: dict, trigger: dict) -> Optional[dict]:
    """Resolve a trigger's digest reference against category.digest.

    Trigger payloads point at digest items by id under three different keys
    (top_item_id / digest_item_id / alert_id). When none is present the most
    relevant item is chosen by kind, so the digest is still usable.
    """
    digest = category.get("digest") or []
    if not digest:
        return None
    payload = trigger.get("payload") or {}

    for key in ("top_item_id", "digest_item_id", "alert_id", "item_id"):
        ref = payload.get(key)
        if ref:
            for item in digest:
                if item.get("id") == ref:
                    return item

    # No explicit reference: prefer a digest entry whose kind matches the trigger.
    kind = trigger.get("kind", "")
    preferred = {
        "research_digest": ("research",),
        "regulation_change": ("regulation", "compliance"),
        "cde_opportunity": ("event", "education", "webinar"),
        "supply_alert": ("alert", "recall", "safety"),
        "category_seasonal": ("seasonal",),
        "category_trend_movement": ("trend",),
    }.get(kind)
    if preferred:
        for item in digest:
            if str(item.get("kind", "")).lower() in preferred:
                return item
    return None


# ---------------------------------------------------------------------------
# Validation -- the rubric gate
# ---------------------------------------------------------------------------

@dataclass
class Validation:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.ok = False

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)


_URL_RE = re.compile(r"https?://|www\.|\b[a-z0-9-]+\.(?:com|in|org|net|co)\b", re.I)

# Overclaim words that are disqualifying in every category. The per-category
# taboo lists are phrase-shaped ("guaranteed weight loss", "miracle cure"), so
# these catch the head word used on its own.
_UNIVERSAL_TABOO_WORDS = (
    "guaranteed", "guarantee", "miracle", "100% safe", "completely cure",
    "permanent results", "instant transformation", "risk-free", "no risk",
    "best in city", "best in town", "amazing deal", "unbeatable",
)
_CTA_MARKERS = ("reply yes", "reply 1", "reply stop", "want me to", "should i",
                "shall i", "chahiye", "bhej", "confirm", "batayein", "bataye",
                "bata dijiye", "bhejiye", "dijiye")

# All-caps tokens the message is allowed to contain: the binary CTA vocabulary.
_CTA_KEYWORDS = frozenset({"YES", "STOP", "NO", "CONFIRM", "CANCEL", "OK"})

_CAPS_RE = re.compile(r"\b[A-Z]{2,}\b")


def harvest_caps(obj: Any, into: Optional[set[str]] = None) -> set[str]:
    """All-caps tokens appearing anywhere in a context object.

    Acronyms the contexts use -- JIDA, DCI, IDA, ORS, IPL, MI -- are legitimate
    in a composed message; only caps the bot introduced itself are shouting.
    """
    acc: set[str] = into if into is not None else set()
    if obj is None:
        return acc
    if isinstance(obj, str):
        acc.update(_CAPS_RE.findall(obj))
        return acc
    if isinstance(obj, dict):
        for k, val in obj.items():
            harvest_caps(k, acc)
            harvest_caps(val, acc)
        return acc
    if isinstance(obj, (list, tuple, set)):
        for val in obj:
            harvest_caps(val, acc)
        return acc
    return acc


def validate(
    body: str,
    cta: str,
    factset: FactSet,
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
    previous_bodies: Optional[Iterable[str]] = None,
    cited_facts: Optional[Iterable[Fact]] = None,
) -> Validation:
    """Gate a composed body against the hard constraints in the briefs.

    Errors are disqualifying (the output must be rebuilt or discarded);
    warnings are rubric risks worth logging but not fatal.

    Two strictness levels for the fabrication gate:

    * ``cited_facts=None`` -- closed-world: every number must appear somewhere
      in the four pushed contexts. Used to sanity-check deterministic output.
    * ``cited_facts=[...]`` -- strict: every number must appear in the specific
      facts handed to the generator. Used to gate LLM output, because a number
      that merely occurs *somewhere* in context can still be a fabricated claim
      ("11 dentists moved to 3-month recall" where 11 came from an unrelated
      field). Narrowing the permitted set to what the generator was actually
      given closes that hole.
    """
    v = Validation(ok=True)
    text = (body or "").strip()

    if not text:
        v.add_error("empty body (treated as malformed, -2)")
        return v

    # 1. URLs -- api-call-examples.md F.4, -3 per occurrence, hard fail.
    if _URL_RE.search(text):
        v.add_error(f"contains a URL or domain: {_URL_RE.search(text).group(0)!r}")

    # 2. Fabrication gate.
    if cited_facts is None:
        permitted, scope_desc = factset.allowed_numbers, "any pushed context"
    else:
        permitted = set(PHRASING_NUMERALS)
        for f in cited_facts:
            harvest_numbers(f.text, permitted, loose=False)
            harvest_numbers(f.value, permitted, loose=False)
        scope_desc = "the facts supplied to the generator"

    for token in _NUM_RE.findall(text):
        norm = token.replace(",", "")
        cands = {norm, norm.rstrip("."), str(norm).lstrip("0") or "0"}
        try:
            fval = float(norm)
            if fval == int(fval):
                cands.add(str(int(fval)))
        except ValueError:
            pass
        if not (cands & permitted):
            v.add_error(f"unverifiable number {token!r} -- not present in {scope_desc}")

    # 3. Category taboos -- brief §11 promotional-tone penalty.
    voice = category.get("voice") or {}
    for taboo in (voice.get("vocab_taboo") or []):
        if re.search(rf"\b{re.escape(str(taboo))}\b", text, re.I):
            v.add_error(f"category taboo phrase used: {taboo!r}")

    # Category taboos are mostly multi-word ("guaranteed weight loss"), so the
    # distinctive head words are checked on their own too -- "guaranteed" alone
    # is just as disqualifying as the full phrase.
    for word in _UNIVERSAL_TABOO_WORDS:
        if re.search(rf"\b{word}\b", text, re.I):
            v.add_error(f"promotional/overclaim word used: {word!r}")

    # Promotional register in a clinical vertical -- brief §11. All-caps is only
    # shouting when the token isn't a CTA keyword or an acronym that appears in
    # the pushed contexts: JIDA, DCI, ORS and CONFIRM/CANCEL are all legitimate.
    if any(t in str(voice.get("tone", "")) for t in ("clinical", "precise")):
        legit_caps = _CTA_KEYWORDS | harvest_caps(category) | harvest_caps(merchant) \
            | harvest_caps(trigger) | harvest_caps(customer)
        shouted = [
            w for w in re.findall(r"\b[A-Z]{4,}\b", text) if w not in legit_caps
        ]
        if text.count("!") > 1 or shouted:
            detail = f" ({', '.join(shouted)})" if shouted else ""
            v.add_error(
                f"promotional register (shouting/exclamations) in a clinical category{detail}"
            )

    # 4. Single primary CTA -- brief §5.3, §11 "multiple CTAs".
    question_marks = text.count("?")
    if question_marks > 1:
        v.add_warning(f"{question_marks} question marks -- risks reading as multiple CTAs")
    if cta == "none" and question_marks:
        v.add_warning("cta declared 'none' but body asks a question")

    # 5. CTA must land last -- brief §11 "buried call-to-action". A trailing
    # source citation ("— JIDA Oct 2026 p.14") may follow the CTA; the brief's
    # own gold example is shaped that way, so measure the residual after the
    # last CTA signal rather than looking only at the final sentence.
    if cta != "none":
        # Find the sentence carrying the ask, then measure what follows *it* --
        # measuring from the marker itself would flag the remainder of the ask's
        # own sentence as buried prose.
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        ask_idx = -1
        for i, s in enumerate(sentences):
            low = s.lower()
            if "?" in s or any(mk in low for mk in _CTA_MARKERS):
                ask_idx = i
        if ask_idx < 0:
            v.add_warning(f"cta declared {cta!r} but no call-to-action found in the body")
        else:
            residual = " ".join(sentences[ask_idx + 1:]).strip(" .—–-")
            # A trailing source citation may follow the ask; prose may not.
            looks_like_citation = bool(re.match(r"^[A-Z][^?]{0,60}$", residual)) and (
                any(ch.isdigit() for ch in residual) or "," in residual
            )
            if len(residual) > 45 and not looks_like_citation:
                v.add_warning(
                    f"call-to-action is buried -- {len(residual)} chars follow the ask"
                )

    # 6. Generic-offer anti-pattern -- brief §11. Quoting an offer the merchant
    # actually runs is not the anti-pattern; inventing discount framing is. So a
    # discount phrase is only an error when it isn't lifted from a real offer
    # title in the merchant's catalogue or the category's template list.
    if re.search(r"\b\d+\s*%\s*(off|discount)\b", text, re.I) or re.search(
        r"\bflat\s+\d+", text, re.I
    ):
        real_titles = [
            str(o.get("title", "")) for o in (merchant.get("offers") or [])
        ] + [str(o.get("title", "")) for o in (category.get("offer_catalog") or [])]
        if not any(t and t in text for t in real_titles):
            v.add_error("generic discount framing used instead of service@price")
        else:
            v.add_warning(
                "quotes a discount-style offer; service@price framing engages "
                "Indian merchants better (brief §3.3)"
            )

    # 7. Long preamble -- brief §11. Scanned over the opening window rather than
    # anchored, because "Hi Dr. Meera, I hope..." contains sentence punctuation.
    if re.search(
        r"(hope (you|this|all|things)|i hope|trust you|trust this|reaching out|wanted to reach|"
        r"just checking in|greetings|hope aap)", text[:140], re.I
    ):
        v.add_error("long preamble / 'hope you are well' opener")

    # 8. Re-introduction after the first message -- brief §11.
    history = merchant.get("conversation_history") or []
    if history and re.search(r"\b(vera se bol|this is vera|i am vera|i'm vera|main vera)\b", text, re.I):
        v.add_error("re-introduces Vera despite existing conversation history")

    # 9. Addressee present -- case-studies.md cross-pattern #3.
    addressee = factset.text_of("cust.name") if customer else factset.text_of("owner")
    if addressee and addressee.split()[0].lower() not in text.lower():
        v.add_warning(f"addressee {addressee!r} not used (costs 1 point on merchant fit)")

    # 10. Language match -- brief §11, FAQ "what language do replies have to be in".
    # A customer's language_pref is an explicit instruction and is honoured
    # strictly. A merchant listing ["en","hi"] merely speaks both, and the
    # brief's own gold example answers such a merchant in English, so Hindi is
    # only expected when the merchant does not list English at all.
    if customer:
        pref = str((customer.get("identity") or {}).get("language_pref") or "en")
        needs_hindi = "hi" in pref
    else:
        langs = [str(x) for x in ((merchant.get("identity") or {}).get("languages") or [])]
        pref = "/".join(langs) or "en"
        needs_hindi = "hi" in langs and "en" not in langs
    if needs_hindi and not _has_devanagari_or_roman_hindi(text):
        v.add_warning(f"language preference {pref!r} but body reads as pure English")

    # 11. Anti-repetition -- failure table F.5, -2 per repeat.
    for prev in (previous_bodies or []):
        if prev.strip() == text:
            v.add_error("verbatim repeat of a message already sent in this conversation")

    # 12. Internal jargon leaking to the merchant -- judge docks -1.
    for jargon in ("suppression_key", "merchant_id", "customer_id", "trigger_id",
                   "lapsed_soft", "lapsed_hard", "ctr_below_peer_median", "stale_posts",
                   "factset", "payload", "context_id"):
        if jargon in text:
            v.add_error(f"internal jargon exposed to the merchant: {jargon!r}")

    return v


_ROMAN_HINDI = (
    "main", "hoon", "deti", "dijiye", "bhejiye", "bhej", "iske", "bas", "wo",
    "hafte", "kaunsi", "sabse", "zyada", "maang", "rahe", "rakhna", "karna",
    "hum", "dete", "suit", "jo", "apna", "slot",
    "aap", "aapka", "aapki", "aapke", "hai", "hain", "kar", "karein", "kya", "ke liye",
    "mein", "se", "ko", "nahi", "abhi", "bhi", "chahiye", "bhej", "dekh", "wala", "wali",
    "raha", "rahi", "diya", "kiya", "lekin", "toh", "ya", "aur", "par", "sakte", "sakti",
    "batayein", "bataye", "milega", "hoga", "chalega", "zyada", "kam", "din", "baat",
)


def _has_devanagari_or_roman_hindi(text: str) -> bool:
    if re.search(r"[ऀ-ॿ]", text):
        return True
    low = f" {text.lower()} "
    return sum(1 for w in _ROMAN_HINDI if f" {w} " in low or f" {w}," in low) >= 2
