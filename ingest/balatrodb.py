#!/usr/bin/env python3
"""BalatroDB ingester: folds append-only JSONL run logs into SQLite.

    python -m ingest.balatrodb --logs "%APPDATA%/Balatro/BalatroDB/runs" --db balatro.db

Design contract (see docs/db-schema.md):

  * `events` + `event_payload` are the system of record.  Rows are inserted
    once and never updated.  `(run_id, seg, n)` is unique.
  * Every other table is a pure function of those two and is rebuilt per run.
    `--rebuild` drops and re-derives the whole derived layer without touching
    a single JSONL file.
  * Ingest is resumable at byte granularity, so a file that is still being
    written can be ingested repeatedly as it grows.
  * Nothing here computes a statistic.  Statistics are SQL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
from typing import Any, Iterable

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
DERIVE_EPOCH = "4"          # bump to force a full derived-layer rebuild
HEAD_BYTES = 64 * 1024

# ---------------------------------------------------------------- numbers

NAN_TEXT = {"nan", "-nan"}


def fmt_float(x: float) -> str:
    """Canonical exact text for a Python number."""
    if isinstance(x, int):
        return str(x)
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return repr(x)


def num_pair(v: Any) -> tuple[str | None, float | None]:
    """Return (exact_text, order_key) for a value in either wire form.

    order_key is a *monotone* map of the real line onto floats:

        plain v   ->  sign(v) * log10(1 + |v|)
        {"s","l"} ->  l          (already sign(v) * log10(|v|), |v| >= 1e14)

    The two agree to within 1e-14 at the encoder's 1e14 threshold and never
    overlap, so MAX(order) ranks correctly across both encodings.  This is the
    whole point: `l` alone is NOT comparable with a raw magnitude, and a naive
    "store l if present else the number" column silently ranks 833027 above
    1.6e15.
    """
    if v is None:
        return None, None
    if isinstance(v, bool):
        return ("1" if v else "0"), (1.0 if v else 0.0)
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            if math.isnan(v):
                return "nan", None
            return ("inf" if v > 0 else "-inf"), (1e308 if v > 0 else -1e308)
        a = abs(v)
        o = math.copysign(math.log10(1.0 + a), v) if v else 0.0
        return fmt_float(v), o
    if isinstance(v, dict) and "s" in v:
        s = str(v["s"])
        if s.strip().lower() in NAN_TEXT:
            return s, None
        l = v.get("l")
        if l is None:
            # No magnitude: unorderable, but keep the exact text.
            return s, None
        return s, float(l)
    # Anything else (the mod's tostring() fallback) is text with no order.
    return str(v), None


def as_int(v: Any) -> int | None:
    """For a column declared INTEGER: loudly refuse a big-number dict."""
    if v is None or isinstance(v, bool):
        return int(v) if isinstance(v, bool) else None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, dict):
        raise ValueError(f"big number in an INTEGER column: {v!r}")
    return None


def num_add(a: Any, b: Any) -> tuple[str | None, float | None]:
    """chips_before + score, before + delta.  Exact while both are plain."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return num_pair(a + b)
    ax, ao = num_pair(a)
    bx, bo = num_pair(b)
    if ao is None or bo is None:
        return None, None
    # Both huge (or one huge): the sum is dominated by the larger magnitude.
    return None, max(ao, bo)


# ---------------------------------------------------------------- helpers

CARD_SINGLE_KEYS = ("card", "from", "to")
CARD_ARRAY_KEYS = ("deck", "deck_cards", "jokers", "consumables", "cards", "targets")

# Ability fields whose value is inert noise rather than information.  The mod
# elides defaults, but its table lists h_x_mult's inert value as 1 while the
# game's default is 0, so `"h_x_mult": 0` is emitted on essentially every card
# (1331 of 1338 state entries in a real 710-event log).  Dropping it here keeps
# card_state from being 99% noise; the raw value is still in event_payload.
INERT_STATE = {("h_x_mult", 0), ("h_x_mult", 0.0)}

ENHANCEMENTS = {
    "m_stone": "n_stone", "m_gold": "n_gold", "m_steel": "n_steel",
    "m_glass": "n_glass", "m_lucky": "n_lucky", "m_mult": "n_mult",
    "m_bonus": "n_bonus", "m_wild": "n_wild",
}

