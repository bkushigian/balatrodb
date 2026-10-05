"""Predict what Balatro's shop will offer, from the run's own random streams.

    python tools/shopsim.py predict              # the run in your save file
    python tools/shopsim.py predict --find Mime --rerolls 300
    python tools/shopsim.py streams --key cdt17 -n 10
    python tools/shopsim.py validate             # replay every logged shop
    python tools/shopsim.py calibrate            # re-learn the joker pool order

How it works is in balarng.py; what is specific to the shop is here.

THE JOKER POOL ORDER. A joker pick is `pool[math.random(#pool)]`, where pool
is the list of every joker of that rarity. Vanilla builds those lists with
pairs() over a hash table and never sorts them, so their order is whatever
LuaJIT's hashing gives -- not the `order` field, and not reproducible from
Python. It is learned instead: every joker the logs saw in a shop says "the
stream rolled index i and the game showed joker k", and the stream is known,
so each observation pins one slot. `calibrate` does that across every logged
run and writes joker_pools.json; with a few runs logged, every slot has one
clear winner. It only needs redoing if a mod adds or removes jokers.

WHAT CAN MAKE A PREDICTION WRONG. The streams are exact; the inputs around
them are the risk:
  * a joker you hold cannot be offered, and hitting one rerolls the pick on a
    `_resample` stream -- so buying, selling or destroying a joker changes
    what the picks after it land on. Predictions assume the board you have;
  * Lucky Cat, Stone Joker, Steel Joker, Golden Ticket and Glass Joker only
    exist while the deck holds the enhancement they read;
  * an Uncommon or Rare tag fills a slot itself, without using the shop's
    streams, so a shop with one of those tags has one card the streams skip;
  * save.jkr is written when you enter the shop, not on every reroll. A
    prediction counts from the save; `predict` lines it up with the run log
    when the log has moved on, and says so.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict

from balarng import (Streams, explain, load_centers, ordered, paths,
                     profile_dir, read_jkr)

HERE = os.path.dirname(os.path.abspath(__file__))
POOLS_JSON = os.path.join(HERE, "joker_pools.json")

# SMODS.poll_rarity: cumulative weights, compared with `<`. Vanilla tests
# `> 0.95` / `> 0.7` instead, which differs only on an exact tie.
RARITY_WEIGHTS = ((1, 0.7), (2, 0.25), (3, 0.05))
RARITY_NAME = {1: "Common", 2: "Uncommon", 3: "Rare", 4: "Legendary"}

# The shop's card types, in the order create_card_for_shop walks them. The
# order matters: the poll is compared against running sums.
SHOP_TYPES = ("Joker", "Tarot", "Planet", "Base", "Spectral")


# ─── the world the pools are culled against ──────────────────────────────

# What a redeemed voucher does to the shop (Card:apply_to_run). The products
# are written the way the game computes them, so the floats match.
VOUCHER_RATES = {
    "v_tarot_merchant": ("Tarot", 4 * (9.6 / 4)), "v_tarot_tycoon": ("Tarot", 4 * (32 / 4)),
    "v_planet_merchant": ("Planet", 4 * (9.6 / 4)), "v_planet_tycoon": ("Planet", 4 * (32 / 4)),
    "v_magic_trick": ("Base", 4),
}
VOUCHER_EDITION = {"v_hone": 2, "v_glow_up": 4}
VOUCHER_SLOTS = {"v_overstock_norm": 1, "v_overstock_plus": 1}


class World:
    """Everything get_current_pool consults, besides the streams."""

    def __init__(self, centers):
        self.centers = centers
        self.ante = 1
        self.used = set()               # G.GAME.used_jokers
        self.held = Counter()           # jokers + consumables actually owned
        self.pool_flags = set()
        self.banned = set()
        self.used_vouchers = set()
        self.enhancements = set()       # in the deck, for enhancement_gate
        self.hands_played = defaultdict(int)
        self.locked = set()             # jokers the profile has not unlocked
        self.rates = {"Joker": 20, "Tarot": 4, "Planet": 4, "Base": 0, "Spectral": 0}
        self.edition_rate = 1
        self.joker_max = 2
        self.modifiers = {}

    def redeem(self, voucher):
        self.used_vouchers.add(voucher)
        if voucher in VOUCHER_RATES:
            t, rate = VOUCHER_RATES[voucher]
            self.rates[t] = rate
        if voucher in VOUCHER_EDITION:
            self.edition_rate = VOUCHER_EDITION[voucher]
        self.joker_max += VOUCHER_SLOTS.get(voucher, 0)

    def showman(self):
        return self.held.get("j_ring_master", 0) > 0

    def available(self, c) -> bool:
        """The cull in get_current_pool, for one center."""
        key = c["key"]
        if key in self.used and not self.showman():
            return False
        if c.get("set") == "Joker" and key in self.locked and c.get("rarity") != 4:
            return False
        if c.get("set") == "Planet" and c.get("softlock") \
                and self.hands_played.get(c.get("hand_type"), 0) <= 0:
            return False
        gate = c.get("enhancement_gate")
        if gate and gate not in self.enhancements:
            return False
        if c.get("name") in ("Black Hole", "The Soul"):
            return False
        if c.get("no_pool_flag") and c["no_pool_flag"] in self.pool_flags:
            return False
        if c.get("yes_pool_flag") and c["yes_pool_flag"] not in self.pool_flags:
            return False
        return key not in self.banned

    def create(self, key):
        self.used.add(key)

    def remove(self, key):
        # Card:remove clears the key unless another copy is still held.
        if not self.held.get(key):
            self.used.discard(key)


# ─── pools ───────────────────────────────────────────────────────────────

def load_joker_pools(centers):
    if not os.path.exists(POOLS_JSON):
        raise SystemExit("no joker_pools.json -- run: python tools/shopsim.py calibrate")
    with open(POOLS_JSON) as fh:
        data = json.load(fh)
    pools = {int(r): keys for r, keys in data["pools"].items()}
    # The legendaries never come from a rarity roll in the shop, but the
    # Soul does pick from them; their order is learned the same way when the
    # logs have any, and otherwise falls back to `order`.
    pools.setdefault(4, [c["key"] for c in sorted(
        (c for c in centers.values() if c.get("rarity") == 4),
        key=lambda c: c["order"])])
    return pools


class Shop:
    """create_card_for_shop and friends, against one World and Streams."""

    def __init__(self, world: World, streams: Streams, joker_pools):
        self.w, self.s, self.jp = world, streams, joker_pools
        c = world.centers
        self.pools = {t: [x["key"] for x in ordered(c, t)]
                      for t in ("Tarot", "Planet", "Spectral", "Voucher", "Booster")}

    # -- one pick from a pool, with the resample loop -----------------------
    def pick(self, keys, pool_key):
        w, cs = self.w, self.w.centers
        avail = [k if w.available(cs[k]) else "UNAVAILABLE" for k in keys]
        if not any(k != "UNAVAILABLE" for k in avail):
            return None, 0
        got = self.s.element(avail, pool_key)
        it = 1
        while got == "UNAVAILABLE":
            it += 1
            got = self.s.element(avail, f"{pool_key}_resample{it}")
        return got, it - 1

    def rarity(self, append):
        u = self.s.random(f"rarity{self.w.ante}{append}")
        cum = 0.0
        for r, wgt in RARITY_WEIGHTS:
            cum += wgt
            if u < cum:
                return r
        return 3

    def edition(self, append):
        """poll_edition for a joker: 'edi'..append..ante. Vanilla rates."""
        u = self.s.random(f"edi{append}{self.w.ante}")
        er = self.w.edition_rate
        if u > 1 - 0.003:
            return "negative"
        if u > 1 - 0.006 * er:
            return "polychrome"
        if u > 1 - 0.02 * er:
            return "holo"
        if u > 1 - 0.04 * er:
            return "foil"
        return None

    def joker(self, append="sho", rarity=None):
        r = rarity or self.rarity(append)
        key, resamples = self.pick(self.jp[r], f"Joker{r}{append}{self.w.ante}")
        key = key or "j_joker"
        # create_card polls these for every shop/pack joker, whatever the
        # stake: the streams advance even when the stickers cannot apply.
        if append in ("sho", "buf"):
            self.s.random(("packetper" if append == "buf" else "etperpoll") + str(self.w.ante))
        ed = self.edition(append)
        return {"set": "Joker", "key": key, "rarity": r, "edition": ed,
                "resamples": resamples}

    def consumable(self, set_, append):
        key, resamples = self.pick(self.pools[set_], f"{set_}{append}{self.w.ante}")
        fallback = {"Tarot": "c_strength", "Planet": "c_pluto", "Spectral": "c_incantation"}
        return {"set": set_, "key": key or fallback[set_], "resamples": resamples}

    # -- the shop -------------------------------------------------------------
    def card_type(self):
        rates = self.w.rates
        total = sum(rates[t] for t in SHOP_TYPES)
        polled = self.s.random(f"cdt{self.w.ante}") * total
        check = 0
        for t in SHOP_TYPES:
            v = rates[t]
            if check < polled <= check + v:
                return t
            check += v
        return "Joker"

    def shop_card(self):
        t = self.card_type()
        if t == "Joker":
            card = self.joker("sho")
        elif t == "Base":
            card = {"set": "Base", "key": "c_base"}
        else:
            card = self.consumable(t, "sho")
        self.w.create(card["key"])
        return card

    def fill(self, n=None):
        return [self.shop_card() for _ in range(n or self.w.joker_max)]

    def reroll(self, current):
        for c in current:
            self.w.remove(c["key"])
        return self.fill()

    def buffoon_pack(self, size=2):
        cards = []
        for _ in range(size):
            c = self.joker("buf")
            self.w.create(c["key"])
            cards.append(c)
        for c in cards:
            self.w.remove(c["key"])
        return cards


# ─── reading a live run from the save ────────────────────────────────────

def world_from_save(save, centers, profile):
    G = save["GAME"]
    w = World(centers)
    w.ante = G["round_resets"]["ante"]
    w.used = {k for k, v in (G.get("used_jokers") or {}).items() if v}
    w.pool_flags = {k for k, v in (G.get("pool_flags") or {}).items() if v}
    w.banned = {k for k, v in (G.get("banned_keys") or {}).items() if v}
    w.used_vouchers = {k for k, v in (G.get("used_vouchers") or {}).items() if v}
    for name, h in (G.get("hands") or {}).items():
        if isinstance(h, dict):
            w.hands_played[name] = h.get("played") or 0
    w.rates = {"Joker": G.get("joker_rate", 20), "Tarot": G.get("tarot_rate", 4),
               "Planet": G.get("planet_rate", 4), "Base": G.get("playing_card_rate", 0),
               "Spectral": G.get("spectral_rate", 0) or 0}
    w.edition_rate = G.get("edition_rate", 1)
    w.joker_max = (G.get("shop") or {}).get("joker_max", 2)
    w.modifiers = G.get("modifiers") or {}
    areas = save.get("cardAreas") or {}
    for a in ("jokers", "consumeables"):
        for c in ((areas.get(a) or {}).get("cards") or {}).values():
            w.held[c["save_fields"]["center"]] += 1
    for a in ("deck", "hand", "discard", "play"):
        for c in ((areas.get(a) or {}).get("cards") or {}).values():
            w.enhancements.add(c["save_fields"]["center"])
    w.locked = locked_jokers(centers, profile)
    return w


def locked_jokers(centers, profile):
    """Jokers that start locked and that this profile has not unlocked."""
    default_locked = {k for k, c in centers.items()
                      if c.get("set") == "Joker" and c.get("unlocked") is False}
    try:
        meta = read_jkr(os.path.join(profile_dir(profile), "meta.jkr"))
    except OSError:
        return default_locked
    unlocked = {k for k, v in (meta.get("unlocked") or {}).items() if v}
    return default_locked - unlocked


def shop_cards_in_save(save):
    area = (save.get("cardAreas") or {}).get("shop_jokers") or {}
    return [{"key": c["save_fields"]["center"]} for c in (area.get("cards") or {}).values()]


# ─── the run log, for calibrating and checking ───────────────────────────

def logged_shop_cards(db, rid):
    """Every card the shop created in a run, in creation order, with the
    shop fill it belonged to and what was held at the time.

    A fill is the set of cards in one shop.offer event that were not in the
    one before it: on entering the shop, or on a reroll. "Not in the one
    before" rather than "never seen": sort_ids are not unique across a
    resume -- the restored cards take ids the game hands out again later --
    so a card can share an id with one seen hours earlier and still be new.
    """
    events = []
    for seg, n, ante, cid, set_, key, ed in db.execute(
            """SELECT seg, n, ante, card_id, set_, key, edition FROM cards
                WHERE run_id = ? AND event = 'shop.offer' AND role = 'cards'
                ORDER BY seg, n, pos""", (rid,)):
        events.append(("offer", seg, n, ante, cid, set_, (key, ed)))
    for seg, n, key in db.execute(
            "SELECT seg, n, key FROM joker_state WHERE run_id = ?", (rid,)):
        events.append(("board", seg, n, None, None, None, key))
    # Every way a joker arrives or leaves -- bought, taken from a pack,
    # made by Judgement, sold, eaten by Ceremonial Dagger -- is a joker.add
    # or joker.remove. Logs from before those existed only have the shop's
    # buys and sells to go on.
    adds = db.execute("""SELECT seg, n, event, key FROM cards WHERE run_id = ?
                          AND role = 'card' AND event IN ('joker.add', 'joker.remove')""",
                      (rid,)).fetchall()
    for seg, n, event, key in adds:
        events.append(("buy" if event == "joker.add" else "sell", seg, n, None, None, "Joker", key))
    sold = {(seg, key) for seg, key in db.execute(
        "SELECT seg, key FROM shop WHERE run_id = ? AND action = 'sell'", (rid,))}
    for seg, n, event, key in adds:
        # Gros Michel leaving any way but a sale went extinct, and from then
        # on Cavendish is in the pool and Gros Michel is not.
        if event == "joker.remove" and key == "j_gros_michel" and (seg, key) not in sold:
            events.append(("flag", seg, n, None, None, None, "gros_michel_extinct"))
    if not adds:
        for seg, n, action, key, set_ in db.execute(
                """SELECT seg, n, action, key, set_ FROM shop
                    WHERE run_id = ? AND action IN ('buy', 'sell')""", (rid,)):
            events.append((action, seg, n, None, None, set_, key))
    for seg, n, event, key in db.execute(
            """SELECT seg, n, event, key FROM cards WHERE run_id = ? AND role = 'card'
                AND event IN ('consumable.add', 'consumable.remove')""", (rid,)):
        events.append((event, seg, n, None, None, None, key))
    for seg, n, key in db.execute(
            """SELECT seg, n, key FROM consumable_uses
                WHERE run_id = ? AND substr(key, 1, 2) = 'v_'""", (rid,)):
        events.append(("voucher", seg, n, None, None, None, key))
    # The softlocked planets need their hand played once.
    for seg, n, hand in db.execute(
            "SELECT seg, n, hand FROM hands WHERE run_id = ?", (rid,)):
        events.append(("hand", seg, n, None, None, None, hand))
    # The enhancement-gated jokers need the enhancement in the deck: the
    # whole deck at every round end and at the start, and each change to a
    # card in between (a tarot used in the shop counts at once).
    for seg, n, event, role, key in db.execute(
            """SELECT seg, n, event, role, key FROM cards WHERE run_id = ?
                AND ((event IN ('round.end', 'run.baseline', 'run.final')
                      AND role IN ('deck', 'deck_cards'))
                  OR (event = 'card.modify' AND role = 'to')
                  OR (event = 'card.add' AND role = 'card'))""", (rid,)):
        events.append(("deck" if role in ("deck", "deck_cards") else "deckadd",
                       seg, n, None, None, event, key))
    events.sort(key=lambda e: (e[1], e[2]))

    fills, offer_at, this_offer, last_offer = [], None, set(), set()
    board, board_at, consum, vouchers = Counter(), None, Counter(), []
    played, deck, deck_at, flags = Counter(), set(), None, set()
    for kind, seg, n, ante, cid, set_, key in events:
        if kind == "board":
            if board_at != (seg, n):
                board, board_at = Counter(), (seg, n)
            board[key] += 1
        elif kind == "buy" and set_ == "Joker":
            board[key] += 1
        elif kind == "sell" and set_ == "Joker":
            board[key] -= 1
        elif kind == "voucher":
            vouchers.append(key)
        elif kind == "flag":
            flags.add(key)
        elif kind == "hand":
            played[key] += 1
        elif kind == "deck":
            if deck_at != (seg, n):
                deck, deck_at = set(), (seg, n)
            deck.add(key)
        elif kind == "deckadd":
            deck.add(key)
        elif kind == "consumable.add":
            consum[key] += 1
        elif kind == "consumable.remove":
            consum[key] -= 1
        elif kind == "offer":
            key, edition = key
            if offer_at != (seg, n):
                offer_at, last_offer, this_offer = (seg, n), this_offer, set()
            this_offer.add((seg, cid, key))
            if (seg, cid, key) in last_offer:
                continue
            if not fills or fills[-1]["at"] != (seg, n):
                held = +board + +consum
                fills.append({"at": (seg, n), "seg": seg, "ante": ante,
                              "held": held, "vouchers": list(vouchers),
                              "played": Counter(played), "deck": set(deck),
                              "flags": set(flags), "cards": []})
            fills[-1]["cards"].append({"set": set_, "key": key, "id": cid,
                                       "edition": edition})
    return fills


def runs_with_seeds(db):
    return db.execute("""SELECT run_id, seed FROM runs WHERE seed IS NOT NULL
                          ORDER BY started_ts""").fetchall()


def calibrate(db, centers):
    """Learn each rarity pool's order from what the logs saw."""
    sizes = Counter(c["rarity"] for c in centers.values()
                    if c.get("set") == "Joker" and c.get("rarity") in (1, 2, 3))
    votes = defaultdict(Counter)
    w = World(centers)
    for rid, seed in runs_with_seeds(db):
        by_ante = defaultdict(list)
        for f in logged_shop_cards(db, rid):
            by_ante[f["ante"]].append(f)
        for ante, fills in by_ante.items():
            s = Streams(seed)
            w.ante = ante
            shop = Shop(w, s, {})
            for f in fills:
                for c in f["cards"]:
                    t = shop.card_type()
                    if t != "Joker" or c["set"] != "Joker":
                        continue
                    r = shop.rarity("sho")
                    if centers.get(c["key"], {}).get("rarity") != r:
                        continue        # a tag's card; the streams skipped it
                    i = s.random(f"Joker{r}sho{ante}", sizes[r])
                    votes[(r, i)][c["key"]] += 1
    pools, report = {}, {}
    for r in (1, 2, 3):
        keys, agree, total = [], 0, 0
        for i in range(1, sizes[r] + 1):
            top = votes[(r, i)].most_common(1)
            keys.append(top[0][0] if top else None)
            agree += top[0][1] if top else 0
            total += sum(votes[(r, i)].values())
        pools[r] = keys
        report[r] = {"slots": sizes[r], "filled": sum(k is not None for k in keys),
                     "distinct": len({k for k in keys if k}), "agree": agree, "total": total}
    return pools, report


