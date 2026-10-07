"""Where Balatro keeps things, per platform.

The mod writes beside the game's own saves -- love.filesystem's save
directory -- so every path the ingest side needs hangs off that one folder,
the database included. Keeping it all there rather than in this repo means a
player can find, back up or delete their data without going near the code.
LÖVE picks it per OS, not us:

    Windows   %APPDATA%\\Balatro
    macOS     ~/Library/Application Support/Balatro
    Linux     $XDG_DATA_HOME/love/Balatro  (default ~/.local/share/love/...)

Lovely reads mods from Mods/ inside the same folder, which is why the mod
link and the run logs are neighbours.

The Linux entry is LÖVE's documented default for a native build and has not
been tried against a real install; under Proton the game believes it is on
Windows and keeps its saves inside the Wine prefix instead.
"""
from __future__ import annotations

import os
import sys


def save_dir() -> str:
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "Balatro")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Balatro")
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(data, "love", "Balatro")


def mods_dir() -> str:
    return os.path.join(save_dir(), "Mods")


def data_dir() -> str:
    """The mod's own folder: log.root in mod/BalatroDB/src/log.lua."""
    return os.path.join(save_dir(), "BalatroDB")


def logs_dir() -> str:
    return os.path.join(data_dir(), "runs")


def db_path() -> str:
    """Derived from the logs and disposable: delete it and it rebuilds."""
    return os.path.join(data_dir(), "balatro.db")


def steam_game_dir() -> str:
    """Steam's default install location for Balatro."""
    if sys.platform == "win32":
        return r"C:\Program Files (x86)\Steam\steamapps\common\Balatro"
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Steam/steamapps/common/Balatro")
    return os.path.expanduser("~/.local/share/Steam/steamapps/common/Balatro")


def game_archive(game_dir: str) -> str | None:
    """The LÖVE archive holding the game's lua and textures, or None.

    On Windows it is Balatro.exe, a zip with an executable stub in front. The
    macOS app ships the same zip as a plain Balatro.love inside the bundle.
    Both are checked whatever the platform, so --game can point at a copy of
    either build.
    """
    for rel in ("Balatro.exe",
                os.path.join("Balatro.app", "Contents", "Resources", "Balatro.love")):
        path = os.path.join(game_dir, rel)
        if os.path.exists(path):
            return path
    return None
