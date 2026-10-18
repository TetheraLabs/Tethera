-- Robinhood's own Stock Token registry (https://api.robinhood.com/rhj/assets) is the authority on which contract is
-- the real NVDA, SPY and so on; copies with the same ticker are not. Official tokens always win the per-symbol ranking.
ALTER TABLE tokens ADD COLUMN IF NOT EXISTS official BOOLEAN NOT NULL DEFAULT FALSE;
