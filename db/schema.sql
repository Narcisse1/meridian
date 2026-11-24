-- Meridian demo schema: operational data for "Lumio Pay", a fictional African payments company.
-- Column comments double as the data dictionary the analyst agent reads.

DROP TABLE IF EXISTS transactions, subscriptions, customers, regions CASCADE;

CREATE TABLE regions (
    id      serial PRIMARY KEY,
    code    text NOT NULL UNIQUE,
    name    text NOT NULL
);
COMMENT ON TABLE regions IS 'Sales regions. Every customer belongs to exactly one region.';
COMMENT ON COLUMN regions.code IS 'Short region code, e.g. WAF = West Africa, EUR = Europe.';

CREATE TABLE customers (
    id            serial PRIMARY KEY,
    region_id     int NOT NULL REFERENCES regions(id),
    signed_up_at  timestamptz NOT NULL,
    plan          text NOT NULL CHECK (plan IN ('starter', 'growth', 'scale')),
    channel       text NOT NULL CHECK (channel IN ('organic', 'paid_search', 'referral', 'partner'))
);
COMMENT ON TABLE customers IS 'Merchant accounts. One row per sign-up.';
COMMENT ON COLUMN customers.signed_up_at IS 'When the merchant created their account (a "sign-up").';
COMMENT ON COLUMN customers.channel IS 'Acquisition channel of the sign-up.';
CREATE INDEX ON customers (signed_up_at);
CREATE INDEX ON customers (region_id);

CREATE TABLE subscriptions (
    id             serial PRIMARY KEY,
    customer_id    int NOT NULL REFERENCES customers(id),
    plan           text NOT NULL CHECK (plan IN ('starter', 'growth', 'scale')),
    mrr            numeric(10, 2) NOT NULL,
    started_at     timestamptz NOT NULL,
    canceled_at    timestamptz,
    cancel_reason  text
);
COMMENT ON TABLE subscriptions IS 'Paid software subscriptions. A subscription is churned when canceled_at is set.';
COMMENT ON COLUMN subscriptions.mrr IS 'Monthly recurring revenue in USD.';
COMMENT ON COLUMN subscriptions.canceled_at IS 'Cancellation time; NULL while the subscription is active. Churn = cancellations.';
COMMENT ON COLUMN subscriptions.cancel_reason IS 'Reason given on cancellation: price_increase, switched_provider, business_closed, other.';
CREATE INDEX ON subscriptions (canceled_at);

CREATE TABLE transactions (
    id              bigserial PRIMARY KEY,
    customer_id     int NOT NULL REFERENCES customers(id),
    region_id       int NOT NULL REFERENCES regions(id),
    created_at      timestamptz NOT NULL,
    amount          numeric(12, 2) NOT NULL,
    currency        text NOT NULL DEFAULT 'USD',
    status          text NOT NULL CHECK (status IN ('succeeded', 'failed', 'refunded')),
    failure_reason  text,
    payment_method  text NOT NULL CHECK (payment_method IN ('card', 'mobile_money', 'bank_transfer'))
);
COMMENT ON TABLE transactions IS 'Payments processed for merchants. Revenue = sum(amount) of succeeded transactions.';
COMMENT ON COLUMN transactions.region_id IS 'Region of the merchant (same as the customer''s region).';
COMMENT ON COLUMN transactions.amount IS 'Payment amount in USD.';
COMMENT ON COLUMN transactions.status IS 'succeeded, failed or refunded.';
COMMENT ON COLUMN transactions.failure_reason IS 'Why a failed payment failed: insufficient_funds, card_declined, network_timeout, issuer_unavailable.';
CREATE INDEX ON transactions (created_at);
CREATE INDEX ON transactions (region_id, created_at);
CREATE INDEX ON transactions (status);
