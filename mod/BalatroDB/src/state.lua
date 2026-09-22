--- Per-run identity, the endless latch, and the envelope context.
---
--- Deliberately holds no statistics. Maxima, bests and totals are derived by
--- the ingester from the event log, never accumulated here -- that is what
--- makes it possible to add a new statistic later and have it apply to runs
--- already recorded.

local util = BalatroDB.util
local log = BalatroDB.log

local state = {
    run_id = nil,
    seg = 0,            -- which session of this run; (seg, n) totally orders it
    started_at = nil,   -- love.timer clock at segment start, for relative t
    active = false,
    endless = false,
    won_pending = false,
}

function state.new_run_id(seed)
    -- Seed alone is not unique: the same seed replayed is a different run.
    return ('%d-%s-%s'):format(os.time(), tostring(seed or 'UNKNOWN'), util.short_id(4))
end

--------------------------------------------------------------------------
-- Endless latch
--
-- G.GAME.won is NOT the boundary. It is set at state_events.lua:112, inside
-- end_round, the instant the win-ante boss is beaten -- before win_game() is
-- even queued. Reading it at emit time therefore stamps the winning round's
-- own cash-out as endless, and permanently removes the largest legitimate
-- non-endless payout from every winning run.
--
-- The real boundary is the player choosing to carry on, so the latch flips on
-- the first blind selected after the win. Everything up to and including the
-- winning round counts as non-endless.
--------------------------------------------------------------------------

--- Called when win_game fires. Arms the latch without flipping it.
function state.mark_won()
    state.won_pending = true
    state.persist()
end

--- Called when a blind is selected. Flips the latch if a win is pending.
function state.note_blind_selected()
    if state.won_pending and not state.endless then
        state.endless = true
        state.won_pending = false
        state.persist()
        return true
    end
    return false
end

--------------------------------------------------------------------------
-- Persistence across save/quit/resume
--
-- save_run serializes G.GAME wholesale through recursive_table_cull
-- (misc_functions.lua:1569), which copies every non-Object scalar verbatim.
-- So plain fields parked on G.GAME survive the round trip, letting a run
-- played over several sessions keep one identity and one log file.
--------------------------------------------------------------------------

function state.persist()
    if not G or not G.GAME then return end
    G.GAME.bdb_run_id = state.run_id
    G.GAME.bdb_seg = state.seg
    G.GAME.bdb_endless = state.endless
    G.GAME.bdb_won_pending = state.won_pending
end

--- Restore identity from a loaded save. Returns true if this is a resume.
function state.restore()
    local game = G and G.GAME
    if not (game and game.bdb_run_id) then return false end
    state.run_id = game.bdb_run_id
    state.seg = (tonumber(game.bdb_seg) or 0) + 1
    state.endless = game.bdb_endless and true or false
    state.won_pending = game.bdb_won_pending and true or false
    return true
end

function state.begin(run_id, seg)
    state.run_id = run_id
    state.seg = seg or 0
    state.started_at = love.timer.getTime()
    state.active = true
    state.persist()
end

function state.finish()
    state.active = false
end

--- Envelope context. Called for every emitted event, so it stays cheap and
--- must tolerate being called when G.GAME is half-built.
function state.context()
    local game = G and G.GAME
    return {
        run = state.run_id,
        seg = state.seg,
        t = state.started_at and
            math.floor((love.timer.getTime() - state.started_at) * 100) / 100 or 0,
        -- Run through util.num: these come straight off G.GAME, and a modded
        -- ante could be a big-number table, which would fail the encode and
        -- cost the event.
        ante = game and game.round_resets and util.num(game.round_resets.ante) or nil,
        round = game and util.num(game.round) or nil,
        endless = state.endless,
    }
end

log.context = state.context

return state
