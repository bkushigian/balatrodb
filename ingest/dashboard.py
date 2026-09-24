"""A local web dashboard over the BalatroDB database.

    python ingest/dashboard.py            # http://localhost:8611
    python ingest/dashboard.py --port 9000 --no-open

Serves `web/index.html` plus a small read-only JSON API. It queries the SQLite
database directly, so it is always as current as the last ingest; run
`python ingest/ingest.py` to pick up new runs.

The queries here are the corrected ones from docs/db-schema.md -- in particular
per-joker maxima come from a single consistently filtered relation (an earlier
version's correlated subqueries returned provenance from the wrong deck), and
money peaks come from the running balance rather than `before + delta`.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import zlib
import sqlite3
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import ingest as ingester

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "balatro.db")
WEB = os.path.join(HERE, "web")


def connect():
    """Open the database, rebuilding it first if schema.sql has moved on.

    This is the path players actually use -- the button inside Balatro starts
    this server, not ingest.py -- so the drift guard has to live here too.
    Without it, editing schema.sql surfaced as a per-panel {"error": "no such
    column"} string, which says nothing about the cause.
    """
    ddl = open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read()
    want = zlib.crc32(ddl.encode("utf-8")) & 0x7FFFFFFF
    if os.path.exists(DB):
        probe = sqlite3.connect(DB)
        stale = probe.execute("PRAGMA user_version").fetchone()[0] != want
        probe.close()
        if stale:
            print("schema changed since this database was built; rebuilding")
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(DB + suffix):
                    os.remove(DB + suffix)
    db = sqlite3.connect(DB, check_same_thread=False)
    db.row_factory = sqlite3.Row
    # Creates the tables on a first run, and is a no-op otherwise.
    db.executescript(ddl)
    db.execute(f"PRAGMA user_version = {want}")
    db.commit()
    return db


def rows(db, sql, params=()):
    return [dict(r) for r in db.execute(sql, params).fetchall()]


# ─── keeping current ──────────────────────────────────────────────────────
# Finishing a run in Balatro should update the dashboard, with no manual step
# and without waiting for a page load. A background thread watches the log
# directory and folds in whatever changed; the page polls `generation` and
# re-renders when it moves.
#
# The ingester only stats each log and skips unchanged ones, so a scan of a few
# hundred runs is a few milliseconds. The lock serializes writes against the
# request threads reading through the same connection.

_sync_lock = threading.Lock()
generation = 0          # bumped whenever a sync changed something
last_sync = 0.0


def sync(db, logs):
    """Fold any changed logs in. Returns how many runs were re-derived."""
    global generation, last_sync
    with _sync_lock:
        ing = ingester.Ingester(db)
        changed = 0
        for path in sorted(glob.glob(os.path.join(logs, "*.jsonl")) +
                           glob.glob(os.path.join(logs, "*.jsonl.gz"))):
            try:
                _, did = ing.ingest_file(path)
                changed += 1 if did else 0
            except Exception as ex:                  # one unreadable log must
                print(f"  sync: {os.path.basename(path)}: {ex}")  # not stop the rest
        if changed:
            ing.derive_counters()
            ing.derive_records()
            ing.derive_abandoned()
            db.commit()
            generation += 1
            print(f"  synced {changed} run(s) -> generation {generation}")
        last_sync = time.time()
        return changed


def watch(db, logs, every=3.0):
    """Poll the log directory forever. A live run's file grows as you play, so
    an in-progress run appears and updates rather than waiting for the end."""
    while True:
        try:
            sync(db, logs)
        except Exception as ex:
            print(f"  watch: {ex}")
        time.sleep(every)


# ─── filters ──────────────────────────────────────────────────────────────
# Every query slices the same way: deck, stake, and endless phase.
#
# `endless` is absent when both phases are wanted -- the page sends it only
# when exactly one toggle is on. For leaderboards the flag is per EVENT, so
# "non-endless" still includes the pre-win portion of a run that later went
# endless, which is the whole reason the flag is stamped per event. For the run
# LIST it is necessarily per run (did this run ever go endless), which is a
# different question with the same name.

def where(q, prefix="r.", endless_col=None):
    clauses, params = [], []
    if q.get("deck"):
        clauses.append(f"{prefix}deck_key = ?")
        params.append(q["deck"])
    if q.get("stake"):
        clauses.append(f"{prefix}stake_key = ?")
        params.append(q["stake"])
    if q.get("endless") in ("0", "1") and endless_col:
        clauses.append(f"{endless_col} = ?")
        params.append(int(q["endless"]))
    # Seeded runs are practice, not records -- you chose the seed. Kept as
    # its own filter rather than folded into the deck/stake ones, since
    # excluding them is a different question from picking what to look at.
    if q.get("seeded") in ("0", "1"):
        clauses.append(f"{prefix}seeded = ?")
        params.append(int(q["seeded"]))
    # Plasma balances chips and mult before scoring, so its numbers are not
    # comparable with any other deck's -- one Plasma run owns most of the
    # score records. Excluding it is a different question from picking a
    # deck, so it is its own filter.
    if q.get("noplasma") == "1":
        clauses.append(f"{prefix}deck_key IS NOT 'b_plasma'")
    return (" AND " + " AND ".join(clauses) if clauses else ""), params


def one(q, key):
    v = q.get(key)
    return v[0] if isinstance(v, list) and v else None


def qdict(query):
    return {k: v[0] for k, v in parse_qs(query).items() if v and v[0] != ""}


# ─── endpoints ────────────────────────────────────────────────────────────
def api_meta(db, q):
    return {
        "decks": rows(db, "SELECT DISTINCT deck_key k, deck_name n FROM runs "
                          "WHERE deck_key IS NOT NULL ORDER BY n"),
        "stakes": rows(db, "SELECT DISTINCT stake_key k FROM runs "
                           "WHERE stake_key IS NOT NULL ORDER BY stake"),
        "defects": rows(db, "SELECT defect, COUNT(*) n FROM run_defects "
                            "GROUP BY defect ORDER BY n DESC"),
    }


def api_summary(db, q):
    # The run counts are run-level facts, so the phase toggle applies to them
    # the same way it does in api_runs: whether the run went endless at all.
    # Without this the header claimed "15 runs" over a slice holding one.
    w, p = where(q)
    if q.get("endless") in ("0", "1"):
        w += " AND r.went_endless = ?"
        p = p + [int(q["endless"])]

    total = db.execute(f"SELECT COUNT(*) FROM runs r WHERE 1=1{w}", p).fetchone()[0]
    # Numerator and denominator must be the same population, and it must be
    # the one the adjacent "N won" label counts. A run is DECIDED once it has
    # either won or ended; a run that won and was then suspended is decided
    # and won, so counting it in one and not the other produced "4 won" beside
    # an 18% rate that was really 2/11.
    won = db.execute(
        f"SELECT COUNT(*) FROM runs r WHERE r.won=1{w}", p).fetchone()[0]
    decided = db.execute(
        f"SELECT COUNT(*) FROM runs r WHERE (r.terminal=1 OR r.won=1){w}", p).fetchone()[0]

    ew, ep = where(q, endless_col="h.endless")
    best = db.execute(
        f"""SELECT h.score_txt v, h.hand, r.run_id, r.deck_name FROM hands h
             JOIN runs r USING (run_id) WHERE h.score_ord IS NOT NULL{ew}
            ORDER BY h.score_ord DESC, CAST(h.score_txt AS REAL) DESC LIMIT 1""", ep).fetchone()

    mw, mp = where(q, endless_col="m.endless")
    money = db.execute(
        f"""SELECT MAX(m.balance) v FROM money m JOIN runs r USING (run_id)
            WHERE 1=1{mw}""", mp).fetchone()

    dw, dp = where(q, endless_col="ro.endless")
    deck = db.execute(
        f"""SELECT MAX(ro.deck_size) v FROM rounds ro JOIN runs r USING (run_id)
            WHERE 1=1{dw}""", dp).fetchone()
    # Per-RUN, not per-round: `rounds.ante` goes backwards when Hieroglyph
    # or Petroglyph calls ease_ante(-n), so its maximum is not the furthest
    # ante reached. `runs.furthest_ante` is the game's own high-water mark,
    # and is what the Runs column and the run dialog already show -- this
    # tile was the one surface deriving it differently, and with the
    # non-endless filter on it printed 8 above a column reaching 9.
    ante = db.execute(
        f"""SELECT MAX(COALESCE(r.furthest_ante, r.ended_ante)) v
              FROM runs r WHERE 1=1{w}""", p).fetchone()
    cash = db.execute(
        f"""SELECT MAX(ro.cashout_total) v FROM rounds ro JOIN runs r USING (run_id)
            WHERE 1=1{dw}""", dp).fetchone()

    return {
        "runs": total,
        "won": won,
        "decided": decided,
        "win_pct": round(100.0 * won / decided, 1) if decided else None,
        "best_hand": best["v"] if best else None,
        "best_hand_name": best["hand"] if best else None,
        "best_hand_deck": best["deck_name"] if best else None,
        "max_money": money["v"] if money else None,
        "max_deck": deck["v"] if deck else None,
        "max_ante": ante["v"] if ante else None,
        "max_cashout": cash["v"] if cash else None,
    }


# Sorting the run list by something that is not one of its columns: a
# joker's peak in that run, or one of a hand's statistics. Each entry is the
# subquery that produces the value and the one that produces its ordering
# key -- kept apart because scores exceed a double and only the ordering key
# is safe to sort on.
METRICS = {
    "joker": (
        "(SELECT to_txt FROM joker_scale js WHERE js.run_id = r.run_id"
        "   AND js.key = ? AND js.is_reset = 0{el} ORDER BY js.to_ord DESC LIMIT 1)",
        "(SELECT MAX(js.to_ord) FROM joker_scale js WHERE js.run_id = r.run_id"
        "   AND js.key = ? AND js.is_reset = 0{el})",
        " AND js.endless = ?"),
    "hand_score": (
        "(SELECT score_txt FROM hands h WHERE h.run_id = r.run_id"
        "   AND h.hand = ?{el} ORDER BY h.score_ord DESC LIMIT 1)",
        "(SELECT MAX(h.score_ord) FROM hands h WHERE h.run_id = r.run_id"
        "   AND h.hand = ?{el})",
        " AND h.endless = ?"),
    "hand_level": (
        "(SELECT MAX(hl.lvl_to) FROM hand_levels hl WHERE hl.run_id = r.run_id"
        "   AND hl.hand = ?{el})",
        "(SELECT MAX(hl.lvl_to) FROM hand_levels hl WHERE hl.run_id = r.run_id"
        "   AND hl.hand = ?{el})",
        " AND hl.endless = ?"),
    "hand_played": (
        "(SELECT COUNT(*) FROM hands h WHERE h.run_id = r.run_id"
        "   AND h.hand = ?{el})",
        "(SELECT COUNT(*) FROM hands h WHERE h.run_id = r.run_id"
        "   AND h.hand = ?{el})",
        " AND h.endless = ?"),
    # A counter joker has no joker_scale rows at all -- its value is a
    # function of game state -- so it could not be picked here, and Bull was
    # missing from every per-joker view. The contribution is what is
    # comparable between runs, and ingest has already converted it.
    "counter": (
        "(SELECT MAX(cp.contributed_value) FROM joker_counter_peaks cp"
        "   WHERE cp.run_id = r.run_id AND cp.joker_key = ?{el})",
        "(SELECT MAX(cp.contributed_ord) FROM joker_counter_peaks cp"
        "   WHERE cp.run_id = r.run_id AND cp.joker_key = ?{el})",
        " AND cp.endless = ?"),
    # The other half of the same joker: what the counter reached whether or
    # not anyone held it. A Fortune Teller record set without ever owning
    # one is this, and for most runs it is the only one that exists.
    # The counter at its best while the joker was NOT in your hands.
    "counter_unheld": (
        "(SELECT MAX(cp.unheld_value) FROM joker_counter_peaks cp"
        "   WHERE cp.run_id = r.run_id AND cp.joker_key = ?{el})",
        "(SELECT MAX(cp.unheld_ord) FROM joker_counter_peaks cp"
        "   WHERE cp.run_id = r.run_id AND cp.joker_key = ?{el})",
        " AND cp.endless = ?"),
    "counter_ambient": (
        "(SELECT MAX(cp.ambient_value) FROM joker_counter_peaks cp"
        "   WHERE cp.run_id = r.run_id AND cp.joker_key = ?{el})",
        "(SELECT MAX(cp.ambient_ord) FROM joker_counter_peaks cp"
        "   WHERE cp.run_id = r.run_id AND cp.joker_key = ?{el})",
        " AND cp.endless = ?"),
}


def api_metrics(db, q):
    """What the run list can be sorted by beyond its own columns."""
    w, p = where(q)
    jokers = rows(db, f"""
        SELECT DISTINCT js.key k FROM joker_scale js JOIN runs r USING (run_id)
         WHERE js.is_reset = 0
           AND js.key NOT IN {ingester.DECAYING_SQL}
           {w} ORDER BY k""", p)
    hands = rows(db, f"""
        SELECT DISTINCT h.hand k FROM hands h JOIN runs r USING (run_id)
         WHERE h.hand IS NOT NULL{w} ORDER BY k""", p)
    # Counter jokers live in their own table and carry a metric name rather
    # than a joker key, so they are listed apart with the key to draw.
    counters = rows(db, f"""
        SELECT DISTINCT cp.joker_key k FROM joker_counter_peaks cp
          JOIN runs r USING (run_id)
         WHERE cp.contributed_value IS NOT NULL{w} ORDER BY k""", p)
    return {"jokers": [j["k"] for j in jokers], "hands": [h["k"] for h in hands],
            "counters": [{"key": c["k"],
                          "metric": ingester.COUNTER_JOKERS[c["k"]][0],
                          # What it contributes, so a reader can colour it
                          # the way the game does -- blue chips, red mult.
                          "field": ingester.COUNTER_JOKERS[c["k"]][1]}
                         for c in counters if c["k"] in ingester.COUNTER_JOKERS]}


def api_runs(db, q):
    w, p = where(q)
    sort = {
        "recent": "r.started_ts DESC",
        # Sort by the same thing the column displays. best_hand_ord is only
        # set on terminal runs, so sorting by it buried a 221,539 hand beneath
        # runs showing 348.
        "score": "(SELECT MAX(score_ord) FROM hands h WHERE h.run_id = r.run_id) DESC",
        "ante": "COALESCE(r.furthest_ante, r.ended_ante) DESC",
        "money": "r.final_dollars DESC",
        "hands": "r.hands_played DESC",
    }.get(q.get("sort"), "r.started_ts DESC")
    # The phase filter applies twice, differently: to which runs are listed
    # (a run-level flag) and to the per-run figures derived from events. The
    # subquery params bind BEFORE the outer ones, since they appear first.
    hw = mw = rw = ""
    sub, tail = [], []
    if q.get("endless") in ("0", "1"):
        el = int(q["endless"])
        # `scope=event` keeps the per-EVENT filter and drops the per-run one,
        # so every run reports what it did in that phase instead of dropping
        # out of the list entirely. A chart needs this: to show a run's
        # standard best beside its overall best, the run has to appear in
        # both answers. The run LIST still filters both ways by default,
        # which is the question that page is asking.
        if q.get("scope") != "event":
            w += " AND r.went_endless = ?"
            p = p + [el]
        hw, mw = " AND h.endless = ?", " AND m.endless = ?"
        rw = " AND ro.endless = ?"
        sub = [el, el, el, el]       # bh_ord, best_hand, peak_money, rounds_won
        # Sorting by score has to see the same slice as the column it sorts.
        # Its placeholder is in the ORDER BY, so it binds last of all.
        if q.get("sort") == "score":
            sort = ("(SELECT MAX(score_ord) FROM hands h "
                    f"WHERE h.run_id = r.run_id{hw}) DESC")
            tail = [el]
    # A metric sort replaces the column sort entirely -- the two are
    # alternatives, never combined.
    metric_txt = metric_ord = "NULL"
    metric = (q.get("metric") or "").split(":", 1)
    if len(metric) == 2 and metric[0] in METRICS:
        val_sql, ord_sql, el_clause = METRICS[metric[0]]
        el = el_clause if q.get("endless") in ("0", "1") else ""
        metric_txt = val_sql.format(el=el)
        metric_ord = ord_sql.format(el=el)
        args = [metric[1]] + ([int(q["endless"])] if el else [])
        sub = args + args + sub          # both appear before the outer WHERE
        sort = "metric_ord IS NULL, metric_ord DESC"

    out = rows(db, f"""
        SELECT {metric_txt} metric_txt, {metric_ord} metric_ord,
               r.run_id, r.log_file, r.started_ts, r.deck_name, r.deck_key,
               r.stake_key, r.seed, r.seeded, r.won, r.result, r.terminal,
               r.abandoned,
               r.went_endless, r.hands_played, r.final_dollars, r.deck_size,
               COALESCE(r.furthest_ante, r.ended_ante) ante,
               -- Sliced by the phase filter like every other derived figure.
               -- Left unsliced, a row showed a 221,539 best hand next to a
               -- "Best hand" panel reporting 13,104 for the same selection.
               (SELECT MAX(score_ord) FROM hands h
                 WHERE h.run_id = r.run_id{hw}) bh_ord,
               (SELECT score_txt FROM hands h
                 WHERE h.run_id = r.run_id{hw}
                 ORDER BY score_ord DESC LIMIT 1) best_hand,
               (SELECT MAX(balance) FROM money m
                 WHERE m.run_id = r.run_id{mw}) peak_money,
               -- A round only gets a cash-out when its blind was beaten, so
               -- this counts blinds cleared rather than blinds faced.
               (SELECT COUNT(*) FROM rounds ro
                 WHERE ro.run_id = r.run_id AND ro.cashout_total IS NOT NULL{rw})
                 rounds_won,
               (SELECT COUNT(*) FROM run_defects d WHERE d.run_id = r.run_id) defects
          FROM runs r WHERE 1=1{w} ORDER BY {sort} LIMIT 300""", sub + p + tail)
    attach_records(db, out, q)
    return out


# What to lead with when a run set several records. Ordering across kinds
# cannot use the values -- a hand level of 32 and a score's log-scale ord are
# not comparable -- so it is a fixed priority, biggest-first within each.
RECORD_ORDER = {"joker": 0, "hand_score": 1, "hand_level": 2, "hand_played": 3}


def endless_union(db):
    """Which stored endless records are records with the restriction lifted.

    The two contests are stored disjoint: non-endless rows come from events
    before the win ante, endless rows from after. That answers "what did I
    do after the win, on its own terms", and it is worth keeping.

    But the contest a player means by "endless" is the one with the
    restriction LIFTED -- anything managed before the win also counts,
    because endless only removes a limit. Derived in isolation the endless
    contest announces records that never were: a Pair of 228 is stored as an
    endless record although the standard record was six figures by then.

    That contest can be read off the two stored ones without deriving
    anything new. Every record in the union is already a record in whichever
    contest it came from -- if a run's best is its non-endless value, that
    value beat every earlier value including every earlier non-endless one,
    so the run already holds a non-endless row carrying exactly it; likewise
    for endless. So merging the stored rows in run order and keeping those
    that beat the running best gives the union exactly.

    Returns the endless rows that survive, as
    {(run_id, kind, subject): (prev_txt, prev_run)} -- with `prev` recomputed
    against the union, so "beat X" names what was really standing.
    """
    per_subject = {}
    for r in db.execute("""SELECT rr.run_id, rr.kind, rr.subject, rr.endless,
                                  rr.value_txt, rr.value_ord
                             FROM run_records rr JOIN runs u USING (run_id)
                            ORDER BY u.started_ts, rr.run_id"""):
        per_subject.setdefault((r["kind"], r["subject"]), []).append(r)

    keep = {}
    for (kind, subject), recs in per_subject.items():
        # A run is one competitor, so its two stored rows compete as one:
        # the better of them is what it did with the restriction lifted.
        best = {}
        for r in recs:
            cur = best.get(r["run_id"])
            if cur is None or (r["value_ord"] or 0) > (cur["value_ord"] or 0):
                best[r["run_id"]] = r

        top, prev_txt, prev_run = None, None, None
        for run_id in dict.fromkeys(r["run_id"] for r in recs):
            r = best[run_id]
            if top is not None and (r["value_ord"] or 0) <= top:
                continue
            if r["endless"]:
                keep[(run_id, kind, subject)] = (prev_txt, prev_run)
            top = r["value_ord"] or 0
            prev_txt, prev_run = r["value_txt"], run_id
    return keep


def attach_records(db, runs, q=None):
    """Give each run what it was the first to achieve, at the time it ran.

    Endless and non-endless are separate contests, so the phase toggle picks
    between them; with no phase filter both are returned, and a run may hold
    one of each for the same subject.

    An endless row is shown only when it is also a record with the
    restriction lifted -- see endless_union. Otherwise a run announces an
    "endless record" that a standard run had already beaten, which is the
    one thing the endless flag should never do.
    """
    for r in runs:
        r["records"] = []
    by_id = {r["run_id"]: r for r in runs}
    if not by_id:
        return
    marks = ",".join("?" * len(by_id))
    params = list(by_id)
    union = endless_union(db)
    # The run a recomputed `prev` points at is any run, not only a listed
    # one, so its label comes from the whole table.
    prev_ts, prev_deck = {}, {}
    for u in db.execute("SELECT run_id, started_ts, deck_name FROM runs"):
        prev_ts[u["run_id"]] = u["started_ts"]
        prev_deck[u["run_id"]] = u["deck_name"]
    ew = ""
    if (q or {}).get("endless") in ("0", "1"):
        ew = " AND rr.endless = ?"
        params.append(int(q["endless"]))
    for rec in rows(db, f"""
            SELECT rr.run_id, rr.kind, rr.subject, rr.endless, rr.field, rr.held,
                   rr.value_txt, rr.value_ord, rr.prev_txt,
                   pr.started_ts prev_ts, pr.deck_name prev_deck
              FROM run_records rr
              LEFT JOIN runs pr ON pr.run_id = rr.prev_run
             WHERE rr.run_id IN ({marks}){ew}""", params):
        if rec["endless"]:
            hit = union.get((rec["run_id"], rec["kind"], rec["subject"]))
            if hit is None:
                continue                       # beaten before it was set
            rec = dict(rec)
            rec["prev_txt"], prev_run = hit
            rec["prev_ts"] = prev_ts.get(prev_run)
            rec["prev_deck"] = prev_deck.get(prev_run)
        by_id[rec["run_id"]]["records"].append(rec)
    for r in runs:
        # Endless records after non-endless ones of the same kind: the
        # ordering keys are not comparable across the two contests.
        r["records"].sort(key=lambda x: (RECORD_ORDER.get(x["kind"], 9),
                                         x["endless"], -(x["value_ord"] or 0)))


def api_all_jokers(db, q):
    """Every joker's peak value, however that value is stored.

    Scaling jokers keep it in their own ability; the four counter jokers read
    a game counter instead and are reconstructed at ingest. That split is an
    implementation detail of Balatro, not something worth making a reader
    care about, so the two are merged into one leaderboard here.
    """
    out = []
    for j in api_jokers(db, q):
        # The ability field a joker scales in ("chips", "mult") is how the
        # game stores it, not something a reader wants on the row.
        out.append({"key": j["key"], "value": j["value"], "ord": j["ord"],
                    "what": None, "field": j["field"], "run_id": j["run_id"],
                    "deck_name": j["deck_name"], "deck_key": j["deck_key"],
                    "stake_key": j["stake_key"]})
    for d in api_derived(db, q):
        # `what` stays off the row -- the board is the joker, not the
        # counter behind it. It goes in the hover instead.
        out.append({"key": d["joker_key"], "value": d["value"],
                    "ord": ord_of(d["value"]), "what": None,
                    "field": d["field"],
                    "counter": f'{d["counter"]:g} {d["what"]}',
                    "held": d["held"], "run_id": d["run_id"], "deck_name": d["deck_name"],
                    "deck_key": d["deck_key"], "stake_key": d["stake_key"]})
    out.sort(key=lambda r: -(r["ord"] or 0))
    return out


def ord_of(v):
    """The ingester's ordering key, not a second copy of it.

    These rows get sorted against `to_ord`/`score_ord` values written by the
    ingester, so the two must agree exactly -- which a reimplementation here
    cannot promise. It also differs on input handling: this returned a key
    for the string "123" where ord_num declines it.
    """
    return ingester.ord_num(v)[0]


def api_jokers(db, q):
    """Per-joker maxima.

    One consistently filtered relation, then ROW_NUMBER over it. The earlier
    version used correlated subqueries that omitted the deck/stake filters, so
    the displayed value and its provenance could come from a different slice
    than the aggregate.
    """
    w, p = where(q, endless_col="js.endless")
    return rows(db, f"""
        WITH eligible AS (
          SELECT js.key, js.field, js.to_txt, js.to_ord, js.card_id,
                 r.run_id, r.deck_name, r.deck_key, r.stake_key
            FROM joker_scale js JOIN runs r USING (run_id)
           WHERE js.is_reset = 0 AND js.to_ord IS NOT NULL
             -- Decaying jokers scale DOWNWARD, so MAX() over their
             -- observations is the highest value seen after decay began, not
             -- a peak. Ranking them beside Wee Joker's earned 2,080 is
             -- meaningless, so they are left off the board entirely.
             AND js.key NOT IN {ingester.DECAYING_SQL}{w}),
        ranked AS (
          SELECT *, ROW_NUMBER() OVER (
                      PARTITION BY key, field
                      ORDER BY to_ord DESC, CAST(to_txt AS REAL) DESC) rn
            FROM eligible)
        SELECT key, field, to_txt value, to_ord ord, run_id, deck_name,
               deck_key, stake_key
          FROM ranked WHERE rn = 1 ORDER BY to_ord DESC""", p)


# The jokers whose value IS a game counter. Their peak is the peak of that
# counter, so the leaderboard row is the joker, not the counter.
# metric -> (joker key, name, what the counter counts, field, per-unit rate)
#
# The board shows what the joker CONTRIBUTES, not the raw counter: a player
# watching Bull sees +378 Chips, not "189 dollars". Rates are the game's own
# `extra` values (game.lua) and the field is the one its text adds to.
# Display names for the counter jokers. The mechanical part -- key, field
# and per-unit rate -- comes from ingest, which needs it to turn a counter
# into a score, so the two cannot drift.
# How to say each one, keyed the same way the ingester keys them: by joker.
# Two of these read the same counter, so the counter cannot be the key.
COUNTER_LABEL = {
    "j_supernova":      ("Supernova",      "plays of this hand"),
    "j_throwback":      ("Throwback",      "blinds skipped"),
    "j_fortune_teller": ("Fortune Teller", "tarots used"),
    "j_stone":          ("Stone Joker",    "stone cards in deck"),
    "j_bull":           ("Bull",           "dollars held"),
    "j_bootstraps":     ("Bootstraps",     "dollars held"),
}


def api_derived(db, q):
    """The jokers whose value is a function of GAME state, not their own.

    Each can hold two different records, and both are returned:

      * `contributed` -- the counter at a moment the joker was actually
        scoring. The joker's own record.
      * `ambient` -- the highest the counter reached at all, held or not. A
        Fortune Teller record set without ever owning one is this; it is a
        record about the run rather than about the joker.

    Both are derived in ingest (joker_counter_peaks); nothing here counts.
    """
    w, p = where(q, endless_col="cp.endless")
    # The toggle picks between a counter joker's two records. It applies only
    # here: a scaling joker has no such distinction and is never filtered out
    # by it.
    want = q.get("held")
    out = []
    for col, held in (("contributed", True), ("ambient", False)):
        if want in ("0", "1") and bool(int(want)) is not held:
            continue
        for r in rows(db, f"""
                WITH ranked AS (
                  SELECT cp.joker_key, cp.{col} value, cp.{col}_value contribution,
                         r.run_id, r.deck_name, r.deck_key, r.stake_key,
                         ROW_NUMBER() OVER (PARTITION BY cp.joker_key
                                            ORDER BY cp.{col}_value DESC) rn
                    FROM joker_counter_peaks cp JOIN runs r USING (run_id)
                   WHERE cp.{col} IS NOT NULL{w})
                SELECT * FROM ranked WHERE rn = 1 AND value > 0""", p):
            key = r["joker_key"]
            name, what = COUNTER_LABEL.get(key, (key, ""))
            field = ingester.COUNTER_JOKERS.get(key, (None, "chips"))[1]
            counter = r["value"]
            # Converted once, in the ingester. Three call sites used to do
            # this arithmetic and two of them render side by side.
            value = r["contribution"]
            out.append({"joker_key": key, "joker": name, "what": what,
                        "field": field, "counter": counter,
                        "held": held, "value": value, "run_id": r["run_id"],
                        "deck_name": r["deck_name"], "deck_key": r["deck_key"],
                        "stake_key": r["stake_key"]})
    out.sort(key=lambda r: -(r["value"] or 0))
    return out


def api_hands(db, q):
    w, p = where(q, endless_col="h.endless")
    best = rows(db, f"""
        WITH ranked AS (
          SELECT h.hand, h.score_txt, h.score_ord, h.level, r.run_id, r.deck_name,
                 r.deck_key, r.stake_key,
                 ROW_NUMBER() OVER (PARTITION BY h.hand
                            ORDER BY h.score_ord DESC, CAST(h.score_txt AS REAL) DESC) rn
            FROM hands h JOIN runs r USING (run_id)
           WHERE h.hand IS NOT NULL AND h.score_ord IS NOT NULL{w})
        SELECT hand, score_txt value, score_ord ord, level, run_id, deck_name,
               deck_key, stake_key
          FROM ranked
         WHERE rn = 1 ORDER BY score_ord DESC""", p)
    return {"best": best}


def api_hand_levels(db, q):
    """The highest level each hand has ever been taken to, and in which run.

    Separate from best-score because they answer different questions: a hand
    can be levelled high and never scored well, and the run that did one is
    rarely the run that did the other.
    """
    w, p = where(q, endless_col="hl.endless")
    return rows(db, f"""
        WITH ranked AS (
          SELECT hl.hand, hl.lvl_to level, r.run_id, r.deck_name,
                 r.deck_key, r.stake_key,
                 ROW_NUMBER() OVER (PARTITION BY hl.hand
                                    ORDER BY hl.lvl_to DESC) rn
            FROM hand_levels hl JOIN runs r USING (run_id)
           WHERE hl.hand IS NOT NULL AND hl.lvl_to IS NOT NULL{w})
        SELECT hand, level, run_id, deck_name, deck_key, stake_key
          FROM ranked WHERE rn = 1 ORDER BY level DESC""", p)


def api_hand_counts(db, q):
    """The most times each hand has been played within a single run.

    A companion to best-score and highest-level: the hand you lean on is not
    always the one that scores biggest or the one you levelled.
    """
    w, p = where(q, endless_col="h.endless")
    return rows(db, f"""
        WITH per_run AS (
          SELECT h.hand, r.run_id, r.deck_name, r.deck_key, r.stake_key,
                 COUNT(*) played
            FROM hands h JOIN runs r USING (run_id)
           WHERE h.hand IS NOT NULL{w}
           GROUP BY h.hand, r.run_id),
        ranked AS (
          SELECT *, ROW_NUMBER() OVER (PARTITION BY hand
                                       ORDER BY played DESC) rn
            FROM per_run)
        SELECT hand, played, run_id, deck_name, deck_key, stake_key
          FROM ranked WHERE rn = 1 ORDER BY played DESC""", p)


def api_antes(db, q):
    """Distribution of how far runs got. Single series, so no categorical
    palette is involved."""
    w, p = where(q)
    return rows(db, f"""
        SELECT COALESCE(r.furthest_ante, r.ended_ante) ante, COUNT(*) n
          FROM runs r WHERE COALESCE(r.furthest_ante, r.ended_ante) IS NOT NULL{w}
         GROUP BY ante ORDER BY ante""", p)


def run_records_for(db, rid):
    """One run's records, with the endless ones filtered as attach_records
    filters them -- one rule, read from one place."""
    union = endless_union(db)
    label = {u["run_id"]: (u["started_ts"], u["deck_name"])
             for u in db.execute("SELECT run_id, started_ts, deck_name FROM runs")}
    out = []
    for rec in rows(db, """SELECT rr.kind, rr.subject, rr.endless, rr.field, rr.held,
                                  rr.value_txt, rr.value_ord, rr.prev_txt,
                                  pr.started_ts prev_ts, pr.deck_name prev_deck
                             FROM run_records rr
                             LEFT JOIN runs pr ON pr.run_id = rr.prev_run
                            WHERE rr.run_id = ?""", (rid,)):
        if rec["endless"]:
            hit = union.get((rid, rec["kind"], rec["subject"]))
            if hit is None:
                continue
            rec = dict(rec)
            rec["prev_txt"], prev_run = hit
            rec["prev_ts"], rec["prev_deck"] = label.get(prev_run, (None, None))
        out.append(rec)
    return out


def api_run(db, q):
    rid = q.get("id")
    if not rid:
        return {"error": "no id"}
    run = rows(db, "SELECT * FROM runs WHERE run_id = ?", (rid,))
    return {
        "run": run[0] if run else None,
        "segments": rows(db, "SELECT * FROM segments WHERE run_id=? ORDER BY seg", (rid,)),
        "rounds": rows(db, """SELECT round_seq, ante, blind_name, is_boss, endless,
                                     required_txt, score_txt, cashout_total, deck_size,
                                     deck_stone, deck_perma_max
                                FROM rounds WHERE run_id=? ORDER BY round_seq""", (rid,)),
        "hands": rows(db, """SELECT round_seq, ante, hand, level, score_txt, oneshot, endless
                               FROM hands WHERE run_id=? ORDER BY seg, n""", (rid,)),
        "jokers": rows(db, """SELECT key, field, MAX(to_ord) o,
                                     (SELECT to_txt FROM joker_scale x
                                       WHERE x.run_id=j.run_id AND x.key=j.key
                                         AND x.field=j.field
                                       ORDER BY x.to_ord DESC LIMIT 1) peak
                                FROM joker_scale j WHERE run_id=? AND is_reset=0
                               GROUP BY key, field ORDER BY o DESC""", (rid,)),
        # Every record this run set, both contests, whatever the page is
        # currently filtered to -- the dialog is about this run, not about
        # the slice you arrived from.
        # Same rule as the Runs column: an endless row counts only when it
        # is also a record with the restriction lifted.
        "records": sorted(
            run_records_for(db, rid),
            key=lambda x: (RECORD_ORDER.get(x["kind"], 9), x["endless"],
                           -(x["value_ord"] or 0))),
        "defects": rows(db, "SELECT defect, detail FROM run_defects WHERE run_id=?", (rid,)),
        # The whole run, like everything else in this dialog -- the Runs
        # column applies the phase filter, so the two can legitimately
        # differ for a run that went endless. What they must not do is
        # disagree about what "won" means, which is why the rule lives
        # here rather than being reimplemented over the rounds array.
        "rounds_won": db.execute(
            "SELECT COUNT(*) FROM rounds WHERE run_id=? AND cashout_total IS NOT NULL",
            (rid,)).fetchone()[0],
    }


# ─── the run in progress ──────────────────────────────────────────────────
# Which run you are in the middle of is a question about the LOG, not about
# the run's recorded status. Status gets it wrong both ways:
#
#   * a run that won and continued past the win ante is `completed` with a
#     terminal result, and is still being played;
#   * a `suspended` run you quit and never went back to is not.
#
# So the current run is whichever log was written last, and its status only
# decides what the badge says. A `died` run is genuinely over, so the panel
# hides rather than announcing a corpse as live.
#
# Records here are compared against EVERY other run rather than against the
# filtered slice. "The record you are chasing" is a fact about the whole
# corpus; it should not move because the page is currently filtered to one
# deck you are not playing.

# How far along counts as worth mentioning. Below this it is not news.
CHASE_AT = 0.5
# All of them are returned -- the page shows a few and expands the rest, so
# cutting the list here would leave it with nothing to expand. The cap is
# only a ceiling on an absurd answer.
CHASE_MAX = 60

# Past this, the last thing you played stops being "the run you are in the
# middle of" and the panel goes away rather than going stale on screen.
LIVE_STALE = 6 * 3600
# And past this it is no longer live, just recent -- the badge stops
# pulsing and says when it was last played.
LIVE_FRESH = 15 * 60

# Set from --logs at startup; the run rows carry a bare filename.
LOGS = ingester.DEFAULT_LOGS


def log_mtime(name):
    """When that run's log was last appended to -- the only wall clock there
    is, since the timestamps inside a log are relative to each segment."""
    for cand in (os.path.join(LOGS, name), name):
        try:
            return os.path.getmtime(cand)
        except OSError:
            continue
    return None


def _best_per(db, sql, params):
    """{subject: (ord, txt)} from a query yielding (k, o, t)."""
    return {r["k"]: (r["o"], r["t"]) for r in db.execute(sql, params)}


# Each family: how to read one run's figures, and everyone else's. `{op}`
# becomes `=` for the live run and `<>` for the rest, so the pair is always
# the same query asked twice and cannot drift.
CHASE = {
    "hand_score": """
        WITH r AS (SELECT hand k, score_ord o, score_txt t,
                          ROW_NUMBER() OVER (PARTITION BY hand
                                             ORDER BY score_ord DESC) rn
                     FROM hands
                    WHERE score_ord IS NOT NULL AND run_id {op} ?{scope})
        SELECT k, o, t FROM r WHERE rn = 1""",
    "hand_level": """
        SELECT hand k, MAX(lvl_to) o, MAX(lvl_to) t FROM hand_levels
         WHERE lvl_to IS NOT NULL AND hand IS NOT NULL AND run_id {op} ?{scope}
         GROUP BY hand""",
    # Per RUN first, then the best of those: the record is the most times
    # it was played inside one run, not across the corpus. Grouping by
    # (hand, run_id) alone yielded one row per run and the reader kept
    # whichever came last -- Two Pair's record read 2 against a real 80.
    "hand_played": """
        SELECT k, MAX(c) o, MAX(c) t FROM (
          SELECT hand k, COUNT(*) c FROM hands
           WHERE hand IS NOT NULL AND run_id {op} ?{scope}
           GROUP BY hand, run_id)
         GROUP BY k""",
    "joker": """
        WITH r AS (SELECT key k, to_ord o, to_txt t,
                          ROW_NUMBER() OVER (PARTITION BY key
                                             ORDER BY to_ord DESC) rn
                     FROM joker_scale
                    WHERE is_reset = 0 AND to_ord IS NOT NULL
                      AND key NOT IN {decaying} AND run_id {op} ?{scope})
        SELECT k, o, t FROM r WHERE rn = 1""",
    # Per joker, not per counter: Bull and Bootstraps read the same money
    # and are worth different things for it.
    "counter": """
        SELECT joker_key k, MAX(ambient_ord) o, MAX(ambient_value) t
          FROM joker_counter_peaks
         WHERE ambient_value IS NOT NULL AND run_id {op} ?{scope}
         GROUP BY joker_key""",
}


def _progress(mine, rec):
    """How far along, as a fraction. Falls back to the ordering keys when
    the values are past what a double holds -- 10**(a-b) is the ratio,
    since the key is log10(1+v)."""
    try:
        a, b = float(mine[1]), float(rec[1])
        if math.isfinite(a) and math.isfinite(b) and b > 0:
            return a / b
    except (TypeError, ValueError, OverflowError):
        pass
    if mine[0] is None or rec[0] is None:
        return None
    try:
        return 10 ** (mine[0] - rec[0])
    except OverflowError:
        return None


# Runs at one stake, as a clause the chase queries can append. A record at
# the stake you are actually playing is the one within reach; the all-time
# record is the one that counts. Both are worth seeing, so both are read.
STAKE_SCOPE = " AND run_id IN (SELECT run_id FROM runs WHERE stake_key = ?)"


def chase_tables(db, rid, stake_key):
    """{kind: (mine, all_time, at_stake)}, each {subject: (ord, txt)}.

    Everyone else's best is read twice, once unscoped and once among runs at
    this stake. `mine` is this run and needs no scope -- it IS at this stake.
    """
    out = {}
    for kind, sql in CHASE.items():
        tmpl = sql.format(op="{op}", scope="{scope}",
                          decaying=ingester.DECAYING_SQL)
        mine = _best_per(db, tmpl.format(op="=", scope=""), (rid,))
        rest = _best_per(db, tmpl.format(op="<>", scope=""), (rid,))
        at = ({} if not stake_key else
              _best_per(db, tmpl.format(op="<>", scope=STAKE_SCOPE),
                        (rid, stake_key)))
        out[kind] = (mine, rest, at)
    return out


def chase_entry(kind, subject, cur, rec, at_stake, stake_key):
    """One comparison, against the corpus and against this stake."""
    # Counter jokers are filed under their own key now, so the subject IS
    # the art for every kind.
    art = subject
    pct = _progress(cur, rec) if (cur and rec) else (1.0 if cur else 0.0)
    return {
        "kind": kind, "subject": subject, "key": art,
        "value_txt": None if cur is None else str(cur[1]),
        "record_txt": None if rec is None else str(rec[1]),
        "stake_txt": None if at_stake is None else str(at_stake[1]),
        "stake_key": stake_key,
        # Beating the stake's best is a smaller thing than beating the
        # corpus, and it happens first, so it is said separately.
        "beats_stake": bool(cur and at_stake
                            and (_progress(cur, at_stake) or 0) > 1),
        "pct": pct,
        "first": cur is not None and rec is None,
    }


def api_live(db, q):
    # `?id=` points the panel at a chosen run instead of the current one --
    # for looking at a finished run the way you would have seen it while it
    # was being played, and for checking this panel without one in flight.
    if q.get("id"):
        run = db.execute("SELECT * FROM runs WHERE run_id = ?",
                         (q["id"],)).fetchone()
        run = dict(run) if run else None
        if run:
            run["last_ts"] = log_mtime(run["log_file"])
    else:
        # The newest log wins. Only the last few are worth stat-ing.
        run = None
        for cand in db.execute("""SELECT * FROM runs
                                   ORDER BY started_ts DESC LIMIT 12"""):
            at = log_mtime(cand["log_file"])
            if at is None:
                continue
            if run is None or at > run["last_ts"]:
                run = dict(cand)
                run["last_ts"] = at
        if run is not None:
            age = time.time() - run["last_ts"]
            # A run that ended in a death is over, whatever else is true of
            # it, and so is one a later run displaced -- its save is gone.
            # Nothing here is "current" once it has gone cold either.
            if (age > LIVE_STALE or run["result"] == "died"
                    or run["abandoned"]):
                run = None
    if not run:
        return {"run": None}
    rid = run["run_id"]

    # What the badge says. A won run that carried on past the win ante is
    # the case the old status test got wrong, so it gets its own word.
    age = time.time() - (run["last_ts"] or 0)
    run["fresh"] = age <= LIVE_FRESH
    run["phase"] = ("live" if run["result"] is None
                    else "over" if run["abandoned"]
                    else "paused" if run["result"] == "suspended"
                    else "endless" if run["went_endless"]
                    else "won" if run["won"] else "over")

    # The board as it stands: the most recent sample of this run's jokers.
    at = db.execute("""SELECT seg, n FROM joker_state WHERE run_id = ?
                        ORDER BY seg DESC, n DESC LIMIT 1""", (rid,)).fetchone()
    board = []
    if at:
        board = rows(db, """SELECT seg, n, pos, card_id, key, state
                              FROM joker_state
                             WHERE run_id = ? AND seg = ? AND n = ?
                             ORDER BY pos, card_id""", (rid, at["seg"], at["n"]))
        attach_scaling(db, rid, {(at["seg"], at["n"]): board})

    money = db.execute("""SELECT balance FROM money WHERE run_id = ?
                           ORDER BY seg DESC, n DESC LIMIT 1""", (rid,)).fetchone()
    # What went out and what came in. `delta` is read straight off the
    # argument to ease_dollars, so it is exact even where `before` is not
    # (see open-findings: five call sites apply their change synchronously).
    # Everything negative counts as spent, including what a blind took off
    # you -- it left your hands either way.
    flow = db.execute("""
        SELECT COALESCE(SUM(CASE WHEN delta < 0 THEN -delta END), 0) spent,
               COALESCE(SUM(CASE WHEN delta > 0 THEN delta END), 0) earned
          FROM money WHERE run_id = ?""", (rid,)).fetchone()
    best = db.execute("""SELECT hand, score_txt, score_ord FROM hands
                          WHERE run_id = ? AND score_ord IS NOT NULL
                          ORDER BY score_ord DESC LIMIT 1""", (rid,)).fetchone()
    where_now = db.execute("""SELECT ante, round_seq, blind_name, is_boss
                                FROM rounds WHERE run_id = ?
                               ORDER BY round_seq DESC LIMIT 1""", (rid,)).fetchone()

    stake_key = run["stake_key"]
    tables = chase_tables(db, rid, stake_key)


    # Every joker on the board and where this run stands with it, however
    # far off -- the whole board, not the part of it that happens to be
    # close. Built first, because a joker shown here is left out of the
    # chases below: rendered both ways the two lists were the same two
    # cards twice.
    #
    # A joker that has never scaled anywhere -- Riff-Raff, Faceless, Raised
    # Fist -- has no record to show and drops out, which is the same test
    # that keeps jokers carrying no number at all off the list.
    holding, seen = [], set()
    for j in board:
        key = j.get("key")
        if not key or key in seen or key in ingester.DECAYING:
            continue
        seen.add(key)
        # A counter joker is filed under its own key now, so the subject
        # is the same either way; only which table to read differs.
        kind = "counter" if key in ingester.COUNTER_JOKERS else "joker"
        subject = key
        mine, rest, at = tables[kind]
        cur, rec = mine.get(subject), rest.get(subject)
        if cur is None and rec is None:
            continue
        holding.append(chase_entry(kind, subject, cur, rec,
                                   at.get(subject), stake_key))
    holding.sort(key=lambda c: -(c["pct"] or 0))
    on_board = {(c["kind"], c["subject"]) for c in holding}

    # What else is within reach: hand scores, levels and counts, and the
    # jokers this run scaled but is no longer holding.
    chasing = []
    for kind, (mine, rest, at) in tables.items():
        for subject, cur in mine.items():
            if (kind, subject) in on_board:
                continue
            e = chase_entry(kind, subject, cur, rest.get(subject),
                            at.get(subject), stake_key)
            # Nobody has ever done it, or you are at least halfway there.
            if e["first"] or (e["pct"] or 0) >= CHASE_AT:
                chasing.append(e)
    chasing.sort(key=lambda c: (-(c["pct"] or 0), c["kind"]))

    return {
        "run": run,
        "board": board,
        "money": money["balance"] if money else None,
        "spent": flow["spent"],
        "earned": flow["earned"],
        "best_hand": best["score_txt"] if best else None,
        "best_hand_name": best["hand"] if best else None,
        "hands_played": db.execute(
            "SELECT COUNT(*) FROM hands WHERE run_id = ?", (rid,)).fetchone()[0],
        "ante": (where_now["ante"] if where_now else None) or run["furthest_ante"],
        "round": where_now["round_seq"] if where_now else None,
        "blind": where_now["blind_name"] if where_now else None,
        "is_boss": where_now["is_boss"] if where_now else None,
        "chasing": chasing[:CHASE_MAX],
        "chasing_n": len(chasing),
        "holding": holding,
    }


def api_version(db, q):
    return {"generation": generation, "last_sync": last_sync}


def api_round(db, q):
    """One round: the board going in, then every action in the order taken.

    Returns `board` (the jokers held, left to right) and `steps` -- plays and
    discards interleaved by event sequence, each with its cards, its score,
    the money it earned and any joker values it moved.

    Not a replay -- it is what was observed, not what can be re-executed -- but
    enough to walk through a round and see how it went.
    """
    rid, rs = q.get("id"), q.get("round")
    if not rid or not rs:
        return {"error": "need id and round"}

    steps = rows(db, """
        SELECT seg, n, t, 'play' AS kind, hand, level, oneshot,
               score_txt, score_num, chips_before_txt, chips_before_num,
               hands_left_before, discards_left_before, NULL AS cards_n
          FROM hands WHERE run_id = ? AND round_seq = ?
        UNION ALL
        SELECT seg, n, t, 'discard', NULL, NULL, NULL,
               NULL, NULL, NULL, NULL, NULL, NULL, cards
          FROM discards WHERE run_id = ? AND round_seq = ?
        ORDER BY seg, n""", (rid, rs, rid, rs))

    # cards and jokers for the steps, fetched once and bucketed by (seg, n)
    keys = {(s["seg"], s["n"]) for s in steps}
    cards, jokers = {}, {}
    if keys:
        for c in rows(db, f"""
                SELECT seg, n, pos, rank, suit, key, enhancement, edition,
                       seal, card_id
                  FROM cards WHERE run_id = ? AND role = 'cards'
                   AND event IN ('hand.play','hand.discard')
                 ORDER BY seg, n, pos""", (rid,)):
            cards.setdefault((c["seg"], c["n"]), []).append(c)
        for j in rows(db, """
                SELECT seg, n, pos, card_id, key, mult, x_mult, chips, extra
                  FROM joker_state WHERE run_id = ? AND round_seq = ?
                 ORDER BY seg, n, pos, card_id""", (rid, rs)):
            jokers.setdefault((j["seg"], j["n"]), []).append(j)

    attach_scaling(db, rid, jokers)
    attribute_money(db, rid, rs, steps)

    running = None
    prev = None
    for st in steps:
        k = (st["seg"], st["n"])
        st["cards"] = cards.get(k, [])
        st["jokers"] = jokers.get(k, [])
        # What this action moved, so scaling is visible without reprinting the
        # whole board on every row.
        st["joker_delta"] = joker_delta(prev, st["jokers"])
        if st["jokers"]:
            prev = st["jokers"]
        if st["kind"] == "play":
            # chips_before is the round total going in; the game floors each
            # hand's contribution, so the running total is floor-summed.
            base = st["chips_before_num"]
            if base is None:
                base = running or 0
            # NOT int(): score_num is a REAL and a hand can score .5, so
            # truncating made the first play of a round show a total smaller
            # than the hand above it (20,987.5 scored -> "20,987 total").
            running = base + (st["score_num"] or 0)
            st["total_after"] = running

    # The board is the earliest sample in the round. Jokers are only sampled
    # when a hand is played, so its VALUES are those after the first scored
    # hand, not at the blind -- the lineup is what this is for.
    board = next((st["jokers"] for st in steps if st["jokers"]), [])
    return {"board": board, "steps": steps}


# Joker fields that carry a scaling value, in the order they are reported.
SCALE_FIELDS = ("chips", "mult", "x_mult", "extra")


def attach_scaling(db, rid, jokers):
    """Give every sampled joker the value it is actually accumulating.

    A joker's ability fields are a poor guide to this. `extra` is a grab-bag:
    static config for Mail-In Rebate (5), Hanging Chad (2) and Hack (1), but
    the step size for Lucky Cat, whose real value is x_mult. And Wee Joker
    keeps its count at ability.extra.chips, which is nested and so lands in
    no column at all -- the one joker on the board with nothing under it.

    joker_scale is the accumulation funnel (every vanilla scaling joker goes
    through SMODS.scale_card), so it says exactly which jokers are changing
    and what the value is. A joker with no scale events gets nothing shown,
    which is the rule: only numbers that move.
    """
    events = rows(db, """
        SELECT seg, n, card_id, field, to_txt, to_ord FROM joker_scale
         WHERE run_id = ? ORDER BY seg, n""", (rid,))
    counters = rows(db, """
        SELECT seg, n, metric, value FROM joker_derived
         WHERE run_id = ? AND value IS NOT NULL ORDER BY seg, n""", (rid,))
    if not events and not counters:
        return
    # Walk samples and events together in (seg, n) order, carrying the latest
    # value per joker forward -- a joker that last scaled three rounds ago
    # still shows what it reached.
    # A counter joker stores nothing, so its value has to be read off the
    # counter it watches and converted into what it actually contributes --
    # Bull holds no number at all -- it reads your money and adds 2 chips a
    # dollar -- and Bootstraps reads the same money for 2 mult per five.
    # Without this they are the jokers on the board with a blank under them.

    cur, i = {}, 0
    cnt, ci = {}, 0
    for key in sorted(jokers):
        while i < len(events) and (events[i]["seg"], events[i]["n"]) <= key:
            e = events[i]
            cur[e["card_id"]] = {"field": e["field"], "value": e["to_txt"],
                                 "ord": e["to_ord"]}
            i += 1
        while ci < len(counters) and (counters[ci]["seg"], counters[ci]["n"]) <= key:
            cnt[counters[ci]["metric"]] = counters[ci]["value"]
            ci += 1
        for j in jokers[key]:
            scale = cur.get(j["card_id"])
            spec = ingester.COUNTER_JOKERS.get(j["key"])
            if scale is None and spec is not None:
                c = cnt.get(spec[0])          # spec[0] is the counter it reads
                if c is not None:
                    field, v = ingester.counter_value(j["key"], c)
                    scale = {"field": field, "value": v, "ord": ord_of(v)}
            j["scale"] = scale


def joker_delta(before, after):
    """Which jokers changed value between two samples, as display rows.

    `before` is None for the round's first sample: nothing to compare against,
    so nothing is reported rather than every joker appearing to have changed.
    """
    if not before or not after:
        return []
    was = {j["card_id"]: j for j in before}
    out = []
    for j in after:
        b = was.get(j["card_id"])
        if not b:
            continue
        for f in SCALE_FIELDS:
            if j[f] is not None and b[f] is not None and j[f] != b[f]:
                out.append({"key": j["key"], "field": f,
                            "from": b[f], "to": j[f]})
    return out


def attribute_money(db, rid, rs, steps):
    """Attach each action's money to it, in place.

    Which action earned a dollar is worked out at ingest time, where the whole
    ordered event stream is available -- see classify_money() there. Money the
    round produced but no action did (the cash out, a sell, a reroll) has a
    different cause and simply does not match here; the cash-out panel already
    accounts for it.
    """
    for st in steps:
        st["money"] = 0
    if not steps:
        return
    owed = {}
    for m in rows(db, """
            SELECT seg, cause_n, SUM(delta) d FROM money
             WHERE run_id = ? AND cause_n IS NOT NULL
               AND cause IN ('hand.play', 'hand.discard')
             GROUP BY seg, cause_n""", (rid,)):
        owed[(m["seg"], m["cause_n"])] = m["d"]
    for st in steps:
        st["money"] = owed.get((st["seg"], st["n"]), 0)


ROUTES = {
    "/api/round": api_round,
    "/api/live": api_live,
    "/api/version": api_version,
    "/api/meta": api_meta,
    "/api/summary": api_summary,
    "/api/runs": api_runs,
    "/api/jokers": api_all_jokers,
    "/api/hands": api_hands,
    "/api/hand_levels": api_hand_levels,
    "/api/hand_counts": api_hand_counts,
    "/api/metrics": api_metrics,
    "/api/antes": api_antes,
    "/api/run": api_run,
}


class Handler(BaseHTTPRequestHandler):
    db = None
    logs = None

    def log_message(self, *a):
        pass

    def send_bytes(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ROUTES:
            try:
                # Share the sync lock: the watcher purges and rebuilds a run on
                # this same connection, and a read landing in that window sees
                # the run missing entirely.
                with _sync_lock:
                    data = ROUTES[u.path](self.db, qdict(u.query))
                self.send_bytes(json.dumps(data).encode(), "application/json")
            except Exception as ex:
                self.send_bytes(json.dumps({"error": str(ex)}).encode(),
                                "application/json", 500)
            return

        rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
        # Serve subdirectories (assets/) but never escape WEB.
        path = os.path.normpath(os.path.join(WEB, rel))
        if not path.startswith(os.path.normpath(WEB) + os.sep) or not os.path.isfile(path):
            self.send_bytes(b"not found", "text/plain", 404)
            return
        ctype = {"html": "text/html; charset=utf-8", "css": "text/css",
                 "js": "text/javascript", "json": "application/json",
                 "png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml",
                 "woff2": "font/woff2"}.get(rel.rsplit(".", 1)[-1].lower(), "text/plain")
        with open(path, "rb") as fh:
            self.send_bytes(fh.read(), ctype)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8611)
    ap.add_argument("--logs", default=ingester.DEFAULT_LOGS)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(DB):
        raise SystemExit(f"no database at {DB}\nrun: python ingest/ingest.py --rebuild")

    # Launching twice -- from the in-game button, say, while one is already
    # running -- should open the dashboard, not crash on the bound port.
    import socket
    probe = socket.socket()
    probe.settimeout(0.4)
    already = probe.connect_ex(("127.0.0.1", a.port)) == 0
    probe.close()
    if already:
        url = f"http://localhost:{a.port}"
        print(f"already running at {url}")
        if not a.no_open:
            webbrowser.open(url)
        return 0

    Handler.db = connect()
    Handler.logs = a.logs
    globals()['LOGS'] = a.logs
    sync(Handler.db, a.logs)
    threading.Thread(target=watch, args=(Handler.db, a.logs),
                     daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://localhost:{a.port}"
    n = Handler.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    print(f"BalatroDB dashboard: {url}   ({n} runs)")
    print(f"watching {a.logs} -- new runs appear automatically")
    print("Ctrl-C to stop.")
    if not a.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
