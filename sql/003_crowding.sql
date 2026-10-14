-- The first crowding query: per pool, per 60-second bucket, per direction,
-- how many distinct automated wallets swapped the same way. Top 20 buckets.
SELECT
    pool,
    date_trunc('minute', block_time)           AS minute_utc,
    direction,
    count(DISTINCT tx_from) FILTER (WHERE is_automated) AS automated_wallets,
    count(*)                FILTER (WHERE is_automated) AS automated_swaps,
    count(DISTINCT tx_from)                             AS all_wallets,
    count(*)                                            AS all_swaps,
    min(block_number)                                   AS first_block,
    max(block_number)                                   AS last_block
FROM swaps
GROUP BY pool, date_trunc('minute', block_time), direction
ORDER BY automated_wallets DESC, automated_swaps DESC
LIMIT 20;
