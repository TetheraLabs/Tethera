-- Paper agents: a registered strategy running live on the chain feed. Fills are rows in desk_record (hash-chained);
-- these tables hold the agent's identity, its book per symbol, and equity marks for the public board.
CREATE TABLE IF NOT EXISTS desk_agents (
    id            text PRIMARY KEY,
    name          text NOT NULL,
    engine        text NOT NULL,                -- composer, robophd, loop
    genome        jsonb NOT NULL,               -- the strategy definition (served only to key holders)
    genome_sha    text NOT NULL,
    registration  text,                         -- ledger hash of the pre-registration
    symbols       text[] NOT NULL,
    tf            text NOT NULL,
    notional      double precision NOT NULL DEFAULT 10000,
    slippage_bps  double precision NOT NULL DEFAULT 15,
    status        text NOT NULL DEFAULT 'paper', -- paper | paused | retired
    label         text NOT NULL DEFAULT 'candidate',  -- candidate | survivor
    started       timestamptz NOT NULL DEFAULT now(),
    note          text
);
CREATE TABLE IF NOT EXISTS desk_agent_book (
    agent_id      text REFERENCES desk_agents(id),
    symbol        text NOT NULL,
    position      double precision NOT NULL DEFAULT 0,   -- target held, -1 to 1
    qty           double precision NOT NULL DEFAULT 0,   -- shares (signed)
    cash          double precision NOT NULL,
    decided_bar   timestamptz,                           -- the completed bar whose close produced the current decision
    pending       double precision,                      -- a decision waiting for its fill bar
    updated       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, symbol)
);
CREATE TABLE IF NOT EXISTS desk_agent_marks (
    agent_id      text REFERENCES desk_agents(id),
    at            timestamptz NOT NULL,
    equity        double precision NOT NULL,             -- sum over symbols, cash + qty * last chain close
    detail        jsonb,
    PRIMARY KEY (agent_id, at)
);
CREATE INDEX IF NOT EXISTS desk_record_strategy_idx ON desk_record (strategy, id);
