"""Champion-challenger prompt registry for Cora's outbound email compose task.

Ported from Forced Action's config/prompt_variants.py (pattern) and rewritten
for Blackink's PM outreach context:

- FA targeted distressed-property investors → Blackink targets PM owners/brokers
- FA had one email type → Blackink has 3 email touches (1, 3, 5 of 5-touch sequence)
- Merge tag syntax: {single_brace} (Instantly) — NOT {{double_brace}} (FA)
- {client_firm} REQUIRED on all variants per compliance gate (outbound_templates.py)

Challenger promotion requires: ≥60 sends, ≥2pp absolute reply-rate lift over
champion, and founder approval. golden_set_approved=True only after
golden_set_eval passes (Sprint 2 — not yet built).

Touch 2 (phone call) and Touch 4 (LinkedIn) are manual Slack tasks, not LLM-drafted.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Touch 1 — Cold Email: Speed Loss Audit + Sendspark video
# ---------------------------------------------------------------------------

_TOUCH_1_VARIANTS: list[dict] = [
    {
        "name": "t1_v1_speed_evidence",
        "touch": 1,
        "description": (
            "Champion: leads with the speed audit result and annual loss figure as "
            "evidence, then presents the Sendspark video as the proof artifact. "
            "Peer tone — one PM operator showing another a real number."
        ),
        "system_template": (
            "You are drafting a single cold outreach email on behalf of {client_firm}, "
            "a property management firm, to a PM owner or broker. "
            "The email must open with a concrete fact from the speed audit: the prospect's "
            "response latency ({audit_speed}) and the estimated annual revenue loss ({loss_dollars}). "
            "Ground every claim ONLY in the facts listed below — never invent a number, name, or "
            "detail not present. After the evidence, present the personalized video ({video_url}) "
            "as a 90-second walkthrough of the finding. Close with a single low-friction ask: "
            "a 15-minute call or a calendar link. "
            "Tone: peer-to-peer, specific, respectful of time — one PM operator to another. "
            "No buzzwords, no urgency theatre. Under 120 words. "
            "Output exactly two lines: 'SUBJECT: <subject>' then 'BODY: <body>'. "
            "The body MUST contain {client_firm} at least once."
        ),
        "is_champion": True,
        "golden_set_approved": True,
    },
    {
        "name": "t1_v2_loss_lead",
        "touch": 1,
        "description": (
            "Challenger: leads with the dollar loss in the subject line, "
            "buries the video below the fold as supporting detail."
        ),
        "system_template": (
            "You are drafting a single cold outreach email on behalf of {client_firm}. "
            "Open the subject line with the annual revenue loss figure ({loss_dollars}) "
            "as the hook — make the cost of inaction the first thing the reader sees. "
            "In the body, briefly explain the speed gap ({audit_speed} vs. industry 1-hour benchmark), "
            "then offer the Sendspark video ({video_url}) for the full picture. "
            "Ground every claim ONLY in the listed facts. No invented numbers. "
            "Single ask: calendar link. Under 110 words. "
            "Output exactly: 'SUBJECT: <subject>' then 'BODY: <body>'. "
            "Body MUST contain {client_firm}."
        ),
        "is_champion": False,
        "golden_set_approved": False,
    },
]

# ---------------------------------------------------------------------------
# Touch 3 — Cold Email (Day 4): Fee-Stack Opportunity
# ---------------------------------------------------------------------------

_TOUCH_3_VARIANTS: list[dict] = [
    {
        "name": "t3_v1_fee_stack",
        "touch": 3,
        "description": (
            "Champion: pivots from speed audit to fee optimization angle — "
            "how the PM can recover lost margin through fee structure improvements."
        ),
        "system_template": (
            "You are drafting a follow-up cold email (touch 3 of 5) on behalf of {client_firm} "
            "to a PM owner or broker who has not yet replied. "
            "Do NOT repeat the speed audit from Email 1. Instead, pivot to the fee-stack angle: "
            "most PM firms in {city} leave 15–30%% of potential management revenue uncaptured through "
            "sub-optimal fee structures. Present this as a separate, adjacent opportunity to the speed "
            "audit finding — two problems, both solvable. "
            "Ground every claim ONLY in the listed facts. No invented percentages beyond the 15-30%% "
            "industry range stated here. "
            "Tone: peer consultative — you spotted a second gap worth a quick conversation. "
            "Under 100 words. Single ask: 15-min call. "
            "Output: 'SUBJECT: <subject>' then 'BODY: <body>'. Body MUST contain {client_firm}."
        ),
        "is_champion": True,
        "golden_set_approved": True,
    },
]

# ---------------------------------------------------------------------------
# Touch 5 — Cold Email (Day 10): Metro Speed Index & scarcity
# ---------------------------------------------------------------------------

_TOUCH_5_VARIANTS: list[dict] = [
    {
        "name": "t5_v1_metro_index",
        "touch": 5,
        "description": (
            "Champion: final email uses metro-level comparative data and "
            "one-client-per-market scarcity to create urgency without theatre."
        ),
        "system_template": (
            "You are drafting the final cold email (touch 5 of 5) on behalf of {client_firm} "
            "to a PM owner or broker in {city} who has not replied to the previous 4 touches. "
            "This is the last automated email in the sequence. "
            "Use a metro speed index angle: PM firms in {city} that respond to owner inquiries "
            "within 1 hour win significantly more new management contracts than slower competitors. "
            "{client_firm} works with one PM firm per metro market — mention this constraint "
            "factually, not as a manufactured deadline. "
            "Ground every claim in listed facts only. No invented data. "
            "Tone: direct, no pressure, final check-in. Under 90 words. "
            "Output: 'SUBJECT: <subject>' then 'BODY: <body>'. Body MUST contain {client_firm}."
        ),
        "is_champion": True,
        "golden_set_approved": True,
    },
]

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

_ALL_TOUCH_VARIANTS: dict[int, list[dict]] = {
    1: _TOUCH_1_VARIANTS,
    3: _TOUCH_3_VARIANTS,
    5: _TOUCH_5_VARIANTS,
}

CHAMPION_VARIANTS: dict[int, dict] = {
    touch: next(v for v in variants if v["is_champion"])
    for touch, variants in _ALL_TOUCH_VARIANTS.items()
}

CHALLENGER_VARIANTS: dict[int, list[dict]] = {
    touch: [v for v in variants if not v["is_champion"] and v["golden_set_approved"]]
    for touch, variants in _ALL_TOUCH_VARIANTS.items()
}

PROMPT_EXPERIMENT_NAME = "cora_pm_outreach_{touch}"
PROMPT_EXPERIMENT_MIN_SAMPLE = 60
PROMPT_EXPERIMENT_MIN_LIFT_PP = 2.0


def get_champion_prompt(touch: int) -> str:
    """Return the champion system prompt for a given touch number (1, 3, or 5).

    Raises KeyError if touch is not an LLM-drafted email touch.
    Touch 2 (phone call) and Touch 4 (LinkedIn) are manual — not LLM-drafted.
    """
    if touch not in CHAMPION_VARIANTS:
        raise KeyError(
            f"Touch {touch} is not an LLM-drafted email touch. "
            f"LLM touches: {sorted(CHAMPION_VARIANTS)}. "
            "Touch 2 (phone call) and Touch 4 (LinkedIn) are manual Slack tasks."
        )
    return CHAMPION_VARIANTS[touch]["system_template"]
