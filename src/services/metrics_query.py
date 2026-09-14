"""Shared platform metrics query used by the digest and admin API."""

METRICS_SQL = """
WITH dispatch_cohort AS (
    SELECT client_id, payload->>'dispatch_id' AS dispatch_id
    FROM events
    WHERE event_type = 'outbound_touch_dispatched'
      AND payload->>'channel' = 'email'
      AND payload->>'dispatch_id' IS NOT NULL
      AND created_at >= NOW() - :window
      AND client_id <> ALL(:demo_client_ids)
),
engagement_cohort AS (
    SELECT e.event_type
    FROM events e
    JOIN dispatch_cohort d
      ON d.client_id = e.client_id
     AND d.dispatch_id = e.payload->>'dispatch_id'
    WHERE e.event_type IN ('email_opened', 'email_clicked', 'email_replied')
)
SELECT
    COUNT(*) FILTER (WHERE event_type = 'owner_visibility_score_calculated') AS scores_generated,
    COUNT(DISTINCT payload->>'county_slug') FILTER (WHERE event_type = 'owner_visibility_score_calculated') AS county_rank_reports_delivered,
    (SELECT COUNT(*) FROM dispatch_cohort) AS cold_emails_dispatched,
    ROUND(100.0 * (SELECT COUNT(*) FROM engagement_cohort WHERE event_type = 'email_opened')
          / NULLIF((SELECT COUNT(*) FROM dispatch_cohort), 0), 1) AS open_rate_pct,
    ROUND(100.0 * (SELECT COUNT(*) FROM engagement_cohort WHERE event_type = 'email_clicked')
          / NULLIF((SELECT COUNT(*) FROM dispatch_cohort), 0), 1) AS click_rate_pct,
    ROUND(100.0 * (SELECT COUNT(*) FROM engagement_cohort WHERE event_type = 'email_replied')
          / NULLIF((SELECT COUNT(*) FROM dispatch_cohort), 0), 1) AS reply_rate_pct,
    COUNT(*) FILTER (WHERE event_type = 'meeting_booked') AS appointments_booked
FROM events
WHERE created_at >= NOW() - :window
  AND client_id <> ALL(:demo_client_ids)
"""
