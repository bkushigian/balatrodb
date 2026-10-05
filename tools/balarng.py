"""Balatro's random number generation, reproduced outside the game.

Every random thing in a Balatro run comes from one mechanism:

    pseudoseed(key)   advances a named stream -- 'cdt17', 'Joker2sho17',
                      'edisho17' -- by one step and returns a float;
    math.randomseed   is seeded with that float;
    math.random       is then read once (or a few times) for the outcome.

A stream's first value is a hash of its key and the run's seed, and each
step after that is a fixed affine map, so a stream's whole future is a pure
function of (seed, key, how many times it has been used). That is what makes
the shop predictable: the shop does not draw from one shared generator, it
draws the Nth value of named streams, and the save file records exactly where
each stream has got to.

The arithmetic is IEEE double throughout, which Python floats are, and the
13-decimal rounding the game does by formatting a string is reproduced by
formatting a string. math.random is LuaJIT's Tausworthe generator (TW223),
ported from lib_math.c; tests/test_balarng.py checks it against LuaJIT itself.

Stdlib only, like everything else in this repo.
"""
from __future__ import annotations

import math
import os
import re
import struct
import sys
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "ingest"))
import paths  # noqa: E402

M64 = (1 << 64) - 1


# ─── LuaJIT's math.random ─────────────────────────────────────────────────
# Combined Tausworthe generator, period 2^223. Four 64-bit words, each a
# shift register with its own (k, q, s); the output is their XOR, with the
# top 12 bits forced to make a double in [1, 2).

_TW = ((0, 63, 31, 18), (1, 58, 19, 28), (2, 55, 24, 7), (3, 47, 21, 8))


