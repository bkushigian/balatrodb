--- Buffered JSONL writer.
---
--- nativefs and json are both registered as global modules by Steamodded
--- (smods/lovely/libs.toml), so no vendoring is needed. nativefs is used in
--- preference to love.filesystem because it is not confined to the save
--- sandbox, which leaves room to point the log somewhere else later.

local NFS = require('nativefs')
local JSON = require('json')

local util = BalatroDB.util

local log = {}

log.root = love.filesystem.getSaveDirectory() .. '/BalatroDB'
log.dir = log.root .. '/runs'

local buffer = {}
local buffer_bytes = 0
local path = nil          -- set only once the run is committed to disk
local armed = nil         -- the path we WOULD write to, pending commitment
local armed_resume = false
local seq = 0

-- Events are small (~150-350 bytes) and bursty during scoring, so batching
-- avoids paying a file open per joker trigger. nativefs.append is a full
-- fopen/fwrite/fclose on the main thread (libs/nativefs/nativefs.lua:294),
-- which under a real-time virus scanner can block for milliseconds -- so
-- flushes are kept deliberately rare and are never tied to frame events.
local FLUSH_BYTES = 16 * 1024
local FLUSH_EVENTS = 128

-- A failed append is retried on the next flush rather than dropped, but the
-- backlog is capped so a permanently unwritable path cannot grow without
-- bound.
local MAX_RETRY_BYTES = 1024 * 1024

--- Supplied by state.lua. Returns run / segment / ante / round / endless for
--- the envelope. Indirected so log has no dependency on state and can be
--- loaded first.
log.context = function() return {} end

local function ensure_dirs()
    NFS.createDirectory(log.root)
    NFS.createDirectory(log.dir)
end

--- Begin writing to a run file.
---
--- `resume` appends to an existing file (one logical run played across several
--- sessions stays in one file); otherwise the file is truncated, so a reused
--- run_id can never interleave two runs in one file.
function log.open(run_id, resume)
    log.flush()
    -- Arm, do not create. Events accumulate in the buffer until log.commit(),
    -- so a run abandoned before it is really begun -- a seed-search reroll,
    -- backing out of the deck select -- leaves no file at all.
    armed = log.dir .. '/' .. run_id .. '.jsonl'
    armed_resume = resume and true or false
    path = nil
    seq = 0
    buffer, buffer_bytes = {}, 0
    -- A resumed run is real by definition; there is nothing to wait for.
    if resume then log.commit() end
end

--- The run is real: create the file and write everything buffered so far.
function log.commit()
    if path or not armed then return end
    ensure_dirs()
    path = armed
    if not armed_resume then
        local ok, err = NFS.write(path, '')
        if not ok then
            sendWarnMessage('could not create ' .. path .. ': ' .. tostring(err), 'BalatroDB')
        end
    end
    sendInfoMessage((armed_resume and 'resuming ' or 'logging run to ') .. path, 'BalatroDB')
    log.flush()
end

--- The run ended without ever being played. Drop it; no file was created.
function log.discard()
    armed, path = nil, nil
    buffer, buffer_bytes = {}, 0
    seq = 0
end

--- Whether anything has been written to disk for this run yet.
function log.committed()
    return path ~= nil
end

function log.flush()
    -- Nothing reaches disk before commitment; the buffer just grows, and it is
    -- only ever the handful of events before the first blind.
    if not path or #buffer == 0 then return end
    local chunk = table.concat(buffer)

    local ok, err = NFS.append(path, chunk)
    if ok then
        buffer, buffer_bytes = {}, 0
        return
    end

    -- Keep the events. Clearing the buffer before confirming the write is what
    -- turns a transient failure (file locked by a scanner, disk full) into a
    -- silent hole in the middle of the file, which the schema promises cannot
    -- happen because seq has already advanced past it.
    sendWarnMessage('append failed, will retry: ' .. tostring(err), 'BalatroDB')
    if buffer_bytes > MAX_RETRY_BYTES then
        sendWarnMessage('retry backlog over cap; dropping ' .. tostring(buffer_bytes) .. ' bytes', 'BalatroDB')
        buffer, buffer_bytes = {}, 0
    else
        buffer, buffer_bytes = { chunk }, #chunk
    end
end

function log.close()
    if not path then
        -- Never committed: the run was abandoned before it began.
        log.discard()
        return
    end
    log.flush()
    -- Only let go of the path if everything actually reached disk. Clearing it
    -- after a failed flush strands the retry buffer: the next log.open resets
    -- it, and those events are gone for good.
    if #buffer == 0 then
        -- `armed` goes with it. Left set, a later commit() would see a nil
        -- path and re-open this finished run's file, appending past its
        -- run.end.
        armed, path = nil, nil
    else
        sendWarnMessage('closing with ' .. tostring(buffer_bytes) ..
                        ' bytes unwritten; will retry on the next flush', 'BalatroDB')
    end
end

--- Emit one event. Never raises: a failure to encode costs one line.
function log.emit(etype, data)
    -- Buffers while armed; only a committed run has a path to flush to.
    if not (path or armed) then return end

    local ctx = log.context()

    -- The envelope is assembled by hand rather than encoded as a table,
    -- because Lua tables are unordered and the encoder emits them in hash
    -- order: the event type could land anywhere, in one real corpus up to 11KB
    -- into a line, and the order shifted whenever a key was added. Fixing the
    -- order -- and putting `e` first -- lets a reader classify a line from its
    -- first few bytes without parsing the whole thing, which is what makes
    -- cheap filtered scans possible.
    local ok, encoded = pcall(function()
        local payload = data ~= nil and JSON.encode(data) or 'null'
        return table.concat({
            '{"e":', JSON.encode(etype),
            ',"v":', tostring(BalatroDB.SCHEMA),
            ',"run":', JSON.encode(ctx.run or ''),
            ',"seg":', tostring(ctx.seg or 0),
            ',"n":', tostring(seq),
            ',"t":', JSON.encode(ctx.t or 0),
            ',"a":', ctx.ante ~= nil and JSON.encode(ctx.ante) or 'null',
            ',"r":', ctx.round ~= nil and JSON.encode(ctx.round) or 'null',
            ',"el":', ctx.endless and 'true' or 'false',
            ',"d":', payload,
            '}',
        })
    end)

    if not ok then
        -- Encoding blew up on something util.num did not catch. Record the
        -- gap rather than dropping it silently, so the sequence stays gap-free
        -- and the ingester can see that something was lost here. Every field
        -- below is a plain scalar, so this encode cannot fail in turn.
        local ok2, fallback = pcall(JSON.encode, {
            v = BalatroDB.SCHEMA, run = ctx.run, seg = ctx.seg, n = seq,
            e = 'encode.error',
            d = { of = etype, err = tostring(encoded) },
        })
        if not ok2 then return end
        encoded = fallback
    end

    seq = seq + 1
    encoded = encoded .. '\n'
    buffer[#buffer + 1] = encoded
    buffer_bytes = buffer_bytes + #encoded

    if buffer_bytes >= FLUSH_BYTES or #buffer >= FLUSH_EVENTS then
        log.flush()
    end
end

function log.is_open()
    return path ~= nil
end

return log