def _matches(got, c):
    return got["key"] == c["key"] or (got["set"] == "Base" and c["set"] in ("Default", "Enhanced"))


def _lookahead(shop, upcoming, k=8):
    """How many of the next k logged cards the shop would reproduce from its
    current state -- on copies, so nothing is used up."""
    w = shop.w
    trial = Shop(w, shop.s.copy(), shop.jp)
    used = set(w.used)
    hits = 0
    for c in upcoming[:k]:
        got = trial.shop_card()
        hits += _matches(got, c)
        w.used.discard(got["key"])
        w.used.add(c["key"])
    w.used = used
    return hits


def validate(db, centers, pools, only=None, verbose=False):
    """Replay every logged shop from the seed and count what matches.

    The streams are exact; what the replay has to work out is everything the
    log does not say outright, and it does that by trying each explanation
    and keeping the one that reproduces the cards that follow:

      * a resumed run reloads its streams from the save, which was written
        on entering the shop -- so after a resume the streams rewind to the
        start of some earlier fill, and the same cards come round again;
      * an Uncommon or Rare tag puts a joker in the shop without touching
        the streams, so a logged card can be one the streams never made.
    """
    total = Counter()
    per_run = []
    for rid, seed in runs_with_seeds(db):
        if only and only not in rid:
            continue
        by_ante = defaultdict(list)
        for f in logged_shop_cards(db, rid):
            by_ante[f["ante"]].append(f)
        stats = Counter()
        deck = _deck(db, rid)
        for ante in sorted(by_ante):
            w = World(centers)
            w.ante = ante
            w.rates["Spectral"] = 2 if deck == "b_ghost" else 0
            shop = Shop(w, Streams(seed), pools)
            fills = by_ante[ante]
            flat = [c for f in fills for c in f["cards"]]
            pos = 0
            saves = [shop.s.copy()]        # the stream state at each fill's start
            prev_seg = fills[0]["seg"]
            for f in fills:
                for v in f["vouchers"]:
                    if v not in w.used_vouchers:
                        w.redeem(v)
                w.held = f["held"]
                w.used = set(k for k, v in w.held.items() if v > 0)
                w.hands_played = f["played"]
                w.enhancements = f["deck"]
                w.pool_flags = f["flags"]
                if f["seg"] != prev_seg:
                    prev_seg = f["seg"]
                    stats["resumes"] += 1
                    # Resumed inside the shop, the game puts the saved shop
                    # back on screen. The log sees those cards again, but
                    # nothing drew them.
                    keys = sorted(c["key"] for c in f["cards"])
                    restored = any(sorted(c["key"] for c in g["cards"]) == keys
                                   for g in fills[:fills.index(f)])
                    after = pos + (len(f["cards"]) if restored else 0)
                    # And the streams came back from the save. Where the run
                    # was saved after the last fill -- quit outside the shop
                    # -- nothing rewinds, so that comes first and wins a tie.
                    best = max([shop.s] + saves[::-1], key=lambda st: _lookahead(
                        Shop(w, st, pools), flat[after:]))
                    shop.s = best.copy()
                    if restored:
                        stats["restored"] += len(f["cards"])
                        pos = after
                        continue
                prev_seg = f["seg"]
                saves.append(shop.s.copy())
                for c in f["cards"]:
                    snap = shop.s.copy()
                    got = shop.shop_card()
                    stats["cards"] += 1
                    if not _matches(got, c) and centers.get(c["key"], {}).get("set") == "Joker":
                        # Was it a tag's joker? Compare the two histories on
                        # the cards after it.
                        w.used.discard(got["key"])
                        w.used.add(c["key"])
                        as_drawn = _lookahead(shop, flat[pos + 1:])
                        tagged = Shop(w, snap, pools)
                        if _lookahead(tagged, flat[pos + 1:]) > as_drawn:
                            shop.s = snap
                            stats["tag"] += 1
                            pos += 1
                            continue
                    logged_set = centers.get(c["key"], {}).get("set") or c["set"]
                    stats["type"] += got["set"] == logged_set or (
                        got["set"] == "Base" and logged_set in ("Default", "Enhanced"))
                    if _matches(got, c):
                        stats["exact"] += 1
                    elif verbose:
                        print(f"  {rid[:24]} ante {ante}: logged {c['key']}, predicted {got['key']}"
                              f" (resamples {got.get('resamples')})")
                    if c["set"] == "Joker" and got["set"] == "Joker":
                        stats["jokers"] += 1
                        stats["joker_exact"] += got["key"] == c["key"]
                        want = (c.get("edition") or "").replace("e_", "") or None
                        stats["edition"] += got.get("edition") == want
                    # Carry on from what actually happened, not the guess.
                    w.used.discard(got["key"])
                    w.create(c["key"])
                    pos += 1
        per_run.append((rid, seed, stats))
        total.update(stats)
    return total, per_run


