"""Exercises ingest/paths.py, and pins it to the mod's half of the same fact.

Where Balatro keeps things is decided twice: in Python, from `sys.platform`,
and in Lua, from `love.system.getOS()`. The installer writes a launcher and
the mod runs it, so if the two disagree about what it is called the button
does nothing and says "Setup needed" -- on a machine that was set up. There
is no test a single machine can run that would catch that, which is why the
check here is that the two sources agree rather than that either one works.

Only one of the three platforms can be the one running this, so the others
are exercised with `sys.platform` patched. `paths.py` reads it inside each
function, so that is all it takes; `install.py` decides at import, so it is
reloaded.

    python tests/test_paths.py
"""
from __future__ import annotations

import importlib
import ntpath
import os
import pathlib
import posixpath
import re
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
import paths                                              # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    if not cond:
        fails += 1
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"  {detail}" if detail and not cond else ""))


HOME = "/home/u"
APPDATA = r"C:\Users\u\AppData\Roaming"


def as_platform(plat, extra=None):
    """A context in which paths.py answers as it would on `plat`.

    `sys.platform` alone is not enough: Python binds os.path to ntpath or
    posixpath once, at interpreter start, so a simulated macOS run on a
    Windows box still joins with backslashes. The separator is half of what
    is being checked, so os.path is swapped for the target's flavour too --
    without that, three of the assertions below passed for the wrong reason.
    """
    mod = ntpath if plat == "win32" else posixpath
    env = {"APPDATA": APPDATA}
    env.update(extra or {})
    if plat != "linux":
        env.pop("XDG_DATA_HOME", None)
    return (mock.patch.object(sys, "platform", plat),
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(os, "path", mod),
            mock.patch.object(mod, "expanduser",
                              lambda p: p.replace("~", HOME, 1)))


def on(plat, fn, extra=None):
    ctx = as_platform(plat, extra)
    for c in ctx:
        c.start()
    try:
        return fn()
    finally:
        for c in reversed(ctx):
            c.stop()


print("each platform gets the directory LOVE would give it")
# The mod reads this from LOVE itself (love.filesystem.getSaveDirectory);
# paths.py has to reach the same folder from outside the game, or the
# ingester watches somewhere nothing writes to. The `love/` component on
# Linux is not a typo -- LOVE nests by identity under its own folder there,
# and only there.
for plat, want in (
        ("win32", os.path.join(APPDATA, "Balatro")),
        ("darwin", "/home/u/Library/Application Support/Balatro"),
        ("linux", "/home/u/.local/share/love/Balatro")):
    got = on(plat, paths.save_dir)
    check(f"{plat}: save dir", got == want, f"got {got!r} want {want!r}")

print("\nXDG_DATA_HOME is honoured on Linux, as LOVE honours it")
got = on("linux", paths.save_dir, {"XDG_DATA_HOME": "/home/u/.data"})
check("XDG override", got == "/home/u/.data/love/Balatro", got)

print("\neverything the ingest side needs hangs off that one folder")
# Logs, the mod link and the database all live beside the game's own saves,
# so a player can find, back up or delete their data without going near the
# repo. If any of these escaped to somewhere else, that stops being true.
for plat in ("win32", "darwin", "linux"):
    def under():
        root = paths.save_dir()
        return {name: (fn().startswith(root), fn())
                for name, fn in (("mods", paths.mods_dir),
                                 ("data", paths.data_dir),
                                 ("logs", paths.logs_dir),
                                 ("db", paths.db_path))}
    got = on(plat, under)
    bad = [f"{k}={v[1]}" for k, v in got.items() if not v[0]]
    check(f"{plat}: all four live under the save dir", not bad, "; ".join(bad))
    check(f"{plat}: logs end in runs", got["logs"][1].endswith("runs"),
          got["logs"][1])

print("\nthe game archive is found by either build's name")
# Windows ships the zip behind an .exe stub; the mac app carries the same
# zip as a plain .love inside the bundle. Both are checked on every
# platform, so --game can point at a copy of either.
import tempfile                                           # noqa: E402

for rel in ("Balatro.exe",
            os.path.join("Balatro.app", "Contents", "Resources", "Balatro.love")):
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        open(target, "wb").close()
        found = paths.game_archive(tmp)
        check(f"finds {rel}", found == target, f"got {found!r}")

with tempfile.TemporaryDirectory() as tmp:
    check("an empty directory yields None", paths.game_archive(tmp) is None)

print("\nPython and Lua agree on what the launcher is called")
# The one fact in this file that no single machine can check for itself.
menu = (ROOT / "mod" / "BalatroDB" / "src" / "menu.lua").read_text(encoding="utf-8")
m = re.search(r"launch-dashboard\.'\s*\.\.\s*\(WINDOWS and '(\w+)' or '(\w+)'\)", menu)
check("menu.lua states both extensions", m is not None,
      "the LAUNCHER line in menu.lua no longer matches this pattern")

if m:
    lua_win, lua_other = m.group(1), m.group(2)
    import install                                         # noqa: E402

    def launcher_ext():
        reloaded = importlib.reload(install)
        return os.path.splitext(reloaded.LAUNCHER)[1].lstrip(".")

    py_win = on("win32", launcher_ext)
    py_mac = on("darwin", launcher_ext)
    py_nix = on("linux", launcher_ext)

    check(f"Windows: lua {lua_win!r} == python {py_win!r}", lua_win == py_win)
    check(f"macOS: lua {lua_other!r} == python {py_mac!r}", lua_other == py_mac)
    check(f"Linux: lua {lua_other!r} == python {py_nix!r}", lua_other == py_nix)
    # Leave install.py holding this machine's answers, not a simulated one's.
    importlib.reload(install)

print("\nthe launcher written is the right kind of script")
import io                                                  # noqa: E402
import install                                             # noqa: E402

for plat, head, detach in (("win32", "@echo off", "start "),
                           ("darwin", "#!/bin/sh", "nohup "),
                           ("linux", "#!/bin/sh", "nohup ")):
    def write():
        reloaded = importlib.reload(install)
        written = {}

        def fake_open(path, mode="r", **kw):
            buf = io.StringIO()
            buf.close = lambda: written.update(body=buf.getvalue())
            return buf

        with mock.patch.object(reloaded.os, "makedirs", lambda *a, **k: None), \
                mock.patch("builtins.open", fake_open):
            reloaded.write_launcher(8611)
        return written.get("body", "")

    body = on(plat, write)
    check(f"{plat}: starts with {head!r}", body.startswith(head), repr(body[:40]))
    check(f"{plat}: detaches with {detach!r}", detach in body, repr(body[:120]))
    check(f"{plat}: names this python", sys.executable in body)

importlib.reload(install)

print("\nFAILURES:", fails)
sys.exit(1 if fails else 0)
