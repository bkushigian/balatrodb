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
    db = sqlite3.connect(DB, check_same_thread=False)
    db.row_factory = sqlite3.Row
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
    if q.get("seeded") == "0":
        clauses.append(f"{prefix}seeded = 0")
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
    w, p = where(q)
    total = db.execute(f"SELECT COUNT(*) FROM runs r WHERE 1=1{w}", p).fetchone()[0]
    # Both sides must agree on the population. Counting wins over ALL runs
    # while dividing by terminal runs inflated the rate: two corpus runs are
    # won but suspended, which made 3/10 read as 50%.
    won = db.execute(
        f"SELECT COUNT(*) FROM runs r WHERE r.won=1 AND r.terminal=1{w}", p).fetchone()[0]
    term = db.execute(f"SELECT COUNT(*) FROM runs r WHERE r.terminal=1{w}", p).fetchone()[0]
    won_any = db.execute(f"SELECT COUNT(*) FROM runs r WHERE r.won=1{w}", p).fetchone()[0]

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
    ante = db.execute(
        f"""SELECT MAX(ro.ante) v FROM rounds ro JOIN runs r USING (run_id)
            WHERE 1=1{dw}""", dp).fetchone()
    cash = db.execute(
        f"""SELECT MAX(ro.cashout_total) v FROM rounds ro JOIN runs r USING (run_id)
            WHERE 1=1{dw}""", dp).fetchone()

    return {
        "runs": total,
        "won": won_any,
        "terminal": term,
        "win_pct": round(100.0 * won / term, 1) if term else None,
        "best_hand": best["v"] if best else None,
        "best_hand_name": best["hand"] if best else None,
        "best_hand_deck": best["deck_name"] if best else None,
        "max_money": money["v"] if money else None,
        "max_deck": deck["v"] if deck else None,
        "max_ante": ante["v"] if ante else None,
        "max_cashout": cash["v"] if cash else None,
    }


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
    if q.get("endless") in ("0", "1"):
        w += " AND r.went_endless = ?"
        p = p + [int(q["endless"])]
    return rows(db, f"""
        SELECT r.run_id, r.log_file, r.started_ts, r.deck_name, r.deck_key,
               r.stake_key, r.seed, r.seeded, r.won, r.result, r.terminal,
               r.went_endless, r.hands_played, r.final_dollars, r.deck_size,
               COALESCE(r.furthest_ante, r.ended_ante) ante,
               (SELECT MAX(score_ord) FROM hands h WHERE h.run_id = r.run_id) bh_ord,
               (SELECT score_txt FROM hands h WHERE h.run_id = r.run_id
                 ORDER BY score_ord DESC LIMIT 1) best_hand,
               (SELECT MAX(balance) FROM money m WHERE m.run_id = r.run_id) peak_money,
               (SELECT COUNT(*) FROM run_defects d WHERE d.run_id = r.run_id) defects
          FROM runs r WHERE 1=1{w} ORDER BY {sort} LIMIT 300""", p)


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
                    "what": None, "run_id": j["run_id"],
                    "deck_name": j["deck_name"], "deck_key": j["deck_key"],
                    "stake_key": j["stake_key"]})
    for d in api_derived(db, q):
        out.append({"key": d["joker_key"], "value": d["value"],
                    "ord": ord_of(d["value"]), "what": d["what"],
                    "run_id": d["run_id"], "deck_name": d["deck_name"],
                    "deck_key": d["deck_key"], "stake_key": d["stake_key"]})
    out.sort(key=lambda r: -(r["ord"] or 0))
    return out


def ord_of(v):
    """Same ordering key the ingester writes, so merged rows sort together."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return math.copysign(math.log10(1 + abs(x)), x)


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
             AND js.key NOT IN ('j_turtle_bean'){w}),
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
COUNTER_JOKERS = {
    "hand_plays":  ("j_supernova",     "Supernova",      "plays of one hand type"),
    "skips":       ("j_throwback",     "Throwback",      "blinds skipped"),
    "tarots":      ("j_fortune_teller", "Fortune Teller", "tarots used"),
    "stone_cards": ("j_stone",         "Stone Joker",    "stone cards held"),
}


def api_derived(db, q):
    """The jokers that read GAME counters rather than their own ability.

    Supernova, Throwback, Fortune Teller and Stone Joker cannot be read from a
    joker sample at all -- their value is a function of run history, so it is
    reconstructed into joker_derived at ingest.

    One row per joker, not per counter subject: Supernova's value is the count
    for whichever hand you just played, so the record is the single highest
    count reached, not a list of every hand type.
    """
    w, p = where(q, endless_col="d.endless")
    raw = rows(db, f"""
        WITH ranked AS (
          SELECT d.metric, d.subject, d.value, r.run_id, r.deck_name,
                 r.deck_key, r.stake_key,
                 ROW_NUMBER() OVER (PARTITION BY d.metric ORDER BY d.value DESC) rn
            FROM joker_derived d JOIN runs r USING (run_id) WHERE 1=1{w})
        SELECT * FROM ranked WHERE rn = 1 AND value > 0""", p)
    out = []
    for r in raw:
        key, name, what = COUNTER_JOKERS.get(r["metric"], (None, r["metric"], ""))
        out.append({"joker_key": key, "joker": name, "what": what,
                    "detail": r["subject"], "value": r["value"],
                    "run_id": r["run_id"], "deck_name": r["deck_name"],
                    "deck_key": r["deck_key"], "stake_key": r["stake_key"]})
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


def api_antes(db, q):
    """Distribution of how far runs got. Single series, so no categorical
    palette is involved."""
    w, p = where(q)
    return rows(db, f"""
        SELECT COALESCE(r.furthest_ante, r.ended_ante) ante, COUNT(*) n
          FROM runs r WHERE COALESCE(r.furthest_ante, r.ended_ante) IS NOT NULL{w}
         GROUP BY ante ORDER BY ante""", p)


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
        "defects": rows(db, "SELECT defect, detail FROM run_defects WHERE run_id=?", (rid,)),
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
            running = base + int(st["score_num"] or 0)
            st["total_after"] = running

    # The board is the earliest sample in the round. Jokers are only sampled
    # when a hand is played, so its VALUES are those after the first scored
    # hand, not at the blind -- the lineup is what this is for.
    board = next((st["jokers"] for st in steps if st["jokers"]), [])
    return {"board": board, "steps": steps}


# Joker fields that carry a scaling value, in the order they are reported.
SCALE_FIELDS = ("chips", "mult", "x_mult", "extra")


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
    "/api/version": api_version,
    "/api/meta": api_meta,
    "/api/summary": api_summary,
    "/api/runs": api_runs,
    "/api/jokers": api_all_jokers,
    "/api/hands": api_hands,
    "/api/hand_levels": api_hand_levels,
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
