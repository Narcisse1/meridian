-- Deterministic demo data: 120 days ending yesterday, with trend, weekly seasonality and noise.
-- Planted anomalies (so the digest has something real to find):
--   * West Africa mobile-money payments fail far more often in the last 6 days (issuer_unavailable)
--   * Sign-ups jump ~70% on the latest day (campaign)
--   * Growth-plan cancellations rise in the last 10 days (price_increase)
-- Re-run any time:  psql "$DATABASE_URL" -f db/schema.sql -f db/seed.sql

SELECT setseed(0.42);

INSERT INTO regions (code, name) VALUES
  ('WAF', 'West Africa'), ('EAF', 'East Africa'), ('SAF', 'Southern Africa'),
  ('NAF', 'North Africa'), ('EUR', 'Europe'), ('NAM', 'North America');

-- Customers ---------------------------------------------------------------------
INSERT INTO customers (region_id, signed_up_at, plan, channel)
SELECT
  (SELECT id FROM regions WHERE code = CASE
      WHEN r.u < 0.30 THEN 'WAF' WHEN r.u < 0.48 THEN 'EAF' WHEN r.u < 0.62 THEN 'SAF'
      WHEN r.u < 0.72 THEN 'NAF' WHEN r.u < 0.88 THEN 'EUR' ELSE 'NAM' END),
  c.day + make_interval(secs => r.t * 86399),
  CASE WHEN r.p < 0.62 THEN 'starter' WHEN r.p < 0.92 THEN 'growth' ELSE 'scale' END,
  CASE WHEN r.ch < 0.40 THEN 'organic' WHEN r.ch < 0.70 THEN 'paid_search'
       WHEN r.ch < 0.88 THEN 'referral' ELSE 'partner' END
FROM (
  SELECT d.day, d.i,
         greatest(5, round(46 * (1 + d.i / 240.0)
           * CASE extract(isodow FROM d.day) WHEN 6 THEN 0.72 WHEN 7 THEN 0.6 ELSE 1 END
           * (0.85 + random() * 0.3)
           * CASE WHEN d.day = current_date - 1 THEN 1.7 ELSE 1 END))::int AS n
  FROM (
    SELECT g::date AS day, row_number() OVER (ORDER BY g) AS i
    FROM generate_series(current_date - 120, current_date - 1, interval '1 day') g
  ) d
) c
CROSS JOIN LATERAL generate_series(1, c.n) k
CROSS JOIN LATERAL (SELECT random() + k * 0 AS u, random() + k * 0 AS t,
                           random() + k * 0 AS p, random() + k * 0 AS ch) r
ORDER BY 2;

-- Subscriptions: ~70% of customers subscribe at sign-up ----------------------------
INSERT INTO subscriptions (customer_id, plan, mrr, started_at, canceled_at, cancel_reason)
SELECT c.id, c.plan,
       CASE c.plan WHEN 'starter' THEN 29 WHEN 'growth' THEN 99 ELSE 399 END,
       c.signed_up_at,
       CASE WHEN s.churn THEN c.signed_up_at + (s.when_frac * (now() - c.signed_up_at)) END,
       CASE WHEN s.churn THEN CASE WHEN s.reason < 0.45 THEN 'switched_provider'
                                   WHEN s.reason < 0.75 THEN 'business_closed' ELSE 'other' END END
FROM customers c
CROSS JOIN LATERAL (SELECT random() + c.id * 0 < 0.70 AS subscribed,
                           random() + c.id * 0 < 0.16 AS churn,
                           random() + c.id * 0 AS when_frac,
                           random() + c.id * 0 AS reason) s
WHERE s.subscribed;

-- Planted churn spike: growth-plan cancellations after a price change 10 days ago
UPDATE subscriptions
SET canceled_at = (current_date - 10) + make_interval(secs => random() * 9 * 86400),
    cancel_reason = 'price_increase'
WHERE plan = 'growth' AND canceled_at IS NULL
  AND started_at < current_date - 30
  AND random() < 0.09;

-- Transactions --------------------------------------------------------------------
-- Customers are inserted in sign-up order, so ids 1..N(day) are the merchants live on that day.
WITH days AS (
  SELECT g::date AS day, row_number() OVER (ORDER BY g) AS i
  FROM generate_series(current_date - 120, current_date - 1, interval '1 day') g
),
live AS (
  SELECT d.day, d.i, (SELECT count(*) FROM customers WHERE signed_up_at < d.day + 1) AS n_live
  FROM days d
),
slots AS (
  SELECT l.day, l.n_live, k
  FROM live l
  CROSS JOIN LATERAL generate_series(1, greatest(50, round(
      9 * sqrt(l.n_live) * 2.4
      * CASE extract(isodow FROM l.day) WHEN 6 THEN 0.82 WHEN 7 THEN 0.7 ELSE 1 END
      * (0.92 + random() * 0.16))::int)) k
  WHERE l.n_live > 0
),
draws AS (
  SELECT s.day,
         1 + floor(random() * s.n_live)::int AS customer_id,
         random() AS t, random() AS a, random() AS m, random() AS f, random() AS fr
  FROM slots s
),
enriched AS (
  SELECT d.*, c.region_id, r.code AS region_code,
         CASE
           WHEN r.code IN ('WAF', 'EAF') AND d.m < 0.55 THEN 'mobile_money'
           WHEN d.m < 0.12 THEN 'mobile_money'
           WHEN d.m < 0.85 THEN 'card'
           ELSE 'bank_transfer'
         END AS payment_method
  FROM draws d
  JOIN customers c ON c.id = d.customer_id
  JOIN regions r ON r.id = c.region_id
)
INSERT INTO transactions (customer_id, region_id, created_at, amount, status, failure_reason, payment_method)
SELECT
  customer_id, region_id,
  day + make_interval(secs => t * 86399),
  round(exp(ln(12) + a * 2.6)::numeric, 2),
  CASE
    WHEN region_code = 'WAF' AND payment_method = 'mobile_money' AND day > current_date - 7 AND f < 0.24 THEN 'failed'
    WHEN f < 0.034 THEN 'failed'
    WHEN f < 0.045 THEN 'refunded'
    ELSE 'succeeded'
  END,
  CASE
    WHEN region_code = 'WAF' AND payment_method = 'mobile_money' AND day > current_date - 7 AND f < 0.24
         AND f >= 0.034 THEN 'issuer_unavailable'
    WHEN f < 0.034 THEN CASE WHEN fr < 0.45 THEN 'insufficient_funds' WHEN fr < 0.75 THEN 'card_declined'
                             WHEN fr < 0.92 THEN 'network_timeout' ELSE 'issuer_unavailable' END
  END,
  payment_method
FROM enriched;

ANALYZE regions; ANALYZE customers; ANALYZE subscriptions; ANALYZE transactions;
