-- Catherine's debit card: non-secret facts only. The card number itself is
-- never stored in this database: it is encrypted in a separate vault file
-- (CARD_VAULT_FILE) with its own key. Purchase requests are card_purchase
-- rows in the actions table, so they share approvals and the audit log.

CREATE TABLE IF NOT EXISTS payment_cards (
    id TEXT PRIMARY KEY,

    -- The agent the card was given to (catherine).
    holder_agent_id TEXT NOT NULL,

    brand TEXT NOT NULL,
    last4 TEXT NOT NULL
        CHECK (length(last4) = 4 AND last4 NOT GLOB '*[^0-9]*'),

    exp_month INTEGER NOT NULL
        CHECK (exp_month BETWEEN 1 AND 12),
    exp_year INTEGER NOT NULL
        CHECK (exp_year BETWEEN 2000 AND 2100),

    status TEXT NOT NULL DEFAULT 'active'
        CHECK (
            status IN (
                'active',
                'frozen',
                'removed'
            )
        ),

    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- At most one card in use (active or frozen) per holder.
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_cards_one_per_holder
    ON payment_cards(holder_agent_id)
    WHERE status != 'removed';

CREATE INDEX IF NOT EXISTS idx_actions_type_created
    ON actions(action_type, created_at);
