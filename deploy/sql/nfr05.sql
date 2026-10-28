-- NFR-05 (PRD §17; M05 acceptance, S05.2 §3.9): how fast triage finishes
-- and urgent alerts reach the owner, and how many immediate alerts the
-- owner marked "not useful" (PRD §21 R2 #7). Run on the server with
-- `just prod <ip> nfr05 [days]` (deploy/server.sh: psql, read-only
-- transaction, psql variable `days`); tested in
-- tests/integration/test_nfr05_query.py.
--
-- Content-free by construction: it reads ids, run types, statuses, kinds,
-- feedback codes and timestamps (agent_runs, notifications, messages.sent_at),
-- never a content column (privacy/content_columns.py; the test checks every
-- one of them).
--
-- Measures:
--   triage completion       succeeded `triage` runs: finished_at minus the
--                           sent_at of the newest message the run read, named
--                           in its trigger_ref (`triage:conv:<id>:through:<id>`,
--                           agent/triage.py). Target p95 < 90 s.
--   urgent alert delivery   sent `triage_urgent` notifications: sent_at minus
--                           the newest sent_at among refs.message_ids. Target
--                           p95 < 2 min.
--   immediate alerts marked not useful
--                           standard alerts sent in the window, and the share
--                           the owner marked `not_useful` (informational).
WITH triage_runs AS (
    -- The cast only where the shape is right: a malformed trigger_ref is
    -- skipped, never an error that stops the whole report.
    SELECT
        r.account_id,
        r.finished_at,
        CASE
            WHEN r.trigger_ref ~ '^triage:conv:[0-9]+:through:[0-9]+$'
                THEN CAST(split_part(r.trigger_ref, ':', 5) AS bigint)
        END AS through_id
    FROM app.agent_runs AS r
    WHERE r.run_type = 'triage'
        AND r.status = 'succeeded'
        AND r.finished_at >= now() - make_interval(days => CAST(:days AS integer))
),
triage AS (
    SELECT extract(EPOCH FROM t.finished_at - m.sent_at) AS s
    FROM triage_runs AS t
    JOIN app.messages AS m
        ON m.account_id = t.account_id
        AND m.id = t.through_id
),
urgent AS (
    SELECT extract(EPOCH FROM n.sent_at - newest.sent_at) AS s
    FROM app.notifications AS n
    CROSS JOIN LATERAL (
        SELECT max(m.sent_at) AS sent_at
        FROM jsonb_array_elements_text(coalesce(n.refs -> 'message_ids', '[]'::jsonb)) AS ref (id)
        JOIN app.messages AS m
            ON m.account_id = n.account_id
            AND m.id = CAST(ref.id AS bigint)
    ) AS newest
    WHERE n.kind = 'triage_urgent'
        AND n.status = 'sent'
        AND newest.sent_at IS NOT NULL
        AND n.sent_at >= now() - make_interval(days => CAST(:days AS integer))
),
immediate AS (
    SELECT coalesce(n.feedback = 'not_useful', false) AS not_useful
    FROM app.notifications AS n
    WHERE n.alert_class = 'standard'
        AND n.status = 'sent'
        AND n.sent_at >= now() - make_interval(days => CAST(:days AS integer))
),
measured (ord, measure, n, p50, p95, limit_s, share) AS (
    SELECT
        1, 'triage completion', count(*),
        percentile_cont(0.5) WITHIN GROUP (ORDER BY s),
        percentile_cont(0.95) WITHIN GROUP (ORDER BY s),
        90, CAST(NULL AS float8)
    FROM triage
    UNION ALL
    SELECT
        2, 'urgent alert delivery', count(*),
        percentile_cont(0.5) WITHIN GROUP (ORDER BY s),
        percentile_cont(0.95) WITHIN GROUP (ORDER BY s),
        120, CAST(NULL AS float8)
    FROM urgent
    UNION ALL
    SELECT
        3, 'immediate alerts marked not useful', count(*),
        CAST(NULL AS float8), CAST(NULL AS float8),
        CAST(NULL AS integer), avg(CASE WHEN not_useful THEN 1.0 ELSE 0.0 END)
    FROM immediate
)
SELECT
    measure,
    n,
    round(p50::numeric, 1) AS p50_s,
    round(p95::numeric, 1) AS p95_s,
    limit_s AS p95_limit_s,
    round((share * 100)::numeric, 1) AS not_useful_pct,
    CASE
        WHEN n = 0 THEN 'no data'
        WHEN limit_s IS NULL THEN 'informational'
        WHEN p95 < limit_s THEN 'met'
        ELSE 'NOT met'
    END AS verdict
FROM measured
ORDER BY ord;