def _deck(db, rid):
    r = db.execute("SELECT deck_key FROM runs WHERE run_id = ?", (rid,)).fetchone()
    return r[0] if r else None


# ─── the commands ────────────────────────────────────────────────────────

def name_of(centers, key):
    return (centers.get(key) or {}).get("name") or key


def resolve_target(centers, text):
    if not text:
        return None
    t = text.lower().strip()
    for k, c in centers.items():
        if k.lower() == t or (c.get("name") or "").lower() == t:
            return k
    raise SystemExit(f"no card called {text!r}")


def card_label(centers, c):
    ed = f" ({c['edition']})" if c.get("edition") else ""
    rar = f" [{RARITY_NAME[c['rarity']]}]" if c.get("rarity") else ""
    return f"{name_of(centers, c['key'])}{rar}{ed}"


def sync_with_log(shop, w, seed, current):
    """Carry the save forward over the rerolls the log has seen since.

    save.jkr is written on entering the shop and not on a reroll, so in the
    middle of a shop it is behind by however many rerolls you have made. The
    log is not: find the save's shop in it, then replay each later reroll and
    check it lands on what the log saw. Stops at the first disagreement
    rather than guessing past it.
    """
    try:
        db = sqlite3.connect(paths.db_path())
        row = db.execute("""SELECT run_id FROM runs WHERE seed = ?
                             ORDER BY started_ts DESC LIMIT 1""", (seed,)).fetchone()
    except sqlite3.Error:
        return current, 0, "no run database to line the save up with"
    if not row:
        return current, 0, "this run is not in the run log"
    fills = [f for f in logged_shop_cards(db, row[0]) if f["ante"] == w.ante]
    if not fills:
        return current, 0, None
    fills = [f for f in fills if f["seg"] == fills[-1]["seg"]]
    have = sorted(c["key"] for c in current)
    start = max((i for i, f in enumerate(fills)
                 if sorted(c["key"] for c in f["cards"]) == have), default=None)
    if start is None:
        return current, 0, "the save's shop is not in the log; predicting from the save alone"
    n = 0
    for f in fills[start + 1:]:
        trial_s, trial_used, held = shop.s.copy(), set(w.used), w.held
        w.held = f["held"] or w.held
        got = shop.reroll(current)
        if sorted(c["key"] for c in got) != sorted(c["key"] for c in f["cards"]):
            shop.s, w.used, w.held = trial_s, trial_used, held
            return current, n, (f"the log and the simulation part ways after {n} "
                                "reroll(s); predicting from there")
        current, n = got, n + 1
    return current, n, None


