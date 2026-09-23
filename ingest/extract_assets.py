"""Pull Balatro's sprite atlases and card positions out of the game.

    python ingest/extract_assets.py [--game DIR]

Balatro.exe is a LÖVE archive -- a zip with an executable stub -- so the
textures and the lua that positions them can be read straight out of it.

Writes into ingest/web/assets/ :
    Jokers.png, Enhancers.png, chips.png   the 2x atlases
    sprites.json                           key -> {atlas, x, y} plus cell sizes

Those outputs are the game's own copyrighted art, so they are gitignored and
regenerated locally rather than committed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "web", "assets")

DEFAULT_GAME = r"C:\Program Files (x86)\Steam\steamapps\common\Balatro"

# Which atlas each key prefix is drawn from, and the 2x cell size in pixels.
# Balatro's card is 71x95 at 1x; chips are square.
ATLASES = {
    "j": ("Jokers.png", 142, 190),      # jokers
    "b": ("Enhancers.png", 142, 190),   # deck backs
    "stake": ("chips.png", 58, 58),     # stake chips
    "c": ("Tarots.png", 142, 190),      # consumables
    "m": ("Enhancers.png", 142, 190),   # card enhancements: bonus, lucky, ...
    "card": ("8BitDeck.png", 142, 190),  # playing card faces, 13 ranks x 4 suits
}

# c_base is the plain white card back every playing card is drawn on. It is
# the one c_* key that is not a consumable, so it does not follow the prefix.
ATLAS_OVERRIDES = {"c_base": ("Enhancers.png", 142, 190)}


def png_size(data: bytes) -> tuple[int, int]:
    return struct.unpack(">II", data[16:24])


def parse_centers(src: str) -> dict[str, tuple[int, int]]:
    """key -> (x, y) atlas cell, from the centre tables in game.lua."""
    entry = re.compile(
        r"(\b(?:j|b|c|m)_[a-z0-9_]+|stake_[a-z]+)\s*=\s*\{(.*?)\},?\s*"
        r"(?=\n|\b(?:j|b|c|m)_[a-z0-9_]+\s*=)", re.S)
    pos = re.compile(r"pos\s*=\s*\{\s*x\s*=\s*(\d+)\s*,\s*y\s*=\s*(\d+)")
    out: dict[str, tuple[int, int]] = {}
    for key, body in entry.findall(src):
        m = pos.search(body)
        if m and key not in out:
            out[key] = (int(m.group(1)), int(m.group(2)))
    return out


def parse_names(z: zipfile.ZipFile) -> dict[str, str]:
    """key -> display name, from the game's own English localization.

    The logs record keys, not names, and deriving a name from the key gives
    "ride the bus" and "mail in rebate". The game already has the real strings,
    so use those rather than inventing a second, worse set.
    """
    try:
        src = z.read("localization/en-us.lua").decode("utf-8", "replace")
    except KeyError:
        return {}
    # `key={ name="..."` -- the name is always the entry's first field.
    pat = re.compile(r"((?:j|b|c|m|p|v|tag|bl)_[a-z0-9_]+|stake_[a-z]+)\s*=\s*\{"
                     r"\s*name\s*=\s*\"([^\"]*)\"")
    out: dict[str, str] = {}
    for key, name in pat.findall(src):
        out.setdefault(key, name)
    # Poker hands are keyed by their English name already, but carry a
    # localized string; keep the mapping so the dashboard need not guess.
    for m in re.finditer(r"\['([A-Z][A-Za-z ]+)'\]\s*=\s*\"([^\"]*)\"", src):
        out.setdefault(m.group(1), m.group(2))
    return out


def parse_hand_order(z: zipfile.ZipFile) -> list:
    """Poker hands, strongest first.

    evaluate_poker_hand (functions/misc_functions.lua) opens with a results
    table listing every hand in descending strength, which is the game's own
    ranking -- better than hardcoding an order here and letting it drift from
    Balatro's, which has Five of a Kind and the flush hands above a Straight
    Flush.
    """
    try:
        src = z.read("functions/misc_functions.lua").decode("utf-8", "replace")
    except KeyError:
        return []
    i = src.find("function evaluate_poker_hand")
    if i < 0:
        return []
    block = src[i:i + 2000]
    j = block.find("local results")
    if j < 0:
        return []
    block = block[j:block.find("}", block.find("High Card")) + 1]
    return re.findall(r'\[\"([^\"]+)\"\]\s*=\s*\{', block.replace("'", '"'))


def parse_hand_planets(src: str) -> dict:
    """Poker hand -> the Planet card that levels it.

    Each Planet centre carries `config = {hand_type = '...'}`, so the pairing
    is the game's own rather than a list here that would drift. It gives the
    hand panels the same card art the rest of the dashboard uses, which also
    makes their rows the same height as the joker rows.
    """
    # One centre per line, and each carries a nested `pos = {x=,y=}` before
    # `set`, so a brace-bounded pattern cannot reach across it. Scan lines.
    key_re = re.compile(r"(c_[a-z0-9_]+)\s*=")
    hand_re = re.compile(r"hand_type\s*=\s*'([^']+)'")
    out = {}
    for line in src.split("\n"):
        if '"Planet"' not in line and "'Planet'" not in line:
            continue
        k, h = key_re.search(line), hand_re.search(line)
        if k and h:
            out.setdefault(h.group(1), k.group(1))
    return out


def make_hand_icons(sprites: dict) -> list:
    """Cut the hand out of Four Fingers and make a held / not-held pair.

    Counter jokers hold two records -- the counter while the joker was
    actually in hand, and the counter regardless -- and a small hand says
    which far better than the words "IF HELD". The game has no hand icon, but
    Four Fingers is a hand, so it is borrowed: the art is greyscale where the
    card behind it is a saturated purple, which separates them cleanly.

    Needs Pillow. Skipped rather than fatal if it is missing, since every
    other asset is useful without it.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("  hand icons      skipped (no Pillow)")
        return []

    spr = sprites.get("j_four_fingers")
    if not spr:
        return []
    cw, ch = 142, 190
    sheet = Image.open(os.path.join(OUT, spr["a"])).convert("RGBA")
    cell = sheet.crop((spr["x"] * cw, spr["y"] * ch,
                       (spr["x"] + 1) * cw, (spr["y"] + 1) * ch))
    # Inside the border and inside the JOKER lettering down each side, or the
    # crop picks up the white frame instead of the hand.
    inner = cell.crop((28, 34, 116, 160))
    src = inner.load()
    cut = Image.new("RGBA", inner.size, (0, 0, 0, 0))
    dst = cut.load()
    for y in range(inner.height):
        for x in range(inner.width):
            r, g, b, a = src[x, y]
            if a and max(r, g, b) - min(r, g, b) < 45:
                dst[x, y] = (r, g, b, a)
    hand = cut.crop(cut.getbbox())

    written = []
    hand.save(os.path.join(OUT, "hand_held.png"))
    written.append("hand_held.png")

    # Not held: the same hand, dimmed, under a hard red slash.
    dim = Image.new("RGBA", hand.size, (0, 0, 0, 0))
    hp, dp = hand.load(), dim.load()
    for y in range(hand.height):
        for x in range(hand.width):
            r, g, b, a = hp[x, y]
            if a:
                v = (r + g + b) // 3
                dp[x, y] = (v, v, v, int(a * 0.55))
    d = ImageDraw.Draw(dim)
    w, h = hand.size
    d.line([(4, h - 5), (w - 5, 4)], fill=(26, 20, 20, 255), width=13)
    d.line([(4, h - 5), (w - 5, 4)], fill=(254, 95, 85, 255), width=8)
    dim.save(os.path.join(OUT, "hand_unheld.png"))
    written.append("hand_unheld.png")
    print(f"  hand icons      {hand.size[0]}x{hand.size[1]}  held + unheld")
    return written