DOMAIN = {
    "joker": "joker", "card": "card", "consumable": "consumable", "voucher": "voucher",
}

# Defect bitmask.  Runs are tagged, never deleted: exclusion is a WHERE clause.
DEFECT_NO_START_DECK = 1 << 0    # run.start carries no deck_cards baseline
DEFECT_LEGACY_RESULT = 1 << 1    # result vocabulary predates died/completed/...
DEFECT_NO_TERMINAL = 1 << 2      # run.end has no `terminal` field
DEFECT_CONSUMABLES_AS_CARDS = 1 << 3  # consumables logged as card.add
DEFECT_NO_CARD_MODIFY = 1 << 4   # enhancements logged as card.add
DEFECT_DECK_IDENTITY = 1 << 5    # the deck-size identity does not hold
DEFECT_NO_RUN_END = 1 << 6       # file ends with no run.end (crash, or still live)
DEFECT_ENCODE_ERROR = 1 << 7     # at least one payload failed to encode
DEFECT_LEGACY_RUN_END_SCORE = 1 << 8   # run.end.score, not final_round_score
DEFECT_NO_BEST_HAND = 1 << 9     # no round_scores block: best_hand unavailable
DEFECT_NO_ENDLESS_FLAG = 1 << 10  # run.end carries no `endless`
DEFECT_ENDLESS_DISAGREE = 1 << 11  # run.end.endless contradicts the el stamps

# Sniffed log format.  The mod never bumped `v` when the layout changed, so the
# format is detected from which events and fields are present, not from `v`.
FORMAT_LEGACY = 0    # arrays inside run.start/run.end, run.end.score, card.add consumables
FORMAT_SPLIT = 1     # run.baseline / run.rebaseline / run.final present
FORMAT_SCORES = 2    # ...and run.end carries final_round_score / best_hand

DEFECT_NAMES = {
    DEFECT_NO_START_DECK: "no_start_deck",
    DEFECT_LEGACY_RESULT: "legacy_result_vocab",
    DEFECT_NO_TERMINAL: "no_terminal_flag",
    DEFECT_CONSUMABLES_AS_CARDS: "consumables_as_card_add",
    DEFECT_NO_CARD_MODIFY: "no_card_modify",
    DEFECT_DECK_IDENTITY: "deck_identity_fail",
    DEFECT_NO_RUN_END: "no_run_end",
    DEFECT_ENCODE_ERROR: "encode_error",
    DEFECT_LEGACY_RUN_END_SCORE: "legacy_run_end_score",
    DEFECT_NO_BEST_HAND: "no_best_hand",
    DEFECT_NO_ENDLESS_FLAG: "no_endless_flag",
    DEFECT_ENDLESS_DISAGREE: "endless_disagrees_with_el",
}

LEGACY_RESULTS = {"win", "loss", "quit"}


def sha_head(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read(HEAD_BYTES))
    return h.hexdigest()


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA synchronous = NORMAL")
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        con.executescript(fh.read())
    cur = con.execute("SELECT val FROM meta WHERE k = 'derive_epoch'")
    row = cur.fetchone()
    if row is None:
        con.execute("INSERT INTO meta(k, val) VALUES ('derive_epoch', ?)", (DERIVE_EPOCH,))
    elif row["val"] != DERIVE_EPOCH:
        # The derived layer's code changed.  It is a pure function of layer 1,
        # so throw it away and rebuild -- no JSONL needed.
        drop_derived(con)
        con.execute("UPDATE meta SET val = ? WHERE k = 'derive_epoch'", (DERIVE_EPOCH,))
    con.commit()
    return con


DERIVED_TABLES = [
    "validations", "snapshots", "hand_stats", "deck_samples", "card_state", "cards",
    "blind_selects", "blind_skips", "use_events", "shop_events", "inventory_events",
    "joker_scale",
    "money", "cashout_items", "hand_levels", "discards", "hands", "rounds",
    "run_segments", "runs",
]


def drop_derived(con: sqlite3.Connection) -> None:
    for t in DERIVED_TABLES:
        con.execute(f"DELETE FROM {t}")


# ---------------------------------------------------------------- layer 1