def cmd_predict(a):
    centers = load_centers()
    pools = load_joker_pools(centers)
    path = a.save or os.path.join(profile_dir(a.profile), "save.jkr")
    if not os.path.exists(path):
        raise SystemExit(f"no save at {path} -- is a run in progress?")
    save = read_jkr(path)
    G = save["GAME"]
    pr = G["pseudorandom"]
    streams = Streams(pr["seed"], pr)
    w = world_from_save(save, centers, a.profile)
    shop = Shop(w, streams, pools)
    target = resolve_target(centers, a.find)

    current = shop_cards_in_save(save)
    in_shop = bool(current)
    synced, note = 0, None
    if in_shop and not a.no_log:
        current, synced, note = sync_with_log(shop, w, pr["seed"], current)
    print(f"seed {pr['seed']}  ante {w.ante}  "
          f"{'in the shop' if in_shop else 'not in a shop'}  "
          f"holding {', '.join(name_of(centers, k) for k in sorted(+w.held)) or 'nothing'}")
    if not in_shop:
        print("The next shop's first fill is reroll 0 below.")
        current = []
    else:
        print("Now in the shop: " + ", ".join(name_of(centers, c["key"]) for c in current))
        if synced:
            print(f"(the save was {synced} reroll{'s' if synced != 1 else ''} behind; "
                  f"caught up from the run log)")
    if note:
        print(f"note: {note}")

    cr = G.get("current_round") or {}
    cost = cr.get("reroll_cost", 5)
    free = cr.get("free_rerolls", 0) or 0
    for _ in range(synced):
        if free > 0:
            free -= 1
        else:
            cost += 1
    spent, found = 0, []
    for i in range(0 if not in_shop else 1, a.rerolls + 1):
        if i == 0:
            cards = shop.fill()
        else:
            cards = shop.reroll(current)
            if free > 0:
                free -= 1
            else:
                spent += cost
                cost += 1
        current = cards
        hit = target and any(c["key"] == target for c in cards)
        if hit:
            found.append((i, spent))
        if a.show and i <= a.show or hit:
            mark = "  <--" if hit else ""
            print(f"  reroll {i:>4} (${spent:>5} spent): "
                  + " | ".join(card_label(centers, c) for c in cards) + mark)
        if hit and not a.all:
            break
    if target:
        if found:
            i, sp = found[0]
            print(f"\n{name_of(centers, target)} appears after {i} reroll{'s' if i != 1 else ''}"
                  f" this ante, about ${sp} in rerolls -- assuming you buy, sell and"
                  f" destroy no jokers until then.")
        else:
            print(f"\n{name_of(centers, target)} does not appear in the next {a.rerolls} rerolls"
                  f" of ante {w.ante}.")
        packs(centers, pools, w, streams, target)
        ahead(centers, pools, w, pr["seed"], target, a.antes)


