"""Where Balatro keeps its things, on each platform it runs on.

Three scripts need these answers -- the ingester (where the logs are),
the installer (where Mods/ is, and what kind of launcher to write) and the
asset extractor (where the game archive is). They used to hold a Windows
answer each, so this is one place rather than three that have to agree.

Nothing here touches the disk except `game_archive`, which has to look.
"""
from __future__ import annotations

import os
import sys

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"

HOME = os.path.expanduser("~")


def _save_root() -> str:
    """Balatro's save directory: Mods/, the profile saves, and our logs.

    LÖVE picks this per platform (love.filesystem.getSaveDirectory), and the
    mod reads it from LÖVE rather than guessing. This is the same answer,
    worked out from outside the game.
    """
    if WINDOWS:
        # %APPDATA% rather than the expanded form, because a roaming profile
        # can put it somewhere other than under the home directory.
        return os.path.join(os.environ.get("APPDATA")
                            or os.path.join(HOME, "AppData", "Roaming"),
                            "Balatro")
    if MACOS:
        return os.path.join(HOME, "Library", "Application Support", "Balatro")
    # LÖVE follows the XDG base directory spec on Linux.
    return os.path.join(os.environ.get("XDG_DATA_HOME")
                        or os.path.join(HOME, ".local", "share"), "Balatro")


SAVE_DIR = _save_root()
MODS_DIR = os.path.join(SAVE_DIR, "Mods")
DATA_DIR = os.path.join(SAVE_DIR, "BalatroDB")
LOGS_DIR = os.path.join(DATA_DIR, "runs")

# The launcher the in-game button runs. menu.lua picks the same name from
# love.system.getOS(), so the two halves have to stay in step -- which is
# what tests/test_paths.py checks.
LAUNCHER_NAME = "launch-dashboard.bat" if WINDOWS else "launch-dashboard.command"
LAUNCHER = os.path.join(DATA_DIR, LAUNCHER_NAME)


def steam_dirs() -> list[str]:
    """Places Steam installs Balatro, most likely first."""
    if WINDOWS:
        return [os.path.join(p, "Steam", "steamapps", "common", "Balatro")
                for p in (os.environ.get("ProgramFiles(x86)")
                          or r"C:\Program Files (x86)",
                          os.environ.get("ProgramFiles") or r"C:\Program Files")]
    if MACOS:
        return [os.path.join(HOME, "Library", "Application Support", "Steam",
                             "steamapps", "common", "Balatro")]
    return [os.path.join(HOME, d, "steamapps", "common", "Balatro")
            for d in (".steam/steam", ".local/share/Steam", ".steam/root")]


# What the LÖVE archive is called inside the install directory. Every one of
# these is a plain zip whichever platform wrote it, so the extractor reads
# them all the same way; only the name and the nesting differ.
ARCHIVE_NAMES = (
    "Balatro.exe",                                  # Windows, and Proton
    os.path.join("Balatro.app", "Contents", "Resources", "Balatro.love"),
    "Balatro.love",
    os.path.join("Contents", "Resources", "Balatro.love"),  # given the .app
)


def game_archive(where: str | None = None) -> tuple[str | None, list[str]]:
    """(path to the game archive, everywhere that was looked).

    Returns the list either way so a failure can say what it tried instead
    of naming one path the user may never have had.
    """
    roots = [where] if where else steam_dirs()
    tried = []
    for root in roots:
        # Pointed straight at the archive rather than at its directory.
        if os.path.isfile(root):
            return root, [root]
        for name in ARCHIVE_NAMES:
            cand = os.path.join(root, name)
            tried.append(cand)
            if os.path.isfile(cand):
                return cand, tried
        # A .app bundle whose inner .love is named for something else.
        app = os.path.join(root, "Balatro.app", "Contents", "Resources")
        if os.path.isdir(app):
            for f in sorted(os.listdir(app)):
                if f.endswith(".love"):
                    return os.path.join(app, f), tried
    return None, tried