def ingest_file(con: sqlite3.Connection, path: str) -> tuple[str | None, int]:
    """Append every complete new line of `path` to `events`.  Returns
    (run_id, rows_added).  Safe to call repeatedly while the file grows."""
    st = os.stat(path)
    row = con.execute("SELECT * FROM ingest_files WHERE path = ?", (path,)).fetchone()
    head = sha_head(path)
    start = 0
    lines_ok = lines_bad = 0
    if row is not None:
        if row["head_sha"] == head and st.st_size >= row["size_seen"]:
            start = row["bytes_done"]
            lines_ok, lines_bad = row["lines_ok"], row["lines_bad"]
        # else: the file was replaced or truncated -- re-read it from 0.  The
        # (run_id, seg, n) uniqueness makes the re-read a no-op for rows we
        # already hold.
        if start >= st.st_size:
            return row["run_id"], 0

    run_id = row["run_id"] if row is not None else None
    added = 0
    consumed = start
    with open(path, "rb") as fh:
        fh.seek(start)
        buf = fh.read()
    # Never parse a torn trailing line: stop at the last newline.
    cut = buf.rfind(b"\n")
    if cut < 0:
        return run_id, 0
    body, consumed = buf[:cut + 1], start + cut + 1

    rows_ev, rows_pl = [], []
    for raw in body.split(b"\n"):
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw.decode("utf-8"))
            rid = ev["run"]
            el = 1 if ev.get("el") else 0
            rows_ev.append((rid, int(ev["seg"]), int(ev["n"]), int(ev.get("v", 0)),
                            ev.get("t"), ev["e"], as_int(ev.get("a")), as_int(ev.get("r")), el))
            rows_pl.append(json.dumps(ev.get("d") or {}, separators=(",", ":")))
            run_id = run_id or rid
            lines_ok += 1
        except Exception:
            lines_bad += 1

    cur = con.cursor()
    for evrow, payload in zip(rows_ev, rows_pl):
        cur.execute(
            "INSERT OR IGNORE INTO events(run_id,seg,n,v,t,e,a,r,el) "
            "VALUES (?,?,?,?,?,?,?,?,?)", evrow)
        if cur.rowcount:
            cur.execute("INSERT INTO event_payload(event_id, d) VALUES (?,?)",
                        (cur.lastrowid, payload))
            added += 1

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    con.execute(
        "INSERT INTO ingest_files(path,run_id,bytes_done,size_seen,mtime_ns,head_sha,"
        "lines_ok,lines_bad,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET run_id=excluded.run_id,"
        "bytes_done=excluded.bytes_done,size_seen=excluded.size_seen,"
        "mtime_ns=excluded.mtime_ns,head_sha=excluded.head_sha,"
        "lines_ok=excluded.lines_ok,lines_bad=excluded.lines_bad,"
        "last_seen=excluded.last_seen",
        (path, run_id or "?", consumed, st.st_size, st.st_mtime_ns, head,
         lines_ok, lines_bad, now, now))
    return run_id, added


# ---------------------------------------------------------------- layer 2

def iter_events(con: sqlite3.Connection, run_id: str) -> Iterable[tuple[sqlite3.Row, dict]]:
    cur = con.execute(
        "SELECT e.*, p.d AS payload FROM events e JOIN event_payload p USING(event_id) "
        "WHERE e.run_id = ? ORDER BY e.seg, e.n", (run_id,))
    for row in cur:
        yield row, json.loads(row["payload"])


def flatten_cards(d: dict) -> Iterable[tuple[str, int, dict]]:
    for k in CARD_SINGLE_KEYS:
        v = d.get(k)
        if isinstance(v, dict) and ("id" in v or "key" in v):
            yield k, 0, v
    for k in CARD_ARRAY_KEYS:
        v = d.get(k)
        if isinstance(v, list):
            for i, c in enumerate(v):
                if isinstance(c, dict):
                    yield k, i, c