def atlas_for(key: str) -> tuple[str, int, int] | None:
    if key in ATLAS_OVERRIDES:
        return ATLAS_OVERRIDES[key]
    prefix = "stake" if key.startswith("stake_") else key.split("_", 1)[0]
    return ATLASES.get(prefix)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", default=DEFAULT_GAME)
    a = ap.parse_args()

    exe = os.path.join(a.game, "Balatro.exe")
    if not os.path.exists(exe):
        raise SystemExit(f"Balatro.exe not found at {exe}\nPass --game <install dir>.")

    os.makedirs(OUT, exist_ok=True)
    z = zipfile.ZipFile(exe)

    wanted = {name for name, _, _ in ATLASES.values()}
    sizes = {}
    for name in sorted(wanted):
        member = f"resources/textures/2x/{name}"
        data = z.read(member)
        with open(os.path.join(OUT, name), "wb") as fh:
            fh.write(data)
        sizes[name] = png_size(data)
        print(f"  {name:16s} {sizes[name][0]}x{sizes[name][1]}")

    # The game's own typeface. The stylesheet already asked for "m6x11" but
    # nothing ever served it, so every page had been falling back to Courier.
    for font in ("m6x11plus.ttf",):
        try:
            data = z.read("resources/fonts/" + font)
        except KeyError:
            print(f"  {font:16s} not in this build")
            continue
        with open(os.path.join(OUT, font), "wb") as fh:
            fh.write(data)
        print(f"  {font:16s} {len(data) // 1024} KB")

    src = z.read("game.lua").decode("utf-8", "replace")
    centers = parse_centers(src)
    names = parse_names(z)
    hand_order = parse_hand_order(z)
    hand_planets = parse_hand_planets(src)
    sprites = {}

    # Playing cards are keyed by suit and rank rather than a centre key, so the
    # dashboard can look one up from the rank and suit it already stores.
    # 13 ranks x 4 suits. Note [^{}] rather than [^}]: the entry ends with a
    # nested `pos = {`, and a class allowing `{` runs straight past the entry.
    card_re = re.compile(
        r"[HCDS]_[0-9AJQKT]+\s*=\s*\{[^{}]*?value\s*=\s*'([^']+)'\s*,"
        r"\s*suit\s*=\s*'([^']+)'\s*,\s*pos\s*=\s*\{\s*x\s*=\s*(\d+)\s*,\s*y\s*=\s*(\d+)")
    for value, suit, x, y in card_re.findall(src):
        sprites[f"card:{suit}:{value}"] = {"a": "8BitDeck.png",
                                           "x": int(x), "y": int(y)}
    for key, (x, y) in centers.items():
        info = atlas_for(key)
        if not info:
            continue
        atlas, cw, ch = info
        sprites[key] = {"a": atlas, "x": x, "y": y}

    doc = {
        "names": names,
        # Strongest first, so a rank is len - index.
        "hand_order": hand_order,
        "hand_planets": hand_planets,
        "atlases": {n: {"w": w, "h": h,
                        "cw": next(c for a, c, _ in ATLASES.values() if a == n),
                        "ch": next(h2 for a, _, h2 in ATLASES.values() if a == n)}
                    for n, (w, h) in sizes.items()},
        "sprites": sprites,
        # j_wee shares cell (0,0) with j_joker in the game's own data, so it
        # renders as the plain Joker. Recorded here rather than silently
        # papered over.
        "known_collisions": [k for k, v in sprites.items()
                             if (v["a"], v["x"], v["y"]) == ("Jokers.png", 0, 0)],
    }
    make_hand_icons(sprites)

    with open(os.path.join(OUT, "sprites.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)

    by_kind = {}
    for k in sprites:
        kind = "card" if k.startswith("card:") else k.split("_", 1)[0]
        by_kind[kind] = by_kind.get(kind, 0) + 1
    print(f"  sprites.json     {len(sprites)} sprites {by_kind}")
    if len(doc["known_collisions"]) > 1:
        print(f"  note: cell (0,0) shared by {doc['known_collisions']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
