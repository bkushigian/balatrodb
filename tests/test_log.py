"""Exercises mod/BalatroDB/src/log.lua under a host interpreter.

The writer decides what reaches disk and what is dropped, so its buffering and
commitment rules are worth testing directly rather than discovering in a corpus.

    pip install lupa && python tests/test_log.py
"""
import lupa

L = lupa.LuaRuntime(unpack_returned_tuples=False)
L.execute("unpack = unpack or table.unpack")

# A nativefs stand-in backed by a table, so the test can see exactly which
# files were created and what landed in them.
L.execute("""
  warnings = 0
  sendWarnMessage = function() warnings = warnings + 1 end
  sendInfoMessage = function() end
  FS = { files = {}, fail_append = false, dirs = 0 }
  local fakefs = {
    createDirectory = function() FS.dirs = FS.dirs + 1; return true end,
    write = function(p, d) FS.files[p] = d or ''; return true end,
    append = function(p, d)
      if FS.fail_append then return false, 'simulated failure' end
      FS.files[p] = (FS.files[p] or '') .. d; return true
    end,
  }
  local realrequire = require
  require = function(n)
    if n == 'nativefs' then return fakefs end
    if n == 'json' then return { encode = function(v)
        if type(v) == 'string' then return '"' .. v .. '"' end
        if type(v) == 'table' then return '{}' end
        return tostring(v) end } end
    return realrequire(n)
  end
  love = { filesystem = { getSaveDirectory = function() return '/save' end },
           timer = { getTime = function() return 0 end } }
  BalatroDB = { SCHEMA = 1 }
""")

util = L.execute(open("mod/BalatroDB/src/util.lua", encoding="utf-8").read())
L.globals().BalatroDB.util = util
log = L.execute(open("mod/BalatroDB/src/log.lua", encoding="utf-8").read())
L.globals().BalatroDB.log = log
log.context = L.eval(
    'function() return {run="R", seg=0, t=0, ante=1, round=1, endless=false} end')

g = L.globals()
fails = 0


def check(name, cond, detail=""):
    global fails
    if not cond:
        fails += 1
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail and not cond else ""))


def files():
    return {k: v for k, v in g.FS.files.items()}


print("a run that is never played leaves nothing behind")
# This is the seed-search reroll case: Brainstorm and GreenNeedle call
# delete_run + start_run on a timer, so without deferral each reroll writes a
# complete little file and buries the corpus.
log.open("reroll-1", False)
log.emit("run.start", None)
log.emit("run.baseline", None)
check("nothing created before commitment", len(files()) == 0, f"got {list(files())}")
check("not reported as committed", log.committed() is False)
log.close()
check("close without commit writes no file", len(files()) == 0, f"got {list(files())}")

print("\na run that is played is written, including what came before")
log.open("real-1", False)
log.emit("run.start", None)
log.emit("run.baseline", None)
check("still nothing on disk yet", len(files()) == 0)
log.commit()
fs = files()
check("commit creates exactly one file", len(fs) == 1, f"got {list(fs)}")
path = list(fs)[0]
check("named for the run", path.endswith("real-1.jsonl"), path)
check("the buffered events were not lost", fs[path].count("\n") == 2,
      f"got {fs[path].count(chr(10))} lines")
log.emit("hand.play", None)
log.flush()
check("later events append", files()[path].count("\n") == 3)
log.close()

print("\nsequence numbering survives deferral")
# n must be gap-free from 0 in the written file, even though the first events
# were buffered before the file existed.
first = files()[path].split("\n")[0]
check("first line is n=0", '"n":0' in first, first[:80])

print("\na resumed run commits immediately")
# There is nothing to wait for: resuming means the run was already played. Note
# a resume deliberately does NOT truncate -- it appends to the file the earlier
# session wrote -- so committing creates nothing on its own.
g.FS.files = L.table()
log.open("resumed-1", True)
check("committed on open, no blind needed", log.committed() is True)
check("commit did not truncate the existing log", len(files()) == 0,
      f"got {list(files())}")
log.emit("run.resume", None)
log.flush()
fs = files()
check("events go straight to the run's file", len(fs) == 1, f"got {list(fs)}")
check("appended to the right path", list(fs)[0].endswith("resumed-1.jsonl"))
log.close()

print("\na failed flush does not strand its buffer")
g.FS.files = L.table()
log.open("flaky-1", False)
log.commit()
g.FS.fail_append = True
log.emit("hand.play", None)
log.flush()
check("nothing written while failing", files().get(list(files())[0], "") == "")
log.close()
check("close keeps the path while bytes are unwritten", log.committed() is True)
g.FS.fail_append = False
log.flush()
check("the retained events are written once it recovers",
      files()[list(files())[0]].count("\n") == 1,
      f"got {files()[list(files())[0]]!r}")

print("\nFAILURES:", fails)
raise SystemExit(1 if fails else 0)