def derive_run(con: sqlite3.Connection, run_id: str) -> None:
    """Rebuild every derived row for one run, from `events` alone."""
    # Deleting the runs row cascades the whole derived layer for this run; the
    # stub is re-inserted immediately so the child tables have a parent to
    # reference while we fill them, and is completed by the UPDATE at the end.
    con.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))   # cascades
    con.execute("INSERT INTO runs(run_id) VALUES (?)", (run_id,))

    meta: dict[str, Any] = {}
    segs: list[tuple] = []
    rounds: list[dict] = []
    open_round: dict | None = None
    ordinal = 0
    n_events = 0
    v_min = v_max = None
    defects = 0
    saw = set()
    result_row = None
    won = 0
    won_ante = None
    entered_endless = 0
    final_ante = final_round = hands_played = None
    furthest_ante = furthest_round = None
    frs_x = frs_o = bh_x = bh_o = None
    end_endless = None
    fmt = FORMAT_LEGACY

    ins = con.cursor()
    deck_base = 0
    deck_adds = deck_rems = 0
    validations: list[tuple] = []

    for row, d in iter_events(con, run_id):
        e, seg, n, el = row["e"], row["seg"], row["n"], row["el"]
        n_events += 1
        v_min = row["v"] if v_min is None else min(v_min, row["v"])
        v_max = row["v"] if v_max is None else max(v_max, row["v"])
        saw.add(e)

        # ---- lifecycle
        if e in ("run.start", "run.resume"):
            if e == "run.start":
                env = d.get("env") or {}
                meta = dict(
                    started_utc=as_int(d.get("ts")), seed=d.get("seed"),
                    seeded=1 if d.get("seeded") else 0,
                    challenge=json.dumps(d["challenge"]) if d.get("challenge") else None,
                    deck=d.get("deck"), deck_key=d.get("deck_key"),
                    stake=as_int(d.get("stake")), stake_key=d.get("stake_key"),
                    win_ante=as_int(d.get("win_ante")), profile=as_int(d.get("profile")),
                    starting_deck_size=as_int(d.get("starting_deck_size")),
                    game_version=env.get("game"), lovely_version=env.get("lovely"),
                    smods_version=env.get("smods"), balatrodb_version=env.get("balatrodb"),
                    mods_json=json.dumps(env.get("mods") or []))
                # Whether a baseline exists at all is decided after the scan:
                # in the current format it arrives in the run.baseline line.
            # Legacy logs carry the baseline inline; current logs send it in the
            # run.baseline line that follows, and we patch the segment there.
            base = len(d.get("deck_cards") or [])
            deck_base, deck_adds, deck_rems = base, 0, 0
            dx, do = num_pair(d.get("dollars"))
            cx, co = num_pair(d.get("chips"))
            segs.append(dict(run_id=run_id, seg=seg,
                             kind="start" if e == "run.start" else "resume",
                             started_utc=as_int(d.get("ts")),
                             dollars_exact=dx, dollars_ord=do,
                             chips_exact=cx, chips_ord=co,
                             deck_baseline=base, n_open=n, n_baseline=None, n_events=0))

        elif e in ("run.baseline", "run.rebaseline"):
            fmt = max(fmt, FORMAT_SPLIT)
            base = len(d.get("deck_cards") or [])
            deck_base, deck_adds, deck_rems = base, 0, 0
            if segs and segs[-1]["seg"] == seg:
                s = segs[-1]
                s["deck_baseline"] = base
                s["n_baseline"] = n
                if d.get("dollars") is not None:
                    s["dollars_exact"], s["dollars_ord"] = num_pair(d.get("dollars"))
                if d.get("chips") is not None:
                    s["chips_exact"], s["chips_ord"] = num_pair(d.get("chips"))
            else:
                # A baseline with no opener: the file was truncated at the head.
                segs.append(dict(run_id=run_id, seg=seg, kind="resume",
                                 started_utc=None, dollars_exact=None, dollars_ord=None,
                                 chips_exact=None, chips_ord=None, deck_baseline=base,
                                 n_open=None, n_baseline=n, n_events=0))

        elif e == "run.final":
            fmt = max(fmt, FORMAT_SPLIT)

        elif e == "run.win":
            won = 1
            won_ante = as_int(d.get("ante"))

        elif e == "run.end":
            result_row = d
            if d.get("won"):
                won = 1
            if d.get("result") in LEGACY_RESULTS:
                defects |= DEFECT_LEGACY_RESULT
            if d.get("terminal") is None:
                defects |= DEFECT_NO_TERMINAL
            final_ante = as_int(d.get("ante"))
            final_round = as_int(d.get("round"))
            hands_played = as_int(d.get("hands_played"))
            # `score` was renamed to `final_round_score`.  Both mean the score of
            # the round the run ended on; neither is the run's best.
            if "final_round_score" in d:
                fmt = max(fmt, FORMAT_SCORES)
                frs_x, frs_o = num_pair(d.get("final_round_score"))
            else:
                defects |= DEFECT_LEGACY_RUN_END_SCORE
                frs_x, frs_o = num_pair(d.get("score"))
            if "best_hand" in d:
                bh_x, bh_o = num_pair(d.get("best_hand"))
            else:
                defects |= DEFECT_NO_BEST_HAND
                bh_x, bh_o = None, None
            furthest_ante = as_int(d.get("furthest_ante"))
            furthest_round = as_int(d.get("furthest_round"))
            if "endless" in d:
                end_endless = 1 if d.get("endless") else 0
            else:
                defects |= DEFECT_NO_ENDLESS_FLAG

        elif e == "encode.error":
            defects |= DEFECT_ENCODE_ERROR

        # ---- rounds
        elif e == "round.start":
            if open_round is not None:
                rounds.append(open_round)
            bx, bo = num_pair(d.get("chips"))
            open_round = dict(run_id=run_id, seg=seg, n_start=n, n_end=None,
                              ordinal=ordinal, el=el, ante=as_int(d.get("ante")),
                              blind_key=d.get("blind_key"), blind_name=d.get("name"),
                              boss=1 if d.get("boss") else 0, reward=as_int(d.get("reward")),
                              blind_chips_exact=bx, blind_chips_ord=bo, cleared=0,
                              score_exact=None, score_ord=None, cashout_exact=None,
                              cashout_ord=None, dollars_before=None, deck_size=None,
                              n_hands=0, n_discards=0)
            ordinal += 1

        elif e == "round.end":
            sx, so = num_pair(d.get("score"))
            tx, to = num_pair(d.get("total"))
            if open_round is not None:
                open_round.update(n_end=n, cleared=1, score_exact=sx, score_ord=so,
                                  cashout_exact=tx, cashout_ord=to,
                                  dollars_before=as_int(d.get("dollars_before")),
                                  deck_size=as_int(d.get("deck_size")))
                rounds.append(open_round)
                ruid = None  # assigned after insert
                open_round["_items"] = d.get("items") or []
                open_round = None

        # ---- everything else is row-per-event
        if e == "hand.play":
            cbx, cbo = num_pair(d.get("chips_before"))
            sx, so = num_pair(d.get("score"))
            cax, cao = num_add(d.get("chips_before"), d.get("score"))
            bx, bo = num_pair(d.get("blind_chips"))
            ins.execute(
                "INSERT OR REPLACE INTO hands(run_id,seg,n,round_uid,el,hand_key,level,"
                "n_cards,oneshot,hands_left_before,discards_left_before,score_exact,"
                "score_ord,chips_before_exact,chips_before_ord,chips_after_exact,"
                "chips_after_ord,blind_chips_exact,blind_chips_ord) "
                "VALUES (?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, seg, n, el, d.get("hand") or "?", as_int(d.get("level")),
                 len(d.get("cards") or []), 1 if d.get("oneshot") else 0,
                 as_int(d.get("hands_left_before")), as_int(d.get("discards_left_before")),
                 sx, so, cbx, cbo, cax, cao, bx, bo))
            if open_round is not None:
                open_round["n_hands"] += 1

        elif e == "hand.discard":
            ins.execute("INSERT OR REPLACE INTO discards VALUES (?,?,?,NULL,?,?,?)",
                        (run_id, seg, n, el, len(d.get("cards") or []),
                         as_int(d.get("discards_left_before"))))
            if open_round is not None:
                open_round["n_discards"] += 1

        elif e == "hand.levelup":
            ins.execute("INSERT OR REPLACE INTO hand_levels VALUES (?,?,?,?,?,?,?,?)",
                        (run_id, seg, n, el, d.get("hand") or "?",
                         as_int(d.get("from")), as_int(d.get("to")), as_int(d.get("amount"))))

        elif e == "money.change":
            bx, bo = num_pair(d.get("before"))
            dx, do = num_pair(d.get("delta"))
            ax, ao = num_add(d.get("before"), d.get("delta"))
            ins.execute("INSERT OR REPLACE INTO money VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (run_id, seg, n, el, bx, bo, dx, do, ax, ao))

        elif e in ("joker.scale", "joker.reset"):
            fx, fo = num_pair(d.get("from"))
            tx, to = num_pair(d.get("to"))
            ins.execute("INSERT OR REPLACE INTO joker_scale VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, seg, n, el, "scale" if e == "joker.scale" else "reset",
                         as_int(d.get("id")), d.get("key") or "?", d.get("name"),
                         d.get("field") or "?", d.get("op"), fx, fo, tx, to))

        elif "." in e and e.split(".")[0] in DOMAIN and e.split(".")[1] in ("add", "remove", "modify"):
            dom, act = e.split(".", 1)
            card = d.get("card") or d.get("to") or {}
            ins.execute("INSERT OR REPLACE INTO inventory_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, seg, n, el, dom, act, d.get("reason"), d.get("area"),
                         as_int(card.get("id")), card.get("key"), card.get("set"),
                         d.get("what")))
            if dom == "card" and act == "add":
                deck_adds += 1
            elif dom == "card" and act == "remove":
                deck_rems += 1

        elif e in ("shop.buy", "shop.sell", "shop.reroll"):
            card = d.get("card") or {}
            amt = d.get("cost") if e != "shop.sell" else d.get("value")
            ax, ao = num_pair(amt)
            ins.execute("INSERT OR REPLACE INTO shop_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, seg, n, el, e.split(".")[1], as_int(card.get("id")),
                         card.get("key"), card.get("set"),
                         1 if d.get("and_use") else 0, ax, ao))

        elif e in ("consumable.use", "pack.open", "voucher.redeem"):
            card = d.get("card") or {}
            ins.execute("INSERT OR REPLACE INTO use_events VALUES (?,?,?,?,?,?,?,?,?)",
                        (run_id, seg, n, el, e, as_int(card.get("id")), card.get("key"),
                         card.get("set"), len(d.get("targets") or [])))

        elif e == "blind.select":
            ins.execute("INSERT OR REPLACE INTO blind_selects VALUES (?,?,?,?,?,?,?,?)",
                        (run_id, seg, n, el, as_int(row["a"]), d.get("blind_key"),
                         d.get("name"), 1 if d.get("entered_endless") else 0))

        elif e == "blind.skip":
            ins.execute("INSERT OR REPLACE INTO blind_skips VALUES (?,?,?,?,?,?,?,?)",
                        (run_id, seg, n, el, as_int(d.get("ante")),
                         d.get("blind_on_deck"), json.dumps(d.get("tag")) if d.get("tag") else None,
                         as_int(d.get("skips"))))

        elif e == "snapshot":
            cu = d.get("consumeable_usage_total") or {}
            dx, do = num_pair(d.get("dollars"))
            cx, co = num_pair(d.get("chips"))
            ins.execute("INSERT OR REPLACE INTO snapshots VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, seg, n, el, dx, do, cx, co,
                         as_int(d.get("deck_size")), as_int(d.get("skips")),
                         as_int(d.get("tag_tally")), as_int(cu.get("tarot")),
                         as_int(cu.get("planet")), as_int(cu.get("spectral")),
                         as_int(cu.get("all")), len(d.get("jokers") or []),
                         len(d.get("consumables") or []), len(d.get("vouchers") or {})))
            for hk, hv in (d.get("hands") or {}).items():
                ins.execute("INSERT OR REPLACE INTO hand_stats VALUES (?,?,?,?,?,?,?)",
                            (run_id, seg, n, el, hk, as_int(hv.get("level")),
                             as_int(hv.get("played"))))

        # ---- cards, state, deck samples
        for role, pos, card in flatten_cards(d):
            scx, sco = num_pair(card.get("sell_cost"))
            stickers = ",".join(sorted(card.get("stickers") or [])) or None
            ins.execute("INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, seg, n, role, pos, el, as_int(card.get("id")),
                         card.get("key"), card.get("name"), card.get("set"),
                         card.get("rank"), card.get("suit"), card.get("enhancement"),
                         card.get("edition"), card.get("seal"), stickers, scx, sco))
            for f, val in (card.get("state") or {}).items():
                if (f, val) in INERT_STATE:
                    continue
                vx, vo = num_pair(val)
                ins.execute("INSERT OR REPLACE INTO card_state VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (run_id, seg, n, role, pos, f, el, card.get("key") or "?", vx, vo))

        deck_arr = None
        source = None
        if e in ("run.start", "run.resume", "run.baseline", "run.rebaseline"):
            deck_arr, source = d.get("deck_cards"), e
        elif e in ("round.end", "run.end", "run.final"):
            deck_arr, source = d.get("deck") or d.get("deck_cards"), e
        if isinstance(deck_arr, list) and deck_arr:
            counts = {v: 0 for v in ENHANCEMENTS.values()}
            n_enh = n_seal = n_ed = 0
            pb_sum = pb_max = 0.0
            for c in deck_arr:
                k = c.get("key")
                if k in ENHANCEMENTS:
                    counts[ENHANCEMENTS[k]] += 1
                if c.get("set") == "Enhanced":
                    n_enh += 1
                if c.get("seal"):
                    n_seal += 1
                if c.get("edition"):
                    n_ed += 1
                pb = (c.get("state") or {}).get("perma_bonus")
                if isinstance(pb, (int, float)):
                    pb_sum += pb
                    pb_max = max(pb_max, pb)
            ins.execute(
                "INSERT OR REPLACE INTO deck_samples VALUES "
                "(?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, seg, n, el, source, len(deck_arr), as_int(d.get("deck_size")),
                 counts["n_stone"], counts["n_gold"], counts["n_steel"], counts["n_glass"],
                 counts["n_lucky"], counts["n_mult"], counts["n_bonus"], counts["n_wild"],
                 n_enh, n_seal, n_ed, pb_sum, pb_max))

        # ---- the deck-size identity
        reported = d.get("deck_size") if e in ("round.end", "snapshot", "run.end") else None
        if isinstance(reported, (int, float)) and e != "run.end":
            expect = deck_base + deck_adds - deck_rems
            if expect != reported:
                validations.append((run_id, seg, n, "deck_size_identity",
                                    str(expect), str(int(reported)),
                                    float(expect - reported),
                                    f"{e}: base {deck_base} + adds {deck_adds} - removes {deck_rems}"))

    if open_round is not None:
        rounds.append(open_round)

    # runs row
    if not meta:
        meta = dict.fromkeys(
            ("started_utc seed seeded challenge deck deck_key stake stake_key win_ante "
             "profile starting_deck_size game_version lovely_version smods_version "
             "balatrodb_version mods_json").split())
    if "run.end" not in saw:
        defects |= DEFECT_NO_RUN_END
    if any(s["kind"] == "start" and not s["deck_baseline"] for s in segs):
        defects |= DEFECT_NO_START_DECK
    if "consumable.use" in saw and "consumable.add" not in saw:
        defects |= DEFECT_CONSUMABLES_AS_CARDS
    if "card.modify" not in saw and any(v[3] == "deck_size_identity" for v in validations):
        defects |= DEFECT_NO_CARD_MODIFY
    if validations:
        defects |= DEFECT_DECK_IDENTITY
    entered_endless = 1 if con.execute(
        "SELECT 1 FROM events WHERE run_id=? AND el=1 LIMIT 1", (run_id,)).fetchone() else 0
    if end_endless is not None and end_endless != entered_endless:
        defects |= DEFECT_ENDLESS_DISAGREE
    is_open = 1
    if result_row is not None and result_row.get("terminal"):
        is_open = 0
    elif result_row is not None and result_row.get("result") in ("died", "loss", "completed",
                                                                "new_run", "restart",
                                                                "abandoned", "profile_switch",
                                                                "exited", "unknown"):
        is_open = 0

    cols = ("started_utc seed seeded challenge deck deck_key stake stake_key win_ante "
            "profile starting_deck_size game_version lovely_version smods_version "
            "balatrodb_version mods_json").split()
    con.execute(
        "UPDATE runs SET " + ",".join(f"{c}=?" for c in cols) +
        ",v_min=?,v_max=?,segments=?,n_events=?,won=?,won_ante=?,result=?,terminal=?,"
        "at_state=?,entered_endless=?,open=?,crashed=?,final_ante=?,final_round=?,"
        "hands_played=?,endless=?,final_round_score_exact=?,final_round_score_ord=?,"
        "best_hand_exact=?,best_hand_ord=?,furthest_ante=?,furthest_round=?,"
        "log_format=?,defects=? WHERE run_id=?",
        tuple(meta[c] for c in cols) +
        (v_min, v_max, len(segs), n_events, won, won_ante,
         (result_row or {}).get("result"),
         1 if (result_row or {}).get("terminal") else (0 if result_row else None),
         (result_row or {}).get("at_state"), entered_endless, is_open,
         1 if ("run.end" not in saw) else 0,
         final_ante, final_round, hands_played, end_endless,
         frs_x, frs_o, bh_x, bh_o, furthest_ante, furthest_round,
         fmt, defects, run_id))

    seg_cols = ("run_id seg kind started_utc dollars_exact dollars_ord chips_exact "
                "chips_ord deck_baseline n_open n_baseline n_events").split()
    con.executemany(
        f"INSERT INTO run_segments({','.join(seg_cols)}) "
        f"VALUES ({','.join('?' * len(seg_cols))})",
        [tuple(s[c] for c in seg_cols) for s in segs])

    for rd in rounds:
        items = rd.pop("_items", [])
        cols = ",".join(rd.keys())
        con.execute(f"INSERT INTO rounds({cols}) VALUES ({','.join('?' * len(rd))})",
                    tuple(rd.values()))
        ruid = con.execute("SELECT round_uid FROM rounds WHERE run_id=? AND seg=? AND n_start=?",
                           (rd["run_id"], rd["seg"], rd["n_start"])).fetchone()[0]
        lo, hi = rd["n_start"], rd["n_end"] if rd["n_end"] is not None else 1 << 30
        con.execute("UPDATE hands SET round_uid=? WHERE run_id=? AND seg=? AND n BETWEEN ? AND ?",
                    (ruid, rd["run_id"], rd["seg"], lo, hi))
        con.execute("UPDATE discards SET round_uid=? WHERE run_id=? AND seg=? AND n BETWEEN ? AND ?",
                    (ruid, rd["run_id"], rd["seg"], lo, hi))
        if rd["n_end"] is not None:
            con.execute("UPDATE deck_samples SET round_uid=? WHERE run_id=? AND seg=? AND n=?",
                        (ruid, rd["run_id"], rd["seg"], rd["n_end"]))
            for i, it in enumerate(items):
                dx, do = num_pair(it.get("dollars"))
                con.execute("INSERT OR REPLACE INTO cashout_items VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (rd["run_id"], rd["seg"], rd["n_end"], i, ruid, rd["el"],
                             it.get("name") or "?", it.get("key"), as_int(it.get("disp")), dx, do))

    con.executemany("INSERT OR REPLACE INTO validations VALUES (?,?,?,?,?,?,?,?)", validations)


# ---------------------------------------------------------------- driver

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--logs", required=True, help="directory of *.jsonl run logs")
    ap.add_argument("--db", required=True)
    ap.add_argument("--rebuild", action="store_true",
                    help="re-derive every run from `events` without reading the logs")
    args = ap.parse_args(argv)

    con = connect(args.db)
    touched: set[str] = set()

    if not args.rebuild:
        paths = sorted(os.path.join(args.logs, f) for f in os.listdir(args.logs)
                       if f.endswith(".jsonl"))
        for p in paths:
            run_id, added = ingest_file(con, p)
            if run_id and added:
                touched.add(run_id)
            print(f"{os.path.basename(p):38s} +{added} events")
        con.commit()
    else:
        drop_derived(con)
        touched = {r[0] for r in con.execute("SELECT DISTINCT run_id FROM events")}

    for run_id in sorted(touched):
        derive_run(con, run_id)
    con.commit()
    con.execute("PRAGMA optimize")
    con.commit()

    n_ev = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    n_run = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    n_bad = con.execute("SELECT COUNT(*) FROM validations").fetchone()[0]
    print(f"\n{n_run} runs, {n_ev} events, {n_bad} validation failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
