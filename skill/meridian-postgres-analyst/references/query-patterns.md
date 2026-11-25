# Query patterns for Meridian

Replace table and column names with the ones from `describe_table`. Each pattern is one
`SELECT`, so it passes the guard as is.

## Period over period

```sql
WITH bounds AS (SELECT max(created_at)::date AS d FROM orders)
SELECT
  round(sum(total) FILTER (WHERE created_at::date >  d - 7), 2)                               AS last_7d,
  round(sum(total) FILTER (WHERE created_at::date <= d - 7 AND created_at::date > d - 14), 2) AS prior_7d,
  round(100.0 * (sum(total) FILTER (WHERE created_at::date > d - 7)
        / nullif(sum(total) FILTER (WHERE created_at::date <= d - 7 AND created_at::date > d - 14), 0) - 1), 2)
                                                                                               AS change_pct
FROM orders, bounds
WHERE created_at::date > d - 14;
```

## Where is a change concentrated?

```sql
WITH bounds AS (SELECT max(created_at)::date AS d FROM orders)
SELECT c.country,
       count(*) FILTER (WHERE o.created_at::date >  d - 7)                              AS last_7d,
       count(*) FILTER (WHERE o.created_at::date <= d - 7 AND o.created_at::date > d - 14) AS prior_7d
FROM orders o JOIN customers c ON c.id = o.customer_id, bounds
WHERE o.created_at::date > d - 14
GROUP BY 1
ORDER BY abs(count(*) FILTER (WHERE o.created_at::date > d - 7)
           - count(*) FILTER (WHERE o.created_at::date <= d - 7 AND o.created_at::date > d - 14)) DESC
LIMIT 10;
```

## Daily trend (chart-ready)

```sql
SELECT date_trunc('day', created_at)::date AS day, count(*) AS orders, round(sum(total), 2) AS revenue
FROM orders
WHERE created_at >= CURRENT_DATE - 30
GROUP BY 1 ORDER BY 1;
```

## Rate with the same denominator

```sql
SELECT status_group, round(100.0 * n / sum(n) OVER (), 2) AS share_pct
FROM (SELECT CASE WHEN status = 'failed' THEN 'failed' ELSE 'other' END AS status_group, count(*) AS n
      FROM payments WHERE created_at >= CURRENT_DATE - 7 GROUP BY 1) t;
```

## Monthly signup cohorts and retention

```sql
WITH cohort AS (
  SELECT id, date_trunc('month', signed_up_at)::date AS cohort FROM customers
), activity AS (
  SELECT DISTINCT customer_id, date_trunc('month', created_at)::date AS month FROM orders
)
SELECT c.cohort,
       count(DISTINCT c.id) AS customers,
       count(DISTINCT a.customer_id) FILTER (WHERE a.month = c.cohort + interval '1 month') AS active_m1,
       count(DISTINCT a.customer_id) FILTER (WHERE a.month = c.cohort + interval '2 month') AS active_m2
FROM cohort c LEFT JOIN activity a ON a.customer_id = c.id
GROUP BY 1 ORDER BY 1 DESC LIMIT 12;
```

## Funnel

```sql
SELECT count(*)                                          AS signed_up,
       count(*) FILTER (WHERE first_order_at IS NOT NULL) AS ordered,
       round(100.0 * count(*) FILTER (WHERE first_order_at IS NOT NULL) / nullif(count(*), 0), 2) AS conversion_pct
FROM (SELECT c.id, min(o.created_at) AS first_order_at
      FROM customers c LEFT JOIN orders o ON o.customer_id = c.id
      WHERE c.signed_up_at >= CURRENT_DATE - 90 GROUP BY c.id) t;
```

## Is today unusual? (robust z-score against the last 8 weeks)

```sql
WITH daily AS (
  SELECT created_at::date AS day, count(*) AS n FROM orders
  WHERE created_at >= CURRENT_DATE - 57 GROUP BY 1
), stats AS (
  SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY n) AS median FROM daily WHERE day < CURRENT_DATE
), mad AS (
  SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(n - median)) AS mad FROM daily, stats WHERE day < CURRENT_DATE
)
SELECT d.day, d.n, s.median, round(((d.n - s.median) / nullif(1.4826 * m.mad, 0))::numeric, 2) AS robust_z
FROM daily d, stats s, mad m
ORDER BY d.day DESC LIMIT 7;
```

A |robust_z| above about 3 is worth investigating; break it down by dimension next.
