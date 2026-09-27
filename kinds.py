"""Per-trigger-kind composition specs.

Covers every one of the 26 kinds present in the generated dataset, not just the
18 that appear in the canonical 30 test pairs -- the judge pushes 15 new triggers
during the test window (testing brief §4 Phase 3), so unseen kinds will arrive.

Each spec declares:

    why_now        how to say "why this message, right now" (brief §8 Trigger relevance)
    anchors        fact keys in priority order; the first present one becomes the
                   specificity hook (brief §8 Specificity)
    levers         which compulsion levers to fire (brief §10)
    cta            CTA shape (brief §5.3)
    scope          "merchant" or "customer" -- drives send_as
    urgency_floor  minimum urgency at which this is worth sending at all
    reframe        for kinds that are meaningless in some categories, the concept
                   to substitute (the category-reinterpretation decision: e.g.
                   chronic_refill_due on a dentist is a recurring-treatment
                   follow-up, never "refill")

Production Vera's two weakest lever families are social proof and asking the
merchant (brief §10), so those are deliberately over-represented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Compulsion levers, brief §10
SPECIFICITY = "specificity"
LOSS_AVERSION = "loss_aversion"
SOCIAL_PROOF = "social_proof"
EFFORT_EXTERNALIZATION = "effort_externalization"
CURIOSITY = "curiosity"
RECIPROCITY = "reciprocity"
ASK_MERCHANT = "ask_merchant"
BINARY_COMMITMENT = "binary_commitment"


@dataclass
class KindSpec:
    kind: str
    why_now: str
    anchors: tuple[str, ...]
    levers: tuple[str, ...]
    cta: str = "open_ended"
    scope: str = "merchant"
    urgency_floor: int = 1
    reframe: dict[str, str] = field(default_factory=dict)
    # Short imperative describing the offer of work Vera makes.
    offer_of_work: str = ""


# ---------------------------------------------------------------------------
# External, knowledge-driven kinds -- the "curiosity/knowledge" portfolio the
# brief §3.4 says production Vera lacks.
# ---------------------------------------------------------------------------

SPECS: dict[str, KindSpec] = {
    "research_digest": KindSpec(
        kind="research_digest",
        why_now="a new item landed in this week's category digest",
        anchors=("digest.trial_n", "digest.source", "digest.title"),
        levers=(SPECIFICITY, RECIPROCITY, CURIOSITY, EFFORT_EXTERNALIZATION, SOCIAL_PROOF),
        cta="open_ended",
        offer_of_work="pull the abstract and draft a patient-facing note you can reshare",
    ),
    "regulation_change": KindSpec(
        kind="regulation_change",
        why_now="a regulator published a change with a compliance deadline",
        anchors=("trg.deadline_iso", "digest.source", "digest.title"),
        levers=(SPECIFICITY, LOSS_AVERSION, EFFORT_EXTERNALIZATION),
        cta="binary_yes_no",
        urgency_floor=3,
        offer_of_work="send the one-page summary of what changes for your practice",
    ),
    "cde_opportunity": KindSpec(
        kind="cde_opportunity",
        why_now="a continuing-education session with credits is open",
        anchors=("digest.credits", "digest.source", "digest.title"),
        levers=(SPECIFICITY, RECIPROCITY, CURIOSITY),
        cta="binary_yes_no",
        offer_of_work="hold a seat and send you the joining details",
    ),
    "supply_alert": KindSpec(
        kind="supply_alert",
        why_now="a batch-level supply alert affects stock you may be holding",
        anchors=("trg.molecule", "trg.affected_batches", "digest.source"),
        levers=(SPECIFICITY, LOSS_AVERSION, EFFORT_EXTERNALIZATION),
        cta="binary_yes_no",
        urgency_floor=4,
        offer_of_work="check the affected batch numbers against your shelf",
    ),
    "category_trend_movement": KindSpec(
        kind="category_trend_movement",
        why_now="search demand in your category moved sharply",
        anchors=("trend.delta", "trend.query"),
        levers=(SPECIFICITY, CURIOSITY, LOSS_AVERSION, SOCIAL_PROOF, ASK_MERCHANT),
        cta="open_ended",
        offer_of_work="draft a listing update that captures that search demand",
    ),
    "category_seasonal": KindSpec(
        kind="category_seasonal",
        why_now="the seasonal demand window for your category is turning",
        anchors=("trg.season", "beat.note", "beat.months"),
        levers=(SPECIFICITY, SOCIAL_PROOF, EFFORT_EXTERNALIZATION),
        cta="binary_yes_no",
        offer_of_work="reorder your listing around what is actually moving this season",
    ),
    "festival_upcoming": KindSpec(
        kind="festival_upcoming",
        why_now="a festival date is close enough to plan for",
        anchors=("trg.days_until", "trg.date", "trg.festival"),
        levers=(SPECIFICITY, LOSS_AVERSION, EFFORT_EXTERNALIZATION, SOCIAL_PROOF, ASK_MERCHANT),
        cta="binary_yes_no",
        offer_of_work="draft the festival offer and schedule it so it is live in time",
    ),
    "local_news_event": KindSpec(
        kind="local_news_event",
        why_now="something happening locally changes footfall today",
        anchors=("trg.event", "locality"),
        levers=(SPECIFICITY, LOSS_AVERSION),
        cta="binary_yes_no",
        offer_of_work="push a same-day update to your listing",
    ),
    "weather_heatwave": KindSpec(
        kind="weather_heatwave",
        why_now="today's weather shifts what customers are searching for",
        anchors=("trg.temp_c", "city"),
        levers=(SPECIFICITY, EFFORT_EXTERNALIZATION),
        cta="binary_yes_no",
        offer_of_work="put up a weather-appropriate offer for today",
    ),
    "ipl_match_today": KindSpec(
        kind="ipl_match_today",
        why_now="a match today changes tonight's demand pattern",
        anchors=("trg.match", "trg.match_time_iso", "trg.venue"),
        levers=(SPECIFICITY, SOCIAL_PROOF, EFFORT_EXTERNALIZATION),
        cta="binary_yes_no",
        offer_of_work="set up a match-night combo for the right nights",
    ),
    "competitor_opened": KindSpec(
        kind="competitor_opened",
        why_now="a new competitor listing appeared near you",
        anchors=("trg.distance_km", "trg.their_offer", "trg.competitor_name"),
        levers=(SPECIFICITY, LOSS_AVERSION, EFFORT_EXTERNALIZATION, SOCIAL_PROOF),
        cta="open_ended",
        offer_of_work="show how your listing compares side by side",
    ),

    # -----------------------------------------------------------------
    # Internal, performance-driven kinds
    # -----------------------------------------------------------------
    "perf_dip": KindSpec(
        kind="perf_dip",
        why_now="a metric dropped week-over-week",
        anchors=("trg.delta_pct", "delta.calls", "delta.views", "perf.calls"),
        levers=(SPECIFICITY, LOSS_AVERSION, EFFORT_EXTERNALIZATION, SOCIAL_PROOF),
        cta="binary_yes_no",
        urgency_floor=2,
        offer_of_work="run the diagnostic and tell you which of the three usual causes it is",
    ),
    "seasonal_perf_dip": KindSpec(
        kind="seasonal_perf_dip",
        why_now="a metric dipped, but the seasonal pattern explains it",
        anchors=("trg.delta_pct", "beat.note", "delta.views"),
        levers=(SPECIFICITY, RECIPROCITY, ASK_MERCHANT),
        cta="open_ended",
        offer_of_work="line your numbers up against the seasonal baseline so you can see it is expected",
    ),
    "perf_spike": KindSpec(
        kind="perf_spike",
        why_now="a metric jumped and the cause is worth locking in",
        anchors=("trg.delta_pct", "trg.likely_driver", "delta.calls", "perf.calls"),
        levers=(SPECIFICITY, CURIOSITY, ASK_MERCHANT, SOCIAL_PROOF),
        cta="open_ended",
        offer_of_work="repeat whatever drove it",
    ),
    "milestone_reached": KindSpec(
        kind="milestone_reached",
        why_now="you are about to cross a visible milestone",
        anchors=("trg.value_now", "trg.milestone_value", "trg.metric"),
        levers=(SPECIFICITY, SOCIAL_PROOF, EFFORT_EXTERNALIZATION, ASK_MERCHANT),
        cta="binary_yes_no",
        offer_of_work="draft the post that gets you over the line",
    ),
    "review_theme_emerged": KindSpec(
        kind="review_theme_emerged",
        why_now="the same complaint is recurring in recent reviews",
        anchors=("trg.occurrences_30d", "review.neg_count", "trg.theme", "review.neg_theme"),
        levers=(SPECIFICITY, LOSS_AVERSION, ASK_MERCHANT, SOCIAL_PROOF),
        cta="open_ended",
        offer_of_work="draft replies to the ones still unanswered",
    ),
    "gbp_unverified": KindSpec(
        kind="gbp_unverified",
        why_now="your Google listing is still unverified, which caps everything else",
        anchors=("trg.estimated_uplift_pct", "perf.views", "trg.verification_path"),
        levers=(SPECIFICITY, LOSS_AVERSION, EFFORT_EXTERNALIZATION, SOCIAL_PROOF),
        cta="binary_yes_no",
        urgency_floor=2,
        offer_of_work="start the verification and walk you through the one step that needs you",
    ),
    "renewal_due": KindSpec(
        kind="renewal_due",
        why_now="your plan lapses shortly",
        anchors=("trg.days_remaining", "subs.days", "perf.views"),
        levers=(SPECIFICITY, LOSS_AVERSION, BINARY_COMMITMENT, SOCIAL_PROOF),
        cta="binary_confirm_cancel",
        urgency_floor=3,
        offer_of_work="keep everything running without a gap",
    ),
    "winback_eligible": KindSpec(
        kind="winback_eligible",
        why_now="your listing has been off-plan for a while and the gap is now measurable",
        anchors=("trg.lapsed_customers_added_since_expiry", "trg.days_since_expiry",
                 "trg.perf_dip_pct", "subs.expired_days"),
        levers=(SPECIFICITY, LOSS_AVERSION, CURIOSITY, SOCIAL_PROOF),
        cta="binary_yes_no",
        offer_of_work="show you exactly what accumulated while it was off",
    ),
    "dormant_with_vera": KindSpec(
        kind="dormant_with_vera",
        why_now="we have not spoken in a while and something changed since",
        anchors=("trg.days_since_last_merchant_message", "perf.views", "delta.views"),
        levers=(RECIPROCITY, CURIOSITY, ASK_MERCHANT),
        cta="open_ended",
        offer_of_work="pick this back up wherever is useful to you",
    ),
    "curious_ask_due": KindSpec(
        kind="curious_ask_due",
        why_now="a standing weekly question to you, not a nudge about your account",
        anchors=("perf.views", "offer.active", "catalog.first"),
        # This kind exists purely to fire the lever production Vera never fires.
        levers=(ASK_MERCHANT, RECIPROCITY, CURIOSITY, SPECIFICITY),
        cta="open_ended",
        offer_of_work="put whatever you say in front of the people already searching for it",
    ),
    "active_planning_intent": KindSpec(
        kind="active_planning_intent",
        why_now="you asked for this and I have it ready",
        anchors=("trg.intent_topic", "offer.active", "catalog.first", "perf.views"),
        # The merchant already said yes -- never re-qualify (brief §9 Pattern D).
        levers=(EFFORT_EXTERNALIZATION, SPECIFICITY, BINARY_COMMITMENT, ASK_MERCHANT, RECIPROCITY),
        cta="binary_yes_no",
        urgency_floor=1,
        offer_of_work="send it across for your approval",
    ),
    "scheduled_recurring": KindSpec(
        kind="scheduled_recurring",
        why_now="your weekly check-in",
        anchors=("perf.views", "delta.views", "offer.active"),
        levers=(ASK_MERCHANT, RECIPROCITY),
        cta="open_ended",
        offer_of_work="take the one thing worth doing this week off your plate",
    ),

    # -----------------------------------------------------------------
    # Customer-scoped kinds -- sent as the merchant, not as Vera
    # -----------------------------------------------------------------
    "recall_due": KindSpec(
        kind="recall_due",
        why_now="your recall window is open",
        anchors=("trg.due_date", "trg.service_due", "cust.last_visit", "cust.last_service"),
        levers=(SPECIFICITY, EFFORT_EXTERNALIZATION),
        cta="multi_choice_slot",
        scope="customer",
        reframe={
            # recall is a clinical concept; a gym or restaurant has no recall
            "gyms": "a check-in on the plan you were last on",
            "restaurants": "a note that it has been a while since your last visit",
            "salons": "your usual appointment interval coming due",
        },
        offer_of_work="hold a slot for you",
    ),
    "appointment_tomorrow": KindSpec(
        kind="appointment_tomorrow",
        why_now="you have a booking tomorrow",
        anchors=("trg.slot_label", "trg.appointment_iso", "cust.last_service"),
        levers=(SPECIFICITY, EFFORT_EXTERNALIZATION),
        cta="binary_confirm_cancel",
        scope="customer",
        offer_of_work="confirm or move it, whichever suits",
    ),
    "customer_lapsed_soft": KindSpec(
        kind="customer_lapsed_soft",
        why_now="it has been a while since your last visit",
        anchors=("trg.days_since_last_visit", "cust.last_visit", "cust.visits",
                 "cust.last_service"),
        levers=(SPECIFICITY, RECIPROCITY),
        cta="multi_choice_slot",
        scope="customer",
        offer_of_work="keep a slot open this week",
    ),
    "customer_lapsed_hard": KindSpec(
        kind="customer_lapsed_hard",
        why_now="you have been away a good while and things have changed since",
        anchors=("trg.days_since_last_visit", "trg.previous_focus",
                 "trg.previous_membership_months", "cust.last_visit"),
        levers=(SPECIFICITY, RECIPROCITY, CURIOSITY),
        cta="binary_yes_no",
        scope="customer",
        offer_of_work="restart from where you left off rather than from scratch",
    ),
    "chronic_refill_due": KindSpec(
        kind="chronic_refill_due",
        why_now="your regular course is about to run out",
        anchors=("trg.stock_runs_out_iso", "trg.molecule_list", "trg.last_refill"),
        levers=(SPECIFICITY, LOSS_AVERSION, EFFORT_EXTERNALIZATION),
        cta="binary_yes_no",
        scope="customer",
        reframe={
            # "refill" is a pharmacy word. Never say it to a dentist's patient.
            "dentists": "your recurring treatment follow-up",
            "gyms": "your membership renewal",
            "salons": "your regular touch-up interval",
            "restaurants": "your usual standing order",
        },
        offer_of_work="have it ready so you do not run out",
    ),
    "trial_followup": KindSpec(
        kind="trial_followup",
        why_now="you came in for a trial and the next step is open",
        anchors=("trg.trial_date", "trg.next_session_options", "cust.last_service"),
        levers=(SPECIFICITY, EFFORT_EXTERNALIZATION),
        cta="multi_choice_slot",
        scope="customer",
        offer_of_work="book the next one at a time that works",
    ),
    "wedding_package_followup": KindSpec(
        kind="wedding_package_followup",
        why_now="your date is close enough that the prep window matters",
        anchors=("trg.days_to_wedding", "trg.wedding_date", "trg.trial_completed"),
        levers=(SPECIFICITY, LOSS_AVERSION, EFFORT_EXTERNALIZATION),
        cta="binary_yes_no",
        scope="customer",
        offer_of_work="map the schedule backwards from your date",
    ),
    "customer_lapsed": KindSpec(  # alias seen in the brief's trigger list
        kind="customer_lapsed",
        why_now="it has been a while since your last visit",
        anchors=("trg.days_since_last_visit", "cust.last_visit", "cust.last_service"),
        levers=(SPECIFICITY, RECIPROCITY),
        cta="multi_choice_slot",
        scope="customer",
        offer_of_work="keep a slot open this week",
    ),
}


# Fallback for a kind the judge invents that we have never seen. Composes from
# merchant state rather than guessing at the trigger's semantics -- degrades
# Trigger relevance gracefully instead of hallucinating a reason.
FALLBACK = KindSpec(
    kind="unknown",
    why_now="something in your account changed that is worth a look",
    anchors=("delta.views", "delta.calls", "perf.views", "perf.calls", "offer.active"),
    levers=(SPECIFICITY, ASK_MERCHANT, EFFORT_EXTERNALIZATION),
    cta="open_ended",
    offer_of_work="take the useful next step off your plate",
)


def spec_for(kind: str) -> KindSpec:
    return SPECS.get(kind, FALLBACK)


def is_known(kind: str) -> bool:
    return kind in SPECS


def reframed_concept(spec: KindSpec, category_slug: str) -> Optional[str]:
    """The category-appropriate substitute when a kind does not fit the vertical.

    Covers the generator's random merchant assignment -- e.g. T08 pairs
    chronic_refill_due (a pharmacy concept) with a dentist, and T29 pairs
    recall_due with a gym.
    """
    return spec.reframe.get(category_slug)


# ---------------------------------------------------------------------------
# Hook templates -- the "why now" opening that carries the specificity anchor.
#
# Mini-language:
#   {fact.key}        substituted with that fact's rendered text
#   [[ ... ]]         optional clause, dropped entirely if any {fact} inside is
#                     missing from the FactSet
#
# The optional-clause form is what lets one template serve both a rich seed
# merchant and a generated merchant with no signals, offers or history -- 14 of
# the 30 canonical test pairs are the sparse case.
# ---------------------------------------------------------------------------

HOOKS: dict[str, str] = {
    # --- knowledge / external -------------------------------------------
    "research_digest":
        "{digest.source} just landed[[ — a {digest.trial_n}-patient trial]]. "
        "{digest.title}.",
    "regulation_change":
        "{digest.title}[[ — compliance deadline {trg.deadline_iso}]].",
    "cde_opportunity":
        "There's a {digest.credits}-credit session open — {digest.title} "
        "[[({digest.source})]].",
    "supply_alert":
        "Batch alert on {trg.molecule}[[ — affected batches {trg.affected_batches}]]. "
        "[[Source: {digest.source}.]]",
    "category_trend_movement":
        "Searches for \"{trend.query}\" are up {trend.delta} year on year"
        "[[ in {city}]].",
    "category_seasonal":
        "The {trg.season} window is turning[[: {beat.note}]].",
    "festival_upcoming":
        "{trg.festival} is {trg.days_until} days out[[ ({trg.date})]].",
    "local_news_event":
        "{trg.event}[[ in {locality}]] today.",
    "weather_heatwave":
        "It's {trg.temp_c}°C in {city} today.",
    "ipl_match_today":
        "{trg.match} tonight[[ at {trg.venue}]][[, {trg.match_time_iso}]].",
    "competitor_opened":
        "A new listing opened {trg.distance_km}km from you"
        "[[ — {trg.competitor_name}, running {trg.their_offer}]].",

    # --- performance / internal -----------------------------------------
    "perf_dip":
        "Your {trg.metric} are down {trg.delta_pct} week-on-week"
        "[[ — {perf.calls} calls off {perf.views} views]].",
    "seasonal_perf_dip":
        "Your {trg.metric} dipped {trg.delta_pct} this week — that's the expected "
        "seasonal pattern[[, not a problem with your listing ({beat.note})]].",
    "perf_spike":
        "Your {trg.metric} are up {trg.delta_pct} week-on-week"
        "[[ — likely your {trg.likely_driver}]].",
    "milestone_reached":
        "You're at {trg.value_now} {trg.metric}[[ — {trg.milestone_value} is the "
        "next visible mark]].",
    "review_theme_emerged":
        "\"{trg.theme}\" has come up {trg.occurrences_30d} times in your reviews "
        "this month[[ — one reads: \"{review.neg_quote}\"]].",
    "gbp_unverified":
        "Your Google listing is still unverified[[, and that caps roughly "
        "{trg.estimated_uplift_pct} of your visibility]]"
        "[[ — you're at {perf.views} views]].",
    "renewal_due":
        "Your {subs.plan} plan lapses in {trg.days_remaining} days"
        "[[ — {perf.views} views and {perf.calls} calls ride on it]].",
    "winback_eligible":
        "Your listing has been off-plan {trg.days_since_expiry} days"
        "[[ — {trg.lapsed_customers_added_since_expiry} customers searched in that window]].",
    "dormant_with_vera":
        "We haven't spoken in {trg.days_since_last_merchant_message} days"
        "[[ — since then your views moved {delta.views}]].",
    "curious_ask_due":
        "One question, not a nudge[[ — you're at {perf.views} views this month]].",
    "active_planning_intent":
        "Picking up your {trg.intent_topic} — I've got the first version ready.",
    "scheduled_recurring":
        "Weekly check[[ — {perf.views} views, {perf.calls} calls]].",

    # --- customer-facing -------------------------------------------------
    "recall_due":
        "Your {trg.service_due} is due[[ ({trg.due_date})]]"
        "[[ — it's been since {cust.last_visit}]].",
    "appointment_tomorrow":
        "Reminder: you're booked for tomorrow[[, {trg.slot_label}]].",
    "customer_lapsed_soft":
        "It's been {trg.days_since_last_visit} days since your last visit"
        "[[ ({cust.last_service})]].",
    "customer_lapsed_hard":
        "It's been {trg.days_since_last_visit} days"
        "[[ — you were {trg.previous_membership_months} months in, working on "
        "{trg.previous_focus}]].",
    "chronic_refill_due":
        "Your course runs out {trg.stock_runs_out_iso}"
        "[[ — {trg.molecule_list}]].",
    "trial_followup":
        "You came in for your trial on {trg.trial_date}.",
    "wedding_package_followup":
        "Your date is {trg.days_to_wedding} days out[[ ({trg.wedding_date})]].",
    "customer_lapsed":
        "It's been {trg.days_since_last_visit} days since your last visit.",
}

# Fallback hooks, used when a kind's own template renders empty because none of
# its facts were pushed. Ordered most- to least-specific.
GENERIC_HOOKS: tuple[str, ...] = (
    "Your listing pulled {perf.views} views and {perf.calls} calls[[ in the last 30 days]].",
    "Your views moved {delta.views} week-on-week.",
    "You're running {offer.active}.",
    "Your listing is live in {locality}.",
)


# Customer-facing fallbacks. The merchant-oriented GENERIC_HOOKS above talk about
# views and calls, which must never appear in a message sent *to* a customer as
# the merchant -- so customer scope gets its own ladder.
GENERIC_HOOKS_CUSTOMER: tuple[str, ...] = (
    "It's been since {cust.last_visit} — you were last in for {cust.last_service}.",
    "It's been a while since your last visit ({cust.last_visit}).",
    "You've been in {cust.visits} times with us.",
    "Checking in from {biz}.",
)

# When a kind is reinterpreted for a category it doesn't natively fit, the offer
# of work has to be reinterpreted too. Leaving the original ("so you do not run
# out") would reintroduce the pharmacy framing the reframe just removed.
REFRAME_WORK: dict[str, dict[str, str]] = {
    "chronic_refill_due": {
        "dentists": "book the next one in before the gap gets longer",
        "gyms": "keep your membership running without a break",
        "salons": "hold your usual slot before it fills",
        "restaurants": "keep your standing order going",
    },
    "recall_due": {
        "gyms": "hold a slot for your check-in",
        "restaurants": "keep a table for you",
        "salons": "hold your usual slot",
    },
}


def reframed_work(spec: "KindSpec", category_slug: str) -> Optional[str]:
    return REFRAME_WORK.get(spec.kind, {}).get(category_slug)