def packs(centers, pools, world, streams, target, limit=400):
    """Where the target falls among this ante's Buffoon-pack jokers.

    Buffoon packs draw from their own streams ('buf'), separate from the
    shop's, so opening one does not move the shop's sequence and rerolling
    does not move the packs'. Counted in jokers: a normal pack shows 2, a
    jumbo or mega 4, in order.
    """
    w = World(centers)
    w.__dict__.update({k: v for k, v in world.__dict__.items()})
    w.used = set(world.used)
    shop = Shop(w, streams.copy(), pools)
    for i in range(1, limit + 1):
        c = shop.joker("buf")
        if c["key"] == target:
            print(f"In Buffoon packs this ante: joker {i} of those packs' draws"
                  f" (a normal pack shows 2, a jumbo or mega 4).")
            return
    print(f"In Buffoon packs this ante: not within the next {limit} pack jokers.")


def ahead(centers, pools, world, seed, target, antes):
    """Where the target falls in the antes after this one.

    Each ante's shop streams start fresh, so this is "the Nth card the shop
    creates in ante X", counted across all of that ante's shops: two per
    shop visit and two per reroll.
    """
    if antes <= 0:
        return
    print(f"\nIn later antes (counting every card the shop creates that ante):")
    for ante in range(world.ante + 1, world.ante + 1 + antes):
        w = World(centers)
        w.__dict__.update({k: v for k, v in world.__dict__.items()
                           if k not in ("ante", "used")})
        w.ante = ante
        w.used = {k for k, v in world.held.items() if v > 0}
        shop = Shop(w, Streams(seed), pools)
        cur, n = [], None
        for fill in range(2000):
            cur = shop.reroll(cur)
            if any(c["key"] == target for c in cur):
                n = fill
                break
        where = (f"in fill {n + 1} (the shop's first fill is 1; each reroll is one more)"
                 if n is not None else "not within 2000 fills")
        print(f"  ante {ante}: {where}")


