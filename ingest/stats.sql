-- BalatroDB statistics: views and the canonical queries.
-- Layer 3.  Nothing here is materialized; see docs/db-schema.md for when that
-- should change.  Run after schema.sql.

-- Every statistic slices by deck, stake and endless-ness.  `el` is per event,
-- so the slice grain is (run, el) and one run can appear in both halves.
DROP VIEW IF EXISTS run_slices;
CREATE VIEW run_slices AS
SELECT DISTINCT e.run_id, e.el,
       r.deck_key, r.stake_key, r.seeded, r.challenge, r.won, r.result,
       r.terminal, r.defects
FROM   events e JOIN runs r USING (run_id);

-- The leaderboard population: unseeded, unchallenged, and free of the known
-- log defects.  Exclusion is a WHERE clause, never missing data.
DROP VIEW IF EXISTS runs_ranked;
CREATE VIEW runs_ranked AS
SELECT * FROM runs
WHERE  seeded = 0 AND challenge IS NULL
  AND  (defects & 63) = 0;          -- everything except no_run_end/encode_error

-- Max ante reached.  round.start.ante stops at the win ante (a won run's last
-- ante advance has no round.start), so the envelope `a` is the honest source
-- and it carries its own `el`.
DROP VIEW IF EXISTS run_ante;
CREATE VIEW run_ante AS
SELECT run_id, el, MAX(a) AS max_ante FROM events WHERE a IS NOT NULL GROUP BY run_id, el;

-- Peak balance.  money.change is exact (before + delta); snapshots and the
-- segment baselines cover the gaps at run start and after a resume.
DROP VIEW IF EXISTS run_money_peak;
CREATE VIEW run_money_peak AS
SELECT run_id, el, MAX(ord) AS peak_ord,
       (SELECT x.ex FROM (
            SELECT before_exact ex, before_ord od FROM money m2
             WHERE m2.run_id = z.run_id AND m2.el = z.el
            UNION ALL SELECT after_exact, after_ord FROM money m3
             WHERE m3.run_id = z.run_id AND m3.el = z.el
            UNION ALL SELECT dollars_exact, dollars_ord FROM snapshots s2
             WHERE s2.run_id = z.run_id AND s2.el = z.el
        ) x ORDER BY x.od DESC LIMIT 1) AS peak_exact
FROM (
    SELECT run_id, el, before_ord AS ord FROM money
    UNION ALL SELECT run_id, el, after_ord FROM money
    UNION ALL SELECT run_id, el, dollars_ord FROM snapshots
) z GROUP BY run_id, el;

-- Max deck size.  deck_samples.reported_size is the game's own count;
-- n_cards is the length of the sampled array.  They must agree.
DROP VIEW IF EXISTS run_deck_max;
CREATE VIEW run_deck_max AS
SELECT run_id, el, MAX(n_cards) AS max_deck_cards,
       MAX(COALESCE(reported_size, n_cards)) AS max_deck_reported
FROM deck_samples GROUP BY run_id, el;

-- Per-joker-instance maximum from joker.scale.  This is the primary source:
-- the sampled `jokers` array lags by one hand (see the doc).
DROP VIEW IF EXISTS joker_instance_max;
CREATE VIEW joker_instance_max AS
SELECT run_id, el, joker_key, field, joker_id,
       MAX(to_ord) AS peak_ord,
       MIN(to_ord) AS trough_ord
FROM   joker_scale WHERE kind = 'scale'
GROUP BY run_id, el, joker_key, field, joker_id;

-- Derived-joker values that never appear as a scale event or on the card.
-- One row per (run, el).  See the doc for why each source is the right one.
DROP VIEW IF EXISTS derived_joker_values;
CREATE VIEW derived_joker_values AS
-- Supernova: mult = G.GAME.hands[hand].played for the hand being scored, which
-- is not stored on the card.  Counting hand.play rows reproduces it exactly,
-- and because every el=0 event precedes every el=1 event, the el=0 count is
-- exactly the value standing at the endless boundary.
SELECT run_id, el, 'j_supernova' AS joker_key, 'mult' AS field,
       MAX(c) AS value
FROM  (SELECT run_id, el, hand_key, COUNT(*) AS c FROM hands GROUP BY run_id, el, hand_key)
GROUP BY run_id, el
UNION ALL
-- Fortune Teller: mult = tarots used.  card_set is load-bearing: consumable.use
-- also carries Jokers (from Buffoon packs) and Enhanced playing cards.
SELECT run_id, el, 'j_fortune_teller', 'mult', COUNT(*)
FROM   use_events WHERE kind = 'consumable.use' AND card_set = 'Tarot'
GROUP BY run_id, el
UNION ALL
-- Throwback: X mult = 1 + 0.25 * skips.
SELECT run_id, el, 'j_throwback', 'x_mult', 1 + 0.25 * COUNT(*)
FROM   blind_skips GROUP BY run_id, el
UNION ALL
-- Stone Joker: chips = 25 * stone cards in deck, from the deck sample.
SELECT run_id, el, 'j_stone', 'chips', 25 * MAX(n_stone)
FROM   deck_samples GROUP BY run_id, el
UNION ALL
-- Hiker: writes perma_bonus onto deck cards with no event at all, so the deck
-- sample is the only source.  perma_bonus only grows, so MAX over samples is
-- exact at the last sample; run.end also carries a deck, closing the tail gap.
SELECT run_id, el, 'j_hiker', 'perma_bonus_sum', MAX(perma_bonus_sum)
FROM   deck_samples GROUP BY run_id, el
UNION ALL
-- Swashbuckler is the one of the five that really is on the card.  Note the
-- shape: pick the row by val_ord, then read val_exact.  Never MAX(val_exact).
SELECT cs.run_id, cs.el, 'j_swashbuckler', 'mult',
       CAST((SELECT c2.val_exact FROM card_state c2
              WHERE c2.run_id = cs.run_id AND c2.el = cs.el
                AND c2.card_key = 'j_swashbuckler' AND c2.field = 'mult'
              ORDER BY c2.val_ord DESC LIMIT 1) AS REAL)
FROM   card_state cs
WHERE  cs.card_key = 'j_swashbuckler' AND cs.field = 'mult'
GROUP BY cs.run_id, cs.el;
