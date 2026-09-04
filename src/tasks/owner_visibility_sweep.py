"""Owner Visibility Score sweep — scores every active company for the current month.

Usage:
    python -m src.tasks.owner_visibility_sweep

Runs as blackink_system (BYPASSRLS) so it can read all companies across
tenants and upsert scores without tenant context. Same pattern as
hunter_nightly_sweep.py.

One upsert per company per month_key. Re-running in the same month
overwrites the previous score (ON CONFLICT DO UPDATE) so the sweep is
safe to re-run after the DBPR CSV is refreshed or after a Google API key
is provisioned mid-month.

google_place_id persistence: when the live Google provider resolves a
place_id that wasn't already on the company row, this sweep writes it
back so future months skip the text-search cost.
"""
import json
import logging
import sys
from datetime import datetime, timezone

from src.core.database import get_system_db_context
from src.services.owner_visibility.county_rank import calculate_county_ranks
from src.services.owner_visibility.score_calculator import calculate_score
from src.services.owner_visibility.signals.website import WebsiteSignalProvider
from src.services.owner_visibility.signals.dbpr_licence import DbprLicenceSignalProvider
from src.services.owner_visibility.signals.google_places import build_google_places_provider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger(__name__)


def _current_month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def run_sweep() -> int:
    """Score all active companies. Returns count of rows upserted."""
    from sqlalchemy import text

    month_key = _current_month_key()
    website_provider = WebsiteSignalProvider()
    dbpr_provider = DbprLicenceSignalProvider()
    google_provider = build_google_places_provider()

    logger.info("owner_visibility_sweep: starting month=%s", month_key)

    with get_system_db_context() as db:
        rows = db.execute(
            text(
                "SELECT company_id, company_name, domain, website, county_slug, google_place_id "
                "FROM companies "
                "WHERE status = 'PROSPECTING' OR status = 'ENGAGED' "
                "ORDER BY company_id"
            )
        ).fetchall()

    logger.info("owner_visibility_sweep: %d companies to score", len(rows))
    upserted = 0

    for row in rows:
        company: dict = {
            "company_id": row.company_id,
            "company_name": row.company_name,
            "domain": row.domain,
            "website": row.website,
            "county_slug": row.county_slug,
            "google_place_id": row.google_place_id,
        }

        signals = (
            website_provider.collect(company)
            + dbpr_provider.collect(company)
            + google_provider.collect(company)
        )
        breakdown = calculate_score(signals)

        with get_system_db_context() as db:
            db.execute(
                text(
                    "INSERT INTO owner_visibility_scores "
                    "  (company_id, month_key, county_slug, score_total, "
                    "   score_website, score_dbpr, score_google, "
                    "   signal_detail, data_gaps, scored_at) "
                    "VALUES "
                    "  (:company_id, :month_key, :county_slug, :score_total, "
                    "   :score_website, :score_dbpr, :score_google, "
                    "   :signal_detail::jsonb, :data_gaps, NOW()) "
                    "ON CONFLICT (company_id, month_key) DO UPDATE SET "
                    "  county_slug    = EXCLUDED.county_slug, "
                    "  score_total    = EXCLUDED.score_total, "
                    "  score_website  = EXCLUDED.score_website, "
                    "  score_dbpr     = EXCLUDED.score_dbpr, "
                    "  score_google   = EXCLUDED.score_google, "
                    "  signal_detail  = EXCLUDED.signal_detail, "
                    "  data_gaps      = EXCLUDED.data_gaps, "
                    "  scored_at      = EXCLUDED.scored_at"
                ),
                {
                    "company_id": company["company_id"],
                    "month_key": month_key,
                    "county_slug": company["county_slug"],
                    "score_total": breakdown.score_total,
                    "score_website": breakdown.score_website,
                    "score_dbpr": breakdown.score_dbpr,
                    "score_google": breakdown.score_google,
                    "signal_detail": json.dumps(breakdown.signal_detail),
                    "data_gaps": breakdown.data_gaps,
                },
            )
            upserted += 1

            # Persist a newly-resolved google_place_id so future sweeps skip the text-search.
            if company.get("google_place_id") and company["google_place_id"] != row.google_place_id:
                db.execute(
                    text(
                        "UPDATE companies SET google_place_id = :place_id "
                        "WHERE company_id = :company_id"
                    ),
                    {"place_id": company["google_place_id"], "company_id": company["company_id"]},
                )

    logger.info("owner_visibility_sweep: done scoring, upserted=%d for month=%s", upserted, month_key)

    _update_county_ranks(month_key)
    return upserted


def _update_county_ranks(month_key: str) -> None:
    """Compute county ranks for every county that has scores for month_key.

    Runs as a second pass after all scores are written so every firm in the
    county is present before ranking begins. Overwrites county_rank,
    county_percentile, and peer_comparisons on each row.
    """
    from sqlalchemy import text

    with get_system_db_context() as db:
        county_slugs = [
            r[0]
            for r in db.execute(
                text(
                    "SELECT DISTINCT county_slug FROM owner_visibility_scores "
                    "WHERE month_key = :month_key"
                ),
                {"month_key": month_key},
            ).fetchall()
        ]

    logger.info("owner_visibility_sweep: ranking %d counties for month=%s", len(county_slugs), month_key)

    for county_slug in county_slugs:
        with get_system_db_context() as db:
            raw_rows = db.execute(
                text(
                    "SELECT o.score_id, o.company_id, c.company_name, "
                    "       o.score_total, o.signal_detail "
                    "FROM owner_visibility_scores o "
                    "JOIN companies c ON c.company_id = o.company_id "
                    "WHERE o.county_slug = :county_slug AND o.month_key = :month_key"
                ),
                {"county_slug": county_slug, "month_key": month_key},
            ).fetchall()

        rows = [
            {
                "score_id":     r.score_id,
                "company_id":   r.company_id,
                "company_name": r.company_name,
                "score_total":  r.score_total,
                "signal_detail": r.signal_detail or {},
            }
            for r in raw_rows
        ]

        ranked = calculate_county_ranks(rows)

        with get_system_db_context() as db:
            for row in ranked:
                db.execute(
                    text(
                        "UPDATE owner_visibility_scores SET "
                        "  county_rank       = :county_rank, "
                        "  county_percentile = :county_percentile, "
                        "  peer_comparisons  = :peer_comparisons::jsonb "
                        "WHERE score_id = :score_id"
                    ),
                    {
                        "county_rank":       row["county_rank"],
                        "county_percentile": row["county_percentile"],
                        "peer_comparisons":  json.dumps(row["peer_comparisons"]),
                        "score_id":          row["score_id"],
                    },
                )
            logger.info(
                "owner_visibility_sweep: ranked county=%s (%d firms)", county_slug, len(ranked)
            )

    logger.info("owner_visibility_sweep: county ranking complete for month=%s", month_key)


def main() -> int:
    try:
        scored = run_sweep()
        logger.info("owner_visibility_sweep: scored %d companies", scored)
        return 0
    except Exception:
        logger.exception("owner_visibility_sweep: fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