def cmd_streams(a):
    if a.save or os.path.exists(os.path.join(profile_dir(a.profile), "save.jkr")):
        save = read_jkr(a.save or os.path.join(profile_dir(a.profile), "save.jkr"))
        pr = save["GAME"]["pseudorandom"]
        s = Streams(a.seed or pr["seed"], pr if not a.seed else None)
    elif a.seed:
        s = Streams(a.seed)
    else:
        raise SystemExit("no save and no --seed")
    if not a.key:
        keys = sorted(s.state)
        print(f"seed {s.seed}: {len(keys)} streams in use")
        for k in keys:
            print(f"  {k:<32} {s.state[k]:.13f}" + (f"  {explain(k)}" if a.explain else ""))
        return
    print(f"seed {s.seed}  stream {a.key}  "
          f"({'resuming' if a.key in s.state else 'fresh'})  {explain(a.key)}")
    for i, seed in enumerate(s.peek(a.key, a.n), 1):
        s.rng.randomseed(seed)
        print(f"  {i:>3}  seed {seed:.13f}  math.random() = {s.rng.random():.6f}")


def cmd_calibrate(a):
    centers = load_centers()
    db = sqlite3.connect(paths.db_path())
    pools, report = calibrate(db, centers)
    for r, rep in report.items():
        print(f"{RARITY_NAME[r]:>9}: {rep['filled']}/{rep['slots']} slots seen, "
              f"{rep['distinct']} distinct, {rep['agree']}/{rep['total']} votes agree")
    if any(rep["filled"] < rep["slots"] or rep["distinct"] < rep["filled"]
           for rep in report.values()):
        print("Not every slot is settled; play more runs before trusting this.")
    with open(POOLS_JSON, "w") as fh:
        json.dump({"note": "Learned by tools/shopsim.py calibrate from the run logs: "
                           "position i of each list is what math.random(#pool) = i "
                           "picks for that rarity.",
                   "pools": {str(r): keys for r, keys in pools.items()}}, fh, indent=1)
    print(f"wrote {POOLS_JSON}")