def _d2u(d: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", d))[0]


def _u2d(u: int) -> float:
    return struct.unpack("<d", struct.pack("<Q", u))[0]


class LuaRandom:
    """math.randomseed / math.random, as LuaJIT 2.1 implements them."""

    def __init__(self, seed: float = 0.0):
        self.gen = [0, 0, 0, 0]
        self.randomseed(seed)

    def _step(self) -> int:
        r = 0
        gen = self.gen
        for i, k, q, s in _TW:
            z = gen[i]
            z = ((((z << q) & M64) ^ z) >> (k - s)) ^ (((z & ((M64 << (64 - k)) & M64)) << s) & M64)
            r ^= z
            gen[i] = z
        return (r & 0x000FFFFFFFFFFFFF) | 0x3FF0000000000000

    def randomseed(self, d: float) -> None:
        r = 0x11090601          # 64 - k[i], as four 8-bit constants
        for i in range(4):
            m = 1 << (r & 255)
            r >>= 8
            d = d * 3.14159265358979323846 + 2.7182818284590452354
            u = _d2u(d)
            if u < m:           # ensure the top k bits are not all zero
                u += m
            self.gen[i] = u
        for _ in range(10):
            self._step()

    def random(self, lo: int | None = None, hi: int | None = None):
        d = _u2d(self._step()) - 1.0
        if lo is None:
            return d
        if hi is None:
            return int(math.floor(d * lo)) + 1
        return int(math.floor(d * (hi - lo + 1))) + lo


# ─── the game's streams ───────────────────────────────────────────────────

def pseudohash(s: str) -> float:
    """misc_functions.lua: pseudohash. Walks the string backwards."""
    num = 1.0
    b = s.encode("latin-1")
    for i in range(len(b), 0, -1):
        num = ((1.1239285023 / num) * b[i - 1] * math.pi + math.pi * i) % 1
    return num


def _advance(v: float) -> float:
    # math.abs(tonumber(string.format("%.13f", (2.134453429141 + v*1.72431234) % 1)))
    return abs(float("%.13f" % ((2.134453429141 + v * 1.72431234) % 1)))


class Streams:
    """G.GAME.pseudorandom: one value per named stream, plus the seed.

    `state` is the table itself, exactly as the save file stores it -- so a
    Streams built from a save carries on from where the game left off, and
    one built from a bare seed starts the run from scratch.
    """

    def __init__(self, seed: str, state: dict | None = None):
        self.seed = seed
        self.hashed_seed = pseudohash(seed)
        self.state = dict(state or {})
        self.state.pop("seed", None)
        self.state.pop("hashed_seed", None)
        self.rng = LuaRandom()

    def copy(self) -> "Streams":
        s = Streams.__new__(Streams)
        s.seed, s.hashed_seed = self.seed, self.hashed_seed
        s.state = dict(self.state)
        s.rng = LuaRandom()
        return s

    def pseudoseed(self, key: str) -> float:
        v = self.state.get(key)
        if v is None:
            v = pseudohash(key + self.seed)
        v = _advance(v)
        self.state[key] = v
        return (v + self.hashed_seed) / 2

    def peek(self, key: str, n: int = 1) -> list[float]:
        """The next n seeds of a stream, without using them up."""
        v = self.state.get(key)
        if v is None:
            v = pseudohash(key + self.seed)
        out = []
        for _ in range(n):
            v = _advance(v)
            out.append((v + self.hashed_seed) / 2)
        return out

    def random(self, key, lo=None, hi=None):
        """pseudorandom(key[, lo, hi]): seed math.random from the stream."""
        seed = self.pseudoseed(key) if isinstance(key, str) else key
        self.rng.randomseed(seed)
        return self.rng.random(lo, hi)

    def element(self, items: list, key: str):
        """pseudorandom_element over an array: math.random(#t) indexes it.

        Arrays only. A pool is an array of keys (or 'UNAVAILABLE'), and the
        game sorts an array's entries by index, so the index is the order.
        """
        self.rng.randomseed(self.pseudoseed(key))
        return items[self.rng.random(len(items)) - 1]

    def element_sorted_keys(self, keys: list, key: str):
        """pseudorandom_element over a dict: keys are sorted as strings."""
        keys = sorted(keys)
        self.rng.randomseed(self.pseudoseed(key))
        return keys[self.rng.random(len(keys)) - 1]


# ─── the save file ────────────────────────────────────────────────────────
# save.jkr is raw DEFLATE around a Lua chunk `return {...}`: a table literal
# of strings, numbers, booleans and nested tables, nothing executable.

_TOKEN = re.compile(r"""
    \s*(?:
      (?P<open>\{) | (?P<close>\}) | (?P<eq>=) | (?P<comma>[,;]) |
      \[(?P<skey>"(?:[^"\\]|\\.)*")\] | \[(?P<nkey>-?[0-9.eE+\-]+)\] |
      (?P<str>"(?:[^"\\]|\\.)*") |
      (?P<num>-?(?:inf|nan|[0-9][0-9.eE+\-]*|\.[0-9][0-9eE+\-]*)) |
      (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    )""", re.X)

_ESC = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'"}


def _unquote(s: str) -> str:
    body = s[1:-1]
    if "\\" not in body:
        return body
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            n = body[i + 1]
            if n.isdigit():
                j = i + 1
                while j < len(body) and j < i + 4 and body[j].isdigit():
                    j += 1
                out.append(chr(int(body[i + 1:j])))
                i = j
                continue
            out.append(_ESC.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _num(s: str):
    if s in ("inf", "-inf"):
        return float(s)
    if s in ("nan", "-nan"):
        return float("nan")
    f = float(s)
    return int(f) if f.is_integer() and "." not in s and "e" not in s.lower() else f


def parse_lua_table(src: str):
    """Parse `return {...}` into dicts. Array parts come back as dicts keyed
    by integer, which is how the game indexes them anyway."""
    src = src.strip()
    if src.startswith("return"):
        src = src[len("return"):]
    toks = []
    pos = 0
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m or m.end() == pos:
            if src[pos:].strip() == "":
                break
            raise ValueError(f"cannot parse save at {pos}: {src[pos:pos + 40]!r}")
        toks.append((m.lastgroup, m.group(m.lastgroup)))
        pos = m.end()
    it = iter(toks)

    def value(tok):
        kind, v = tok
        if kind == "open":
            return table()
        if kind == "str":
            return _unquote(v)
        if kind == "num":
            return _num(v)
        if kind == "word":
            return {"true": True, "false": False, "nil": None}.get(v, v)
        raise ValueError(f"unexpected {kind} {v!r}")

    def table():
        t, n = {}, 1
        for tok in it:
            kind, v = tok
            if kind == "close":
                return t
            if kind == "comma":
                continue
            if kind in ("skey", "nkey"):
                k = _unquote(v) if kind == "skey" else _num(v)
                assert next(it)[0] == "eq"
                t[k] = value(next(it))
            else:
                t[n] = value(tok)
                n += 1
        return t

    return value(next(it))


def read_jkr(path: str):
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        text = zlib.decompress(raw, -15)
    except zlib.error:
        text = raw              # an uncompressed save, as some debug builds write
    return parse_lua_table(text.decode("latin-1"))


def profile_dir(profile: int | str = 1) -> str:
    return os.path.join(paths.save_dir(), str(profile))


# ─── the game's own data ──────────────────────────────────────────────────
# Read from game.lua inside the game archive -- the same file the asset
# extractor reads -- so the pools are the shipped ones, not a copy that
# drifts. Lovely's dump of the patched file is preferred when it exists: it
# is what the game actually ran.

_CENTER = re.compile(r"^\s*([a-z]+_[A-Za-z0-9_]+)\s*=\s*\{(.*)\},?\s*(?:--.*)?$", re.M)


def _field(body: str, name: str):
    m = re.search(r"(?<![A-Za-z_])" + name + r"\s*=\s*(\"[^\"]*\"|'[^']*'|[^,{}]+)", body)
    if not m:
        return None
    v = m.group(1).strip()
    if not v:
        return None
    if v[0] in "\"'":
        return v[1:-1]
    if v in ("true", "false"):
        return v == "true"
    try:
        return _num(v)
    except ValueError:
        return v


def game_lua_source() -> str:
    dump = os.path.join(paths.save_dir(), "Mods", "lovely", "dump", "game.lua")
    if os.path.exists(dump):
        with open(dump, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    archive = paths.game_archive(paths.steam_game_dir())
    if not archive:
        raise SystemExit("cannot find the game: no Lovely dump and no game archive")
    with zipfile.ZipFile(archive) as z:
        return z.read("game.lua").decode("utf-8", "replace")


def load_centers(src: str | None = None) -> dict:
    """Every P_CENTERS entry: key -> {set, order, rarity, name, ...}."""
    src = src or game_lua_source()
    start = src.find("self.P_CENTERS = {")
    end = src.find("\n    }", start)
    out = {}
    for m in _CENTER.finditer(src, start, end):
        key, body = m.group(1), m.group(2)
        c = {"key": key}
        for f in ("set", "order", "rarity", "name", "unlocked", "cost",
                  "enhancement_gate", "yes_pool_flag", "no_pool_flag",
                  "softlock", "hand_type", "kind", "weight", "requires"):
            v = _field(body, f)
            if v is not None:
                c[f] = v
        rq = re.search(r"requires\s*=\s*\{([^}]*)\}", body)
        if rq:
            c["requires"] = re.findall(r"'([^']+)'|\"([^\"]+)\"", rq.group(1))
            c["requires"] = [a or b for a, b in c["requires"]]
        out[key] = c
    return out


def load_tags(src: str | None = None) -> dict:
    src = src or game_lua_source()
    start = src.find("self.P_TAGS = {")
    end = src.find("\n    }", start)
    out = {}
    for m in _CENTER.finditer(src, start, end):
        key, body = m.group(1), m.group(2)
        out[key] = {"key": key, "order": _field(body, "order"),
                    "min_ante": _field(body, "min_ante"),
                    "requires": _field(body, "requires"),
                    "name": _field(body, "name")}
    return out


def ordered(centers: dict, set_: str) -> list[dict]:
    return sorted((c for c in centers.values() if c.get("set") == set_),
                  key=lambda c: c.get("order") or 0)


# ─── what each stream is for ──────────────────────────────────────────────
# Read off the game's source (and Steamodded's, which this install runs).
# A key is a family name plus, usually, an "append" saying who is drawing --
# sho the shop, buf a Buffoon pack, ar1 an Arcana pack, pl1 a Celestial,
# spe a Spectral pack, sta a Standard pack, rif Riff-raff, jud Judgement,
# wra Wraith, uta/rta the Uncommon/Rare tags -- and the ante, so that most
# streams start over each ante. The ones marked [checked] are reproduced by
# tools/shopsim.py and replayed against the run logs.

STREAM_FAMILIES = [
    (r"cdt\d+", "[checked] shop slot's card type: joker / tarot / planet / playing card / spectral"),
    (r"rarity\d+\w*", "[checked] a joker's rarity (common < .70 <= uncommon < .95 <= rare); append says who drew it"),
    (r"Joker[1-4]\w*?\d*_resample\d+", "[checked] re-pick after the first pick hit a joker that is not available"),
    (r"Joker4\w*", "legendary pick (The Soul); no ante, so the same order all run"),
    (r"Joker[1-3]\w*", "[checked] which joker, from that rarity's list; append says who drew it"),
    (r"(Tarot|Planet|Spectral|Tarot_Planet)\w*?\d*_resample\d+", "[checked] re-pick of a consumable that is not available"),
    (r"(Tarot|Planet|Spectral|Tarot_Planet)\w*", "[checked] which consumable; sho the shop, ar1/pl1/spe packs, others cards"),
    (r"edi\w*", "[checked] a joker's edition: edisho shop, edibuf pack, others"),
    (r"etperpoll\d+|packetper\d+", "[checked] eternal / perishable sticker roll (polled at every stake)"),
    (r"ssjr\d+|packssjr\d+", "rental sticker roll"),
    (r"soul_smods_\w+", "Steamodded's roll for modded legendary consumables"),
    (r"soul_\w+", "The Soul / Black Hole replacing a pack card (> 0.997)"),
    (r"shop_pack\d+", "which booster packs the shop stocks"),
    (r"Voucher(_fromtag)?\d*(_resample\d+)?", "which voucher the shop offers (or a Voucher tag gives)"),
    (r"Tag\w*", "which tag a skippable blind offers"),
    (r"boss", "which boss blind comes next"),
    (r"front\w*|stdset\d+|stdseal\d+|stdsealtype\d+|standard_edition\d+|scc\w+",
     "playing cards: suit/rank, enhancement, seal, edition"),
    (r"nr\d+|shuffle|cashout\d+|aajk", "deck or joker shuffles"),
    (r"cas\d+|anc\d+|idol\d+|mail\d+|to_do", "this round's target for Castle / Ancient / Idol / Mail-in Rebate / To Do List"),
    (r"halu\d+", "Hallucination's chance of a tarot"),
    (r"lucky_mult|lucky_money|wheel_of_fortune|8ball|business|bloodstone|parking|space|"
     r"cavendish|gros_michel|misprint|glass|wheel|sigil|ouija|familiar_create|grim_create|"
     r"incantation_create|immolate|madness|perkeo|invisible|erratic|ankh_choice|random_destroy|"
     r"flipped_card|crimson_heart|cerulean_bell|hook|marb_fr|cert_fr|certsl|spe_card|strength|"
     r"weakness|orbital|omen_globe|illusion|edition_deck|\w+",
     "a card's or blind's own effect (chances, targets, created cards)"),
]


def explain(key: str) -> str:
    for pat, what in STREAM_FAMILIES:
        if re.fullmatch(pat, key):
            return what
    return ""
