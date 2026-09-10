"""
Create knowledge_base_entries and seed 5 active entries (Subtask 2.1.3).

knowledge_base_entries is NOT tenant-bearing — entries are shared across all
clients (same KB, different client routing). No RLS, no client_id column.

    PYTHONPATH=. python migrations/apply_knowledge_base.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = [
    """
    CREATE TABLE IF NOT EXISTS knowledge_base_entries (
        entry_id                    SERIAL PRIMARY KEY,
        topic                       VARCHAR(120) NOT NULL,
        trigger_patterns            TEXT[]       NOT NULL,
        approved_response_template  TEXT         NOT NULL,
        min_confidence_threshold    NUMERIC(4,3) NOT NULL DEFAULT 0.900,
        is_active                   BOOLEAN      NOT NULL DEFAULT TRUE,
        approved_count              INTEGER      NOT NULL DEFAULT 0,
        created_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_kb_entries_active ON knowledge_base_entries (entry_id) WHERE is_active = TRUE",
    "GRANT SELECT ON knowledge_base_entries TO blackink_app",
    "GRANT SELECT, INSERT, UPDATE ON knowledge_base_entries TO blackink_system",
    "GRANT USAGE ON SEQUENCE knowledge_base_entries_entry_id_seq TO blackink_system",
]

# 5 seed entries — no dollar amounts anywhere in response templates.
# Each must offer a meeting / next step, not quote fees.
SEED_ENTRIES = [
    {
        "topic": "How does this work?",
        "trigger_patterns": [
            "how does this work",
            "how does it work",
            "how does your service work",
            "what is this",
            "what do you do",
            "explain your service",
            "tell me more",
            "how does blackink work",
        ],
        "approved_response_template": (
            "Great question — I'd love to walk you through it properly.\n\n"
            "In short, Blackink analyses your portfolio's online visibility against "
            "every other property manager in your county, identifies where owners "
            "are being lost to competitors, and gives you a concrete action plan to "
            "capture that revenue. It takes about 15 minutes to show you what this "
            "looks like for your specific market.\n\n"
            "Would it make sense to find a time this week to go through your report together?"
        ),
        "min_confidence_threshold": 0.900,
    },
    {
        "topic": "What does it cost?",
        "trigger_patterns": [
            "what does it cost",
            "how much does it cost",
            "what is the price",
            "how much do you charge",
            "what are your fees",
            "what is the pricing",
            "how much is it",
            "what is your fee",
        ],
        "approved_response_template": (
            "I appreciate you asking directly.\n\n"
            "Rather than quoting a number out of context, I find it's much more "
            "useful to show you what the opportunity looks like for your portfolio "
            "first — the value Blackink delivers varies a lot based on door count "
            "and market, and I'd rather give you something concrete to weigh it against.\n\n"
            "Would you be open to a quick 15-minute call so I can show you your "
            "county report? You'll have a clear picture of whether this makes sense "
            "for you before we talk about anything else."
        ),
        "min_confidence_threshold": 0.900,
    },
    {
        "topic": "What areas do you cover?",
        "trigger_patterns": [
            "what areas do you cover",
            "which areas",
            "what counties",
            "do you cover my area",
            "what markets",
            "which markets",
            "where do you operate",
            "what locations",
            "do you work in",
        ],
        "approved_response_template": (
            "We're currently active across several Florida counties — Hillsborough, "
            "Pinellas, and a few others we're expanding into.\n\n"
            "If you let me know which county your portfolio is in, I can tell you "
            "whether we have benchmark data for your market and what that looks like "
            "for firms your size.\n\n"
            "Happy to pull up your county report on a quick call — usually takes about 15 minutes."
        ),
        "min_confidence_threshold": 0.900,
    },
    {
        "topic": "How long does it take?",
        "trigger_patterns": [
            "how long does it take",
            "how long does this take",
            "how long until",
            "how quickly",
            "what is the timeline",
            "when will i see results",
            "how fast will i see results",
            "turnaround time",
            "time to results",
        ],
        "approved_response_template": (
            "Good question — the audit itself runs in minutes once we have your "
            "portfolio details.\n\n"
            "Most clients see their full Owner Visibility Score and county ranking "
            "within 24 hours of our first call. From there, the action plan is "
            "something we review together so you know exactly what to tackle first.\n\n"
            "Want to get the clock started? I can set up a 15-minute intro and we "
            "can have your report ready before the week is out."
        ),
        "min_confidence_threshold": 0.900,
    },
    {
        "topic": "Is this a guarantee?",
        "trigger_patterns": [
            "is this a guarantee",
            "do you guarantee",
            "do you offer a guarantee",
            "money back guarantee",
            "do you offer a refund",
            "what if it doesn't work",
            "what happens if it doesnt work",
            "what happens if this doesnt work out",
            "risk free",
            "no risk",
        ],
        "approved_response_template": (
            "That's a fair thing to ask about.\n\n"
            "I don't want to make a promise that oversimplifies what we do — what I "
            "can tell you is that every recommendation we make comes from your own "
            "county's benchmark data, not a generic playbook. You'll see exactly what "
            "we're basing it on before committing to anything.\n\n"
            "The best way I can back that up is to show you the report first. If it "
            "doesn't show a clear opportunity worth pursuing, you'll know immediately. "
            "Can we find 15 minutes this week?"
        ),
        "min_confidence_threshold": 0.900,
    },
]


def main() -> int:
    with get_owner_db_context() as db:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()

    with get_owner_db_context() as db:
        for entry in SEED_ENTRIES:
            existing = db.execute(
                text("SELECT entry_id FROM knowledge_base_entries WHERE topic = :topic"),
                {"topic": entry["topic"]},
            ).first()
            if existing:
                # Update trigger_patterns on every run so re-running the migration
                # propagates pattern changes to already-seeded rows.
                db.execute(
                    text(
                        "UPDATE knowledge_base_entries "
                        "SET trigger_patterns = :patterns "
                        "WHERE topic = :topic"
                    ),
                    {"patterns": entry["trigger_patterns"], "topic": entry["topic"]},
                )
                print(f"  updated patterns: {entry['topic']!r}")
                continue
            db.execute(
                text(
                    "INSERT INTO knowledge_base_entries "
                    "(topic, trigger_patterns, approved_response_template, min_confidence_threshold) "
                    "VALUES (:topic, :patterns, :template, :threshold)"
                ),
                {
                    "topic":     entry["topic"],
                    "patterns":  entry["trigger_patterns"],
                    "template":  entry["approved_response_template"],
                    "threshold": entry["min_confidence_threshold"],
                },
            )
            print(f"  seeded: {entry['topic']!r}")
        db.commit()

    print("apply_knowledge_base: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