def cmd_validate(a):
    centers = load_centers()
    pools = load_joker_pools(centers)
    db = sqlite3.connect(paths.db_path())
    total, per_run = validate(db, centers, pools, a.run, a.verbose)
    for rid, seed, st in per_run:
        if st["cards"]:
            print(f"{rid[:26]}  {st['cards']:>5} cards  type {st['type']:>5}  "
                  f"exact {st['exact']:>5}  jokers {st['joker_exact']}/{st['jokers']}"
                  + (f"  tag cards {st['tag']}" if st["tag"] else "")
                  + (f"  resumes {st['resumes']}" if st["resumes"] else ""))
    c = total["cards"] or 1
    print(f"\nall runs: {total['cards']} shop cards; type right {total['type'] / c:.2%}, "
          f"card right {total['exact'] / c:.2%}; "
          f"jokers right {total['joker_exact']}/{total['jokers']}, "
          f"their editions right {total['edition']}/{total['jokers']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("predict", help="what the shop will offer next")
    p.add_argument("--find", help="a card to look for, by name or key")
    p.add_argument("--rerolls", type=int, default=200)
    p.add_argument("--show", type=int, default=10, help="print this many rerolls")
    p.add_argument("--all", action="store_true", help="keep going past the first hit")
    p.add_argument("--antes", type=int, default=3, help="also look this many antes ahead")
    p.add_argument("--profile", default="1")
    p.add_argument("--save", help="a save.jkr to read instead of the profile's")
    p.add_argument("--no-log", action="store_true",
                   help="do not catch the save up from the run log")
    p.set_defaults(fn=cmd_predict)
    p = sub.add_parser("streams", help="peek at raw streams")
    p.add_argument("--key")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--seed", help="start fresh from this seed instead of the save")
    p.add_argument("--explain", action="store_true", help="say what each stream is for")
    p.add_argument("--profile", default="1")
    p.add_argument("--save")
    p.set_defaults(fn=cmd_streams)
    p = sub.add_parser("validate", help="replay logged shops and score the model")
    p.add_argument("--run", help="only runs whose id contains this")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("calibrate", help="learn the joker pool order from the logs")
    p.set_defaults(fn=cmd_calibrate)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
