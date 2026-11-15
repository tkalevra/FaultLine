-- 234_agent_eval_rounds.sql — CONSTRAINED customer-agent EVALUATION GAUNTLET, CLOSED-SOURCE SaaS.
--
-- THE PRODUCT'S INSPECTION BAR. The constrained agent passes when it BEATS the tenant's
-- reference three consecutive rounds. A round is: the critic takes a question the customer
-- would ask, gets the agent's REAL reply (fresh context, never a summary), strips every label,
-- and a BLIND judge picks which of two answers better represents the tenant, accurately
-- describes their services, and actually helps the customer while staying in scope. On a loss
-- the critic returns the single largest meaningful gap; the agent is revised and re-entered.
--
-- This table is the ROUND LOG: every round's question, both answers, the blind verdict, the
-- single-gap feedback (on a loss), the Cortex usage (recall/train/expand calls + token counts)
-- and any hiccups (recall failures, brain faults, triage refusals, abstentions). Read via the
-- admin portal so a tenant admin can WATCH the gauntlet rather than trust a summary.
--
-- WHY A TABLE (not a log line): the gauntlet runs server-side over real chat turns, the rounds
-- must survive restart, and the admin portal must render history + consecutive-win state. This
-- also keeps every round's provenance (which seat, which question, both answers) inspectable —
-- the whole point of the bar is that the judge inspects REAL output, not a recap.

CREATE TABLE IF NOT EXISTS public.agent_eval_rounds (
    id               BIGSERIAL PRIMARY KEY,
    account_id       UUID NOT NULL,
    seat_id          UUID NOT NULL,
    round_no         INT  NOT NULL,
    question         TEXT NOT NULL,
    agent_answer     TEXT NOT NULL,
    reference_answer TEXT NOT NULL,
    reference_kind   TEXT NOT NULL DEFAULT 'gold',   -- gold | website
    judge_verdict    TEXT NOT NULL,                  -- agent | reference
    agent_won        BOOLEAN NOT NULL,
    gap_feedback     TEXT,                            -- critic's single-largest-gap on a loss
    cortex_usage     JSONB NOT NULL DEFAULT '{}'::jsonb,
    hiccups          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_eval_rounds_seat
    ON public.agent_eval_rounds (account_id, seat_id, round_no DESC);

COMMENT ON TABLE public.agent_eval_rounds IS
    'Round log for the CONSTRAINED customer-agent evaluation gauntlet (SPEC: blind judge vs the '
    'tenant''s reference; passes on 3 consecutive wins). Every round records the question, both '
    'answers, the blind verdict, single-gap feedback on a loss, Cortex usage and hiccups. '
    'Written by saas/agent_gauntlet.py; read by the tenant console admin portal.';

COMMENT ON COLUMN public.agent_eval_rounds.reference_kind IS
    'How the reference answer was produced: gold = the tenant''s authored gold answer set; '
    'website = composed by the critic from the tenant''s official website reference.';

COMMENT ON COLUMN public.agent_eval_rounds.cortex_usage IS
    'Per-round Cortex usage: recall calls/chars, train (ingest) writes, expand/learn firings, '
    'and token counts where available. Logged so the gauntlet''s Cortex spend is inspectable.';

COMMENT ON COLUMN public.agent_eval_rounds.hiccups IS
    'Per-round hiccups: recall failures, brain faults, triage refusals, abstentions, and any '
    'transport errors. A hiccup is not a win even if the judge would have voted for the agent.';
