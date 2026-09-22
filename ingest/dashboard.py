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
import json
import os
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "balatro.db")
WEB = os.path.join(HERE, "web")


def connect():
    db = sqlite3.connect(DB, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def rows(db, sql, params=()):
    return [dict(r) for r in db.execute(sql, params).fetchall()]


# ─── filters ──────────────────────────────────────────────────────────────
# Every query slices the same way: deck, stake, and endless phase. `endless`
# is per-event rather than per-run, because a run that continues past the win
# ante legitimately contributes to both leaderboards.

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
    won = db.execute(f"SELECT COUNT(*) FROM runs r WHERE r.won=1{w}", p).fetchone()[0]
    term = db.execute(f"SELECT COUNT(*) FROM runs r WHERE r.terminal=1{w}", p).fetchone()[0]

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
        "won": won,
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
        "score": "r.best_hand_ord DESC NULLS LAST",
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
                 r.run_id, r.deck_name, r.stake_key
            FROM joker_scale js JOIN runs r USING (run_id)
           WHERE js.is_reset = 0 AND js.to_ord IS NOT NULL{w}),
        ranked AS (
          SELECT *, ROW_NUMBER() OVER (
                      PARTITION BY key, field
                      ORDER BY to_ord DESC, CAST(to_txt AS REAL) DESC) rn
            FROM eligible)
        SELECT key, field, to_txt value, run_id, deck_name, stake_key
          FROM ranked WHERE rn = 1 ORDER BY to_ord DESC""", p)


def api_derived(db, q):
    """The jokers that read GAME counters rather than their own ability.

    Supernova, Throwback, Fortune Teller and Stone Joker cannot be read from a
    joker sample at all -- their value is a function of run history, so it is
    reconstructed into joker_derived at ingest.
    """
    w, p = where(q, endless_col="d.endless")
    return rows(db, f"""
        WITH ranked AS (
          SELECT d.metric, d.subject, d.value, r.run_id, r.deck_name,
                 ROW_NUMBER() OVER (PARTITION BY d.metric, d.subject
                                    ORDER BY d.value DESC) rn
            FROM joker_derived d JOIN runs r USING (run_id) WHERE 1=1{w})
        SELECT metric, subject, value, run_id, deck_name FROM ranked
         WHERE rn = 1 AND value > 0 ORDER BY metric, value DESC""", p)


def api_hands(db, q):
    w, p = where(q, endless_col="h.endless")
    best = rows(db, f"""
        WITH ranked AS (
          SELECT h.hand, h.score_txt, h.score_ord, h.level, r.run_id, r.deck_name,
                 ROW_NUMBER() OVER (PARTITION BY h.hand
                            ORDER BY h.score_ord DESC, CAST(h.score_txt AS REAL) DESC) rn
            FROM hands h JOIN runs r USING (run_id)
           WHERE h.hand IS NOT NULL AND h.score_ord IS NOT NULL{w})
        SELECT hand, score_txt value, level, run_id, deck_name FROM ranked
         WHERE rn = 1 ORDER BY score_ord DESC""", p)
    lw, lp = where(q, endless_col="hl.endless")
    levels = rows(db, f"""
        SELECT hl.hand, MAX(hl.lvl_to) level FROM hand_levels hl
          JOIN runs r USING (run_id) WHERE hl.hand IS NOT NULL{lw}
         GROUP BY hl.hand ORDER BY level DESC""", lp)
    return {"best": best, "levels": levels}


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


ROUTES = {
    "/api/meta": api_meta,
    "/api/summary": api_summary,
    "/api/runs": api_runs,
    "/api/jokers": api_jokers,
    "/api/derived": api_derived,
    "/api/hands": api_hands,
    "/api/antes": api_antes,
    "/api/run": api_run,
}


class Handler(BaseHTTPRequestHandler):
    db = None

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
                data = ROUTES[u.path](self.db, qdict(u.query))
                self.send_bytes(json.dumps(data).encode(), "application/json")
            except Exception as ex:
                self.send_bytes(json.dumps({"error": str(ex)}).encode(),
                                "application/json", 500)
            return

        name = "index.html" if u.path in ("/", "") else os.path.basename(u.path)
        path = os.path.join(WEB, name)
        if not os.path.isfile(path):
            self.send_bytes(b"not found", "text/plain", 404)
            return
        ctype = {"html": "text/html; charset=utf-8", "css": "text/css",
                 "js": "text/javascript"}.get(name.rsplit(".", 1)[-1], "text/plain")
        with open(path, "rb") as fh:
            self.send_bytes(fh.read(), ctype)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8611)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(DB):
        raise SystemExit(f"no database at {DB}\nrun: python ingest/ingest.py --rebuild")

    Handler.db = connect()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://localhost:{a.port}"
    n = Handler.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    print(f"BalatroDB dashboard: {url}   ({n} runs)")
    print("Ctrl-C to stop.")
    if not a.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
