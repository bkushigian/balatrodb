"""Exercises ingest/paths.py, and pins it to the mod's half of the same fact.

Where Balatro keeps things is decided twice: in Python, from `sys.platform`,
and in Lua, from `love.system.getOS()`. The installer writes a launcher and
the mod runs it, so if the two disagree about what it is called the button
does nothing and says "Setup needed" -- on a machine that was set up.

Only one of the three platforms can be the one running this, so the other
two are exercised by reloading the module with `sys.platform` patched. That
is the whole reason `paths.py` computes everything at import.

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
import paths as _paths                                    # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    if not cond:
        fails += 1
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"  {detail}" if detail and not cond else ""))


def as_platform(plat, env=None):
    """paths.py as it would be on another OS.

    `sys.platform` alone is not enough: Python binds os.path to ntpath
    or posixpath once, at interpreter start, so a simulated macOS run
    on a Windows box still joins with backslashes. The separator is
    half of what is being tested, so os.path is swapped for the
    target's flavour too.
    """
    mod = ntpath if plat == "win32" else posixpath
    base = {"HOME": "/home/u", "USERPROFILE": r"C:\Users\u",
            "APPDATA": r"C:\Users\u\AppData\Roaming"}
    base.update(env or {})
    with mock.patch.object(sys, "platform", plat), \
            mock.patch.dict(os.environ, base, clear=False), \
            mock.patch.object(os, "path", mod), \
            mock.patch.object(mod, "expanduser",
                              lambda p: p.replace("~", base["HOME"], 1)):
        return importlib.reload(_paths)


print("each platform gets the directory LOVE would give it")
# LOVE's save directory per platform. The mod reads this from LOVE itself;
# paths.py has to reach the same place from outside the game, or the
# ingester watches a folder nothing writes to.
for plat, want in (
        ("win32", os.path.join(r"C:\Users\u\AppData\Roaming", "Balatro")),
        ("darwin", "/home/u/Library/Application Support/Balatro"),
        ("linux", "/home/u/.local/share/Balatro")):
    p = as_platform(plat)
    check(f"{plat}: save dir", p.SAVE_DIR == want, f"got {p.SAVE_DIR!r}")

print("\nXDG_DATA_HOME is honoured on Linux, as LOVE honours it")
p = as_platform("linux", {"XDG_DATA_HOME": "/home/u/.data"})
check("XDG override", p.SAVE_DIR == "/home/u/.data/Balatro", p.SAVE_DIR)

print("\nthe logs live under the save directory on every platform")
for plat in ("win32", "darwin", "linux"):
    p = as_platform(plat)
    check(f"{plat}: logs under save dir",
          p.LOGS_DIR.startswith(p.SAVE_DIR) and p.LOGS_DIR.endswith("runs"),
          p.LOGS_DIR)

print("\nPython and Lua agree on what the launcher is called")
# menu.lua picks its name from love.system.getOS(); paths.py from
# sys.platform. Two halves of one handshake, in two languages.
menu = (ROOT / "mod" / "BalatroDB" / "src" / "menu.lua").read_text(encoding="utf-8")
lua_names = set(re.findall(r"'BalatroDB/(launch-dashboard\.[a-z]+)'", menu))
py_names = {as_platform(p).LAUNCHER_NAME for p in ("win32", "darwin", "linux")}
check("the same set of names", lua_names == py_names,
      f"lua {sorted(lua_names)} vs python {sorted(py_names)}")
# ...and the same way round: Windows takes the .bat.
check("Lua gives Windows the .bat",
      re.search(r"WINDOWS and 'BalatroDB/launch-dashboard\.bat'", menu) is not None)
check("Python gives Windows the .bat",
      as_platform("win32").LAUNCHER_NAME == "launch-dashboard.bat")

print("\nthe launcher written is the right kind of script")
import install                                            # noqa: E402
for plat, head, must in (
        ("win32", "@echo off", "start "),
        ("darwin", "#!/bin/sh", "nohup "),
        ("linux", "#!/bin/sh", "nohup ")):
    p = as_platform(plat)
    with mock.patch.object(install, "paths", p), \
            mock.patch.object(install.os, "makedirs", lambda *a, **k: None), \
            mock.patch.object(install.os, "stat", lambda *a, **k: os.stat_result(
                (0o644, 0, 0, 1, 0, 0, 0, 0, 0, 0))), \
            mock.patch.object(install.os, "chmod", lambda *a, **k: None):
        written = {}
        import io

        def fake_open(path, mode="r", **kw):
            buf = io.StringIO()
            buf.close = lambda: written.update(body=buf.getvalue())
            return buf

        with mock.patch("builtins.open", fake_open):
            install.write_launcher(8611)
    body = written.get("body", "")
    check(f"{plat}: starts with {head!r}", body.startswith(head), repr(body[:40]))
    check(f"{plat}: detaches with {must!r}", must in body, repr(body[:120]))
    check(f"{plat}: names this python", sys.executable in body)

# Leave the module as the running platform found it, so anything importing
# it after this file does not get a foreign OS's answers.
importlib.reload(_paths)

print("\nFAILURES:", fails)
sys.exit(1 if fails else 0)
