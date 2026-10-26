-- NFR-04 (PRD; M03 acceptance, IMPLEMENTATION_PLAN §5): how fast owner
-- requests are answered, from `agent_runs` timings. Run on the server with
-- `just prod <ip> nfr04 [days]` (deploy/server.sh: psql, read-only
-- transaction, psql variable `days`); tested in
-- tests/integration/test_nfr04_query.py.
--
-- Content-free by construction: it reads ids, run types, statuses,
-- timestamps and tool names (agent_runs, agent_run_steps) and the owner
-- turn's id, role and arrival time (control_turns), never a content column
-- (privacy/content_columns.py; the test checks every one of them).
--
-- Classes (one owner request = one `owner_request` run):
--   catch-up        the run called `summarize_period` (the catch-up tool);
--   attention list  it called `get_attention_list` and no `summarize_period`;
--                   held to the catch-up bound, because it summarizes stale
--                   chats inline (M03 D62);
--   simple answer   every other owner request.
-- Durations: `run` = started_at -> finished_at; `reply` = the owner's
-- message (control_turns.created_at, Telegram's time) -> finished_at, so it
-- adds the wait in the queue. The verdict uses the reply p95 (what the owner
-- waits), over succeeded runs; failed or refused runs are counted apart.
WITH runs AS (
    SELECT
        r.id,
        r.status = 'succeeded' AS ok,
        extract(EPOCH FROM r.finished_at - r.started_at) AS run_s,
        extract(EPOCH FROM r.finished_at - coalesce(t.created_at, r.started_at)) AS reply_s,
        CASE
            WHEN EXISTS (
                SELECT 1 FROM app.agent_run_steps AS s
                WHERE s.run_id = r.id AND s.tool_name = 'summarize_period'
            ) THEN 'catch-up'
            WHEN EXISTS (
                SELECT 1 FROM app.agent_run_steps AS s
                WHERE s.run_id = r.id AND s.tool_name = 'get_attention_list'
            ) THEN 'attention list'
            ELSE 'simple answer'
        END AS kind
    FROM app.agent_runs AS r
    LEFT JOIN app.control_turns AS t
        ON t.role = 'owner' AND r.trigger_ref = 'turn:' || t.id
    WHERE r.run_type = 'owner_request'
        AND r.finished_at IS NOT NULL
        AND r.started_at >= now() - make_interval(days => CAST(:days AS integer))
),
classes (kind, limit_s, ord) AS (
    VALUES
        ('simple answer', 20, 1),
        ('catch-up', 60, 2),
        ('attention list', 60, 3),
        ('all owner requests', NULL, 4)
),
measured AS (
    SELECT
        c.ord,
        c.kind,
        c.limit_s,
        count(r.id) FILTER (WHERE r.ok) AS n,
        count(r.id) FILTER (WHERE NOT r.ok) AS not_ok,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_s) FILTER (WHERE r.ok) AS run_p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY r.run_s) FILTER (WHERE r.ok) AS run_p95,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY r.reply_s) FILTER (WHERE r.ok) AS reply_p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY r.reply_s) FILTER (WHERE r.ok) AS reply_p95,
        max(r.reply_s) FILTER (WHERE r.ok) AS reply_max
    FROM classes AS c
    LEFT JOIN runs AS r ON r.kind = c.kind OR c.limit_s IS NULL
    GROUP BY c.ord, c.kind, c.limit_s
)
SELECT
    kind AS class,
    n,
    not_ok,
    round(run_p50::numeric, 1) AS run_p50_s,
    round(run_p95::numeric, 1) AS run_p95_s,
    round(reply_p50::numeric, 1) AS reply_p50_s,
    round(reply_p95::numeric, 1) AS reply_p95_s,
    round(reply_max::numeric, 1) AS reply_max_s,
    limit_s AS p95_limit_s,
    CASE
        WHEN limit_s IS NULL AND n >= 20 THEN 'enough requests (20 or more)'
        WHEN limit_s IS NULL THEN 'fewer than 20 requests'
        WHEN n = 0 THEN 'no data'
        WHEN reply_p95 < limit_s THEN 'met'
        ELSE 'NOT met'
    END AS verdict
FROM measured
ORDER BY ord;
