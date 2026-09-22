--- Every observation point, in one file.
---
--- Balatro's game logic is plain globals and G.FUNCS entries, so almost all of
--- this is function wrapping -- no Lovely patches are needed. Each hook is
--- annotated with where its target lives in the patched source that Lovely
--- writes to Mods/lovely/dump/ on every launch.
---
--- The recurring hazard, and the reason several hooks look more elaborate than
--- they need to: Balatro defers nearly all state mutation into G.E_MANAGER
--- events that run on later frames. An after-observer on a G.FUNCS entry point
--- runs inline, so it sees PRE-action state. Anything that must be read after
--- the fact is either captured from the arguments instead, or observed at the
--- place the mutation actually lands.

local util  = BalatroDB.util
local log   = BalatroDB.log
local env   = BalatroDB.env
local state = BalatroDB.state

local hooks = {}

local function emit(etype, data) log.emit(etype, data) end

--- Why the current run is being torn down; see "Why a run ended" below.
--- Declared here because the run-start hook clears it, and a Lua local is only
--- visible to closures created after its declaration.
local pending_reason = nil

--- Forward declaration, for the same reason: the run-start hook needs to be
--- able to close a run that somehow survived to that point.
local end_run

--- Jokers, sampled with their numeric state. Cheap enough to attach to every
--- hand and round, which is what makes maxima exact for the jokers that never
--- call SMODS.scale_card (Supernova, Fortune Teller, Stone Joker, Throwback,
--- Swashbuckler) and for the few vanilla scaling paths that still mutate
--- ability fields directly (Glass Joker via The Hanged Man at card.lua:3122,
--- Invisible Joker's counter at :3347).
local function joker_sample()
    return util.nonempty(util.cards(G.jokers))
end

--- The real deck. G.deck is only the undrawn pile -- during a blind the rest
--- of the cards are in G.hand, G.play and G.discard -- whereas G.playing_cards
--- is the full set, maintained by Card:remove (card.lua:5195) and the various
--- add paths. It is what the game itself uses for Erosion (card.lua:4287).
local function deck_cards()
    return G.playing_cards
end

local function deck_size()
    return G.playing_cards and #G.playing_cards or nil
end

--------------------------------------------------------------------------
-- Run lifecycle
--------------------------------------------------------------------------

local function emit_run_start(resumed)
    local game = G.GAME or {}
    emit(resumed and 'run.resume' or 'run.start', {
        ts         = os.time(),
        seed       = game.pseudorandom and game.pseudorandom.seed,
        seeded     = game.seeded and true or false,
        challenge  = game.challenge,
        deck       = game.selected_back and game.selected_back.name,
        deck_key   = game.selected_back and game.selected_back.effect
                     and game.selected_back.effect.center and game.selected_back.effect.center.key,
        stake      = game.stake,
        stake_key  = env.stake_key(),
        win_ante   = game.win_ante,
        profile    = G.SETTINGS and G.SETTINGS.profile,
        starting_deck_size = game.starting_deck_size,
        env        = env.capture(),
    })

    -- The inventory baseline is a SEPARATE event, deliberately.
    --
    -- Every segment needs one: a resumed segment because CardArea:load
    -- (cardarea.lua:718) rebuilds the areas directly and fires no add events,
    -- and a fresh one because the starting deck is built inside
    -- Game:start_run before the log opens.
    --
    -- But it is 5-10KB of card arrays, and keeping it out of run.start means a
    -- reader can identify a run -- deck, stake, seed, date -- by reading just
    -- the first line, instead of parsing all of it. Measured at roughly 19x
    -- cheaper for a run-list scan.
    emit(resumed and 'run.rebaseline' or 'run.baseline', {
        jokers      = joker_sample(),
        consumables = util.nonempty(util.cards(G.consumeables)),
        deck_cards  = util.cards(deck_cards()),
        dollars     = util.num(game.dollars),
        chips       = util.num(game.chips),
    })
end

local last_snapshot = 0

-- game.lua:2048. The same entry point serves a fresh run and a resumed save
-- (engine/controller.lua:907), distinguished by args.savetext.
util.hook_around(Game, 'start_run', function(args)
    -- Read before the original runs: it replaces G.GAME wholesale.
    return { was_active = state.active }
end, function(args, _, pre)
    local opts = args[2] or {}
    local resumed = state.restore()

    if pre and pre.was_active and not resumed then
        -- Safety net. Game:delete_run normally closes the run first, so this
        -- only fires if some path starts a run without tearing the old one
        -- down -- in which case the previous run still needs a proper ending.
        end_run(pending_reason or 'restart')
    end

    if resumed then
        log.open(state.run_id, true)
        state.begin(state.run_id, state.seg)
    else
        local run_id = state.new_run_id(G.GAME and G.GAME.pseudorandom and G.GAME.pseudorandom.seed)
        state.endless, state.won_pending = false, false
        log.open(run_id, false)
        state.begin(run_id, 0)
    end

    last_snapshot = 0
    pending_reason = nil
    emit_run_start(resumed)
end)

--- Whether the run can be resumed. G.SAVED_GAME is only populated while a save
--- is being loaded (engine/controller.lua:905), so it is nil when quitting to
--- the menu and cannot be used to tell a suspension from an abandonment. The
--- save file on disk is what the game itself checks
--- (button_callbacks.lua:197).
local function save_exists()
    local profile = G.SETTINGS and G.SETTINGS.profile
    if not profile then return false end
    return love.filesystem.getInfo(profile .. '/save.jkr') ~= nil
end

--------------------------------------------------------------------------
-- Why a run ended
--
-- Game:delete_run is shared by at least six entry points -- new run, restart,
-- main menu, profile switch, language change, demo -- so nothing observable at
-- teardown time distinguishes them. In particular a save file still exists on
-- disk at that moment even when the player is abandoning the run to start a
-- different one, so its presence alone reads a reroll as a suspension.
--
-- The intent is therefore recorded at the entry point the player actually
-- triggered, and consumed when the teardown arrives.
--------------------------------------------------------------------------

local function set_reason(r) return function() pending_reason = r end end

--- Reasons that genuinely finish a run, as opposed to parking it.
local NON_TERMINAL = { suspended = true }

end_run = function(result)
    if not state.active then pending_reason = nil; return end
    local game = G.GAME or {}
    local scores = game.round_scores or {}

    -- Final inventory first, so that run.end is the LAST line of the file and
    -- stays small. A reader can then tail the file to learn how a run ended
    -- without parsing the deck and joker arrays.
    emit('run.final', {
        jokers = joker_sample(),
        deck   = util.cards(deck_cards()),
    })

    emit('run.end', {
        result  = result,
        -- False means the run is expected to continue in a later segment, so
        -- completion stats can filter on it without enumerating reasons.
        terminal = not NON_TERMINAL[result],
        -- Diagnostic: a result of 'unknown' means some path tore the run down
        -- without announcing itself, and the state it happened in is the only
        -- clue for tracking down which one.
        at_state = G and G.STATE and util.state_name(G.STATE) or nil,
        ante    = util.num(game.round_resets and game.round_resets.ante),
        round   = util.num(game.round),
        -- G.GAME.chips is the score of the round the run ended ON, not the
        -- run's best -- a run that peaked at 800k and then died on a 40k round
        -- reports 40k. Named for what it is, with the actual bests alongside.
        final_round_score = util.num(game.chips),
        -- The game maintains these as running maxima for free
        -- (check_and_set_high_score, misc_functions.lua:1146).
        best_hand      = util.num(scores.hand and scores.hand.amt),
        furthest_ante  = util.num(scores.furthest_ante and scores.furthest_ante.amt),
        furthest_round = util.num(scores.furthest_round and scores.furthest_round.amt),
        dollars = util.num(game.dollars),
        hands_played = game.hands_played,
        skips   = game.skips,
        deck_size = deck_size(),
        -- Repeated from the envelope so an endless filter is answerable from
        -- this one line, without scanning the file for run.win.
        endless = state.endless,
        -- THE win flag, independent of how the session ended. Winning is
        -- beating the win ante; a run that then continued into endless and
        -- died is still a won run. Win rate is COUNT(won) over terminal runs,
        -- never a test against `result`.
        won     = game.won and true or false,
    })
    state.finish()
    pending_reason = nil
    log.close()
end
hooks.end_run = end_run

-- The entry points that initiate a teardown. Each is queued, so the reason is
-- set well before Game:delete_run actually runs.
util.hook_around(G.FUNCS, 'start_run', function(args)
    -- button_callbacks.lua:3037 -- the restart button reuses this function.
    local e = args[1]
    local restart = e and e.config and e.config.id == 'restart_button'
    pending_reason = restart and 'restart' or 'new_run'
end, nil)

util.hook_around(G.FUNCS, 'go_to_menu', set_reason('menu'), nil)
util.hook_around(G.FUNCS, 'load_profile', set_reason('profile_switch'), nil)

-- Steamodded replaces the run-select screen, and its own start path calls
-- G:delete_run() directly on the _skip_wipe branch
-- (smods/src/utils/run_select.lua:325) rather than going through
-- G.FUNCS.start_run. Without this, starting a run from that screen tears the
-- previous one down with no reason recorded, which is what produced the first
-- run.end result of 'unknown'.
if SMODS.RunSelect and SMODS.RunSelect.Functions then
    util.hook_around(SMODS.RunSelect.Functions, 'start_run', set_reason('new_run'), nil)
end

-- game.lua:1177. This is where a run actually ends, and it runs BEFORE the
-- stage flips to MAIN_MENU -- it empties G.jokers, G.playing_cards and resets
-- G.GAME. Closing the run from the stage watcher instead recorded run.end with
-- ante 1, round 0, no dollars and an empty deck, and logged the entire
-- teardown as card.remove/joker.remove "destroyed".
--
-- A game over has already closed the run by this point, so this is a no-op
-- then; state.active guards it.
util.hook_around(Game, 'delete_run', function()
    local reason = pending_reason
    if (G.GAME and G.GAME.won) and not state.endless then
        -- The run beat the win ante and the player left without continuing
        -- into endless, so it is finished rather than parked, whatever button
        -- they used. The save survives a win (only a death calls remove_save,
        -- at game.lua:3781), so testing for one would report every win as a
        -- suspension -- which is exactly what happened on the first real win.
        reason = 'completed'
    elseif reason == 'menu' then
        -- Otherwise the save decides: the run is parked if it can be resumed,
        -- abandoned if there is nothing to return to.
        reason = save_exists() and 'suspended' or 'abandoned'
    end
    end_run(reason or 'unknown')
end, nil)

-- state_events.lua:1. Fires when the win ante is beaten. The run does not end
-- here and the player has not yet chosen whether to continue, so this arms the
-- endless latch rather than flipping it, and is itself stamped non-endless.
util.hook(_G, 'win_game', function()
    emit('run.win', {
        -- The ante that was beaten, not the live counter. ease_ante(1) runs
        -- from a queued event inside end_round (state_events.lua:190) and has
        -- already drained by the time win_game is called, so the counter --
        -- and the envelope's `a` -- both read one higher than the ante the
        -- player actually cleared.
        win_ante = util.num(G.GAME and G.GAME.win_ante),
        round = util.num(G.GAME and G.GAME.round),
        score = util.num(G.GAME and G.GAME.chips),
    })
    state.mark_won()
end)

--------------------------------------------------------------------------
-- State machine watcher
--
-- G.STATES.GAME_OVER is set from at least three places (state_events.lua:115,
-- :305, smods utils.lua:3132), so watching the transition in Game:update is
-- more reliable than hooking each site.
--------------------------------------------------------------------------

local last_state, last_stage

util.hook(Game, 'update', function()
    local st, stage = G.STATE, G.STAGE

    if stage ~= last_stage then
        -- Fallback only: Game:delete_run normally closes the run before the
        -- stage changes, so this fires just for paths that reach the menu
        -- without tearing the run down.
        if last_stage == G.STAGES.RUN and stage == G.STAGES.MAIN_MENU then
            end_run(save_exists() and 'suspended' or 'abandoned')
        end
        last_stage = stage
    end

    if st ~= last_state then
        if state.active then
            emit('state.change', {
                from = last_state and util.state_name(last_state),
                to = util.state_name(st),
            })
            if st == G.STATES.GAME_OVER then
                -- Always a death, never a verdict. Winning does not reach this
                -- state at all: game_over and game_won are independent
                -- (state_events.lua:110-115) and the run stays alive so the
                -- player can continue into endless. Whether the run was WON is
                -- a separate question answered by the `won` field -- a death
                -- in endless is a won run that ended by dying.
                end_run('died')
            end
        end
        last_state = st
    end
end)

--------------------------------------------------------------------------
-- Joker scaling
--
-- The per-joker statistic list rests on this hook. Steamodded's
-- lovely/scaling.toml rewrites the vanilla scaling jokers to call
-- SMODS.scale_card (smods/src/utils.lua:3357), so one wrapper sees them all,
-- plus any modded joker using the same API.
--------------------------------------------------------------------------

--- scale_card's first argument is not always the card that owns the value:
--- Madness passes `context.blueprint_card or self` while mutating its own
--- ability (card.lua:2901), so a copied Madness would otherwise be logged
--- against the Blueprint. Resolve by finding who actually owns the table.
local function resolve_owner(card, ref_table)
    local ab = card and card.ability
    if ab and (ref_table == ab or ref_table == ab.extra) then return card end
    for _, j in ipairs((G.jokers and G.jokers.cards) or {}) do
        local jab = j.ability
        if jab and (ref_table == jab or ref_table == jab.extra) then return j end
    end
    return card
end

local function scaling_observer(event_name)
    return function(args)
        local card, a = args[1], args[2]
        if not (card and a and a.ref_value) then return nil end
        local ref_table = a.ref_table or (card.ability and card.ability.extra)
        if type(ref_table) ~= 'table' then return nil end
        return { ref_table = ref_table, field = a.ref_value, from = ref_table[a.ref_value] }
    end, function(args, _, pre)
        if not pre then return end
        local a = args[2]
        local to = pre.ref_table[pre.field]

        -- Both scale_card and reset_card bail out early when G.deck is absent
        -- (utils.lua:3358, :3410) without touching anything; emitting then
        -- would litter the log with no-op scales.
        if to == pre.from then return end

        local owner = resolve_owner(args[1], pre.ref_table)
        local center = owner and owner.config and owner.config.center

        emit(event_name, {
            id    = owner and owner.sort_id,
            key   = center and center.key,
            name  = center and center.name,
            field = pre.field,
            from  = util.num(pre.from),
            to    = util.num(to),
            op    = type(a.operation) == 'string' and a.operation or (a.operation and 'fn' or '+'),
        })
    end
end

do
    local before, after = scaling_observer('joker.scale')
    util.hook_around(SMODS, 'scale_card', before, after)
end

-- Campfire, Hit the Road, Ride the Bus and Obelisk reset through a separate
-- function (utils.lua:3409). Maxima survive without it, but the value series
-- would show unexplained drops, and "rounds survived" style stats need the
-- reset points.
do
    local before, after = scaling_observer('joker.reset')
    util.hook_around(SMODS, 'reset_card', before, after)
end

--------------------------------------------------------------------------
-- Playing hands
--------------------------------------------------------------------------

-- state_events.lua:586. The played cards have already moved from G.hand into
-- G.play by the time this runs, and are cleared afterwards, so capture on the
-- way in. Hand and discard counters are captured here too: the ease_* calls
-- that decrement them are queued, and the two are not even consistent with
-- each other, so everything is reported with explicit "before" semantics.
util.hook_around(G.FUNCS, 'evaluate_play', function()
    local round = (G.GAME or {}).current_round or {}
    return {
        cards = util.cards(G.play),
        blind_chips = G.GAME and G.GAME.blind and G.GAME.blind.chips,
        chips_before = G.GAME and G.GAME.chips,
        hands_left_before = round.hands_left,
        discards_left_before = round.discards_left,
        -- Jokers are sampled AFTER the hand resolves, below. Sampling here
        -- would capture their pre-scoring values, which is the opposite of
        -- what a "max value" statistic wants.
    }
end, function(_, _, pre)
    local game = G.GAME or {}
    -- G.GAME.last_hand_played is the internal hand key, set synchronously at
    -- state_events.lua:592. current_round.current_hand.handname holds the
    -- LOCALIZED display string, so it is not comparable across languages and
    -- will not index G.GAME.hands outside English.
    local hand_key = game.last_hand_played
    local hand = hand_key and game.hands and game.hands[hand_key]

    emit('hand.play', {
        cards       = pre and pre.cards,
        hand        = hand_key,
        level       = util.num(hand and hand.level),
        score       = util.num(SMODS.last_hand_score),
        blind_chips = util.num(pre and pre.blind_chips),
        -- G.GAME.chips is raised by a queued ease (state_events.lua:871), so
        -- the running total after this hand is chips_before + score.
        chips_before = util.num(pre and pre.chips_before),
        -- SMODS.last_hand_oneshot is "this single hand beat the blind", not
        -- "the blind is now cleared" -- a three-hand clear is false on every
        -- hand including the last.
        oneshot     = SMODS.last_hand_oneshot and true or false,
        hands_left_before = pre and pre.hands_left_before,
        discards_left_before = pre and pre.discards_left_before,
        -- Post-scoring, so an accumulator that grew during this hand shows its
        -- new value here rather than a hand late.
        jokers      = joker_sample(),
    })
    log.flush()
end)

-- state_events.lua:389
util.hook_around(G.FUNCS, 'discard_cards_from_highlighted', function()
    local round = (G.GAME or {}).current_round or {}
    return {
        cards = util.cards(G.hand and G.hand.highlighted),
        discards_left_before = round.discards_left,
    }
end, function(_, _, pre)
    emit('hand.discard', {
        cards = pre and pre.cards,
        discards_left_before = pre and pre.discards_left_before,
    })
end)

-- common_events.lua:471
util.hook_around(_G, 'level_up_hand', function(args)
    local hand = args[2]
    local h = hand and G.GAME and G.GAME.hands and G.GAME.hands[hand]
    return { hand = hand, from = h and h.level }
end, function(args, _, pre)
    if not (pre and pre.hand) then return end
    local h = G.GAME.hands[pre.hand]
    emit('hand.levelup', {
        hand   = pre.hand,
        from   = util.num(pre.from),
        to     = util.num(h and h.level),
        -- level_up_hand defaults this internally (common_events.lua:472), so
        -- the argument is nil for the common single-level case.
        amount = args[4] or 1,
    })
end)

--------------------------------------------------------------------------
-- Money
--
-- Every balance change funnels through ease_dollars (common_events.lua:68),
-- which queues its mutation unless `instant` is passed -- and no gameplay call
-- site passes it. So no after-hook anywhere can observe a new balance, and
-- recording the deltas here is both exact and simpler than trying.
--------------------------------------------------------------------------

util.hook(_G, 'ease_dollars', function(args)
    local delta = args[1]
    if not delta or delta == 0 then return end
    emit('money.change', {
        delta  = util.num(delta),
        before = util.num(G.GAME and G.GAME.dollars),
    })
end)

--------------------------------------------------------------------------
-- Round cash out
--
-- evaluate_round (state_events.lua:981) builds the payout from separately
-- named rows before totalling, so collecting the rows gives the breakdown --
-- which blind reward, which joker, how much interest -- rather than one
-- opaque number. Every add_round_eval_row call in it is inline, so this is one
-- of the few places an after-hook sees the real thing.
--------------------------------------------------------------------------

local cashout_rows = nil
local cashout_total = nil

-- common_events.lua:1178. The last row is named 'bottom' and carries the
-- authoritative total (state_events.lua:1118), taken from there rather than
-- summed so that adjustments made through the modify_final_cashout context
-- are included. Collecting here also captures rows past the 7-row cap the UI
-- itself drops (common_events.lua:1185).
util.hook(_G, 'add_round_eval_row', function(args)
    if not cashout_rows then return end
    local cfg = args[1] or {}
    if cfg.name == 'bottom' then
        cashout_total = cfg.dollars
        return
    end
    cashout_rows[#cashout_rows + 1] = {
        name    = cfg.name,
        dollars = util.num(cfg.dollars),
        disp    = cfg.disp,
        key     = cfg.card and cfg.card.config and cfg.card.config.center
                  and cfg.card.config.center.key,
    }
end)

util.hook_around(G.FUNCS, 'evaluate_round', function()
    cashout_rows, cashout_total = {}, nil
    return { dollars_before = G.GAME and G.GAME.dollars }
end, function(_, _, pre)
    local rows, total = cashout_rows, cashout_total
    cashout_rows, cashout_total = nil, nil

    if not total then
        total = 0
        for _, r in ipairs(rows or {}) do
            if type(r.dollars) == 'number' then total = total + r.dollars end
        end
    end

    emit('round.end', {
        items = rows,
        total = util.num(total),
        dollars_before = util.num(pre and pre.dollars_before),
        score = util.num(G.GAME and G.GAME.chips),
        blind = G.GAME and G.GAME.blind and G.GAME.blind.name,
        jokers = joker_sample(),
        -- Deck composition once per round: too expensive per hand, but the
        -- only way to derive Stone Joker, Hiker and max deck size.
        deck = util.cards(deck_cards()),
        deck_size = deck_size(),
    })
    log.flush()
end)

--------------------------------------------------------------------------
-- Blinds
--------------------------------------------------------------------------

-- blind.lua:99. G.FUNCS.select_blind does all its work in queued events, so an
-- after-hook there reads the PREVIOUS round's blind (and, on the run's first
-- blind, an empty Blind with chips = 0). Blind:set_blind is where the
-- selection actually lands, via new_round (state_events.lua:280).
util.hook(Blind, 'set_blind', function(args)
    local self, proto, reset = args[1], args[2], args[3]
    if reset or not proto then return end
    emit('round.start', {
        blind_key = self.config and self.config.blind and self.config.blind.key,
        name      = self.name,
        chips     = util.num(self.chips),
        boss      = self.boss and true or false,
        reward    = self.dollars,
        ante      = util.num(G.GAME and G.GAME.round_resets and G.GAME.round_resets.ante),
    })
end)

-- button_callbacks.lua:2563. Kept as the record of the player's DECISION
-- (round.start is the consequence), and as the point the endless latch flips.
util.hook(G.FUNCS, 'select_blind', function(args)
    local proto = args[1] and args[1].config and args[1].config.ref_table
    local flipped = state.note_blind_selected()
    emit('blind.select', {
        blind_key = proto and proto.key,
        name      = proto and proto.name,
        entered_endless = flipped or nil,
    })
end)

-- button_callbacks.lua:2795. G.GAME.skips is updated synchronously (:2810) so
-- it is accurate here, but blind_on_deck has already been advanced to the
-- blind being skipped TO (:2816), so the skipped blind is taken from the
-- argument instead.
util.hook_around(G.FUNCS, 'skip_blind', function()
    return { skipped = G.GAME and G.GAME.blind_on_deck }
end, function(_, _, pre)
    local game = G.GAME or {}
    local tags = game.tags or {}
    local tag = tags[#tags]
    emit('blind.skip', {
        blind_on_deck = pre and pre.skipped,
        ante  = util.num(game.round_resets and game.round_resets.ante),
        skips = game.skips,
        tag   = tag and (tag.key or tag.name),
    })
end)

--------------------------------------------------------------------------
-- Shop
--------------------------------------------------------------------------

-- button_callbacks.lua:2453. Returns false without buying when there is no
-- room (:2457), so the return value has to gate the event.
util.hook_around(G.FUNCS, 'buy_from_shop', function(args)
    local e = args[1]
    local card = e and e.config and e.config.ref_table
    if not card then return nil end
    return {
        card = util.card(card),
        cost = card.cost,
        -- buy_and_use is the same entry point, discriminated by the button id.
        and_use = e.config.id == 'buy_and_use' or nil,
    }
end, function(_, rets, pre)
    if not pre or rets[1] == false then return end
    emit('shop.buy', {
        card = pre.card,
        cost = util.num(pre.cost),
        and_use = pre.and_use,
    })
end)

-- button_callbacks.lua:2371 delegates to Card:sell_card (card.lua:1916), which
-- is the single sell funnel. Flagging the card here lets the removal hook tell
-- a sale from a destruction -- sell_card dissolves the card (card.lua:1949),
-- so without this every sale also logs as "destroyed".
util.hook_around(Card, 'sell_card', function(args)
    local card = args[1]
    if not card then return nil end
    card.bdb_selling = true
    return { card = util.card(card), value = card.sell_cost }
end, function(_, _, pre)
    if not pre then return end
    emit('shop.sell', { card = pre.card, value = util.num(pre.value) })
end)

-- button_callbacks.lua:2918. The shop is torn down and repopulated in a queued
-- event (:2929), so the new contents cannot be read here; shop.enter reports
-- them once they exist.
util.hook_around(G.FUNCS, 'reroll_shop', function()
    return { cost = G.GAME and G.GAME.current_round and G.GAME.current_round.reroll_cost }
end, function(_, _, pre)
    emit('shop.reroll', { cost = util.num(pre and pre.cost) })
end)

-- button_callbacks.lua:2179. Early-returns without using the card when
-- check_use rejects it (:2187), so the return has to gate the event. This one
-- entry point also handles booster packs and vouchers (:2202, :2213, :2297),
-- which is where pack.open, pack.pick and voucher redemption come from -- the
-- card's `set` says which.
util.hook_around(G.FUNCS, 'use_card', function(args)
    local e = args[1]
    local card = e and e.config and e.config.ref_table
    if not card then return nil end
    return {
        card = util.card(card),
        set = card.ability and card.ability.set,
        targets = util.cards(G.hand and G.hand.highlighted),
    }
end, function(_, rets, pre)
    if not pre or rets[1] == false then return end
    local set = pre.set
    local etype = (set == 'Booster' and 'pack.open')
        or (set == 'Voucher' and 'voucher.redeem')
        or 'consumable.use'
    emit(etype, { card = pre.card, targets = pre.targets })
end)

--------------------------------------------------------------------------
-- Cards entering and leaving
--
-- Card:add_to_deck (card.lua:748) and Card:remove (card.lua:5169) are the
-- universal funnels. The earlier choices here were wrong: add_joker takes a
-- centre KEY string rather than a Card and is only reachable from challenge
-- setup and debug, create_playing_card has two callers, and start_dissolve
-- misses Card:shatter (every Glass card, state_events.lua:820) and
-- SMODS.pinch_and_remove (Gros Michel / Cavendish, card.lua:3428).
--------------------------------------------------------------------------

--- Which family of event a card belongs to.
---
--- Card:add_to_deck is called for jokers, consumables and playing cards alike
--- (buy_from_shop runs it for every purchase at button_callbacks.lua:2468), so
--- without this every tarot bought counted as a card entering the deck. One
--- real ante-6 run logged 67 Tarot, 20 Planet and 7 Spectral acquisitions as
--- card.add, which made deck size underivable from the event stream.
local CONSUMABLE_SETS = { Tarot = true, Planet = true, Spectral = true }

local function card_event_name(c)
    local set = c.ability and c.ability.set
    if set == 'Joker' then return 'joker' end
    if CONSUMABLE_SETS[set] then return 'consumable' end
    if set == 'Voucher' then return 'voucher' end
    return 'card'
end

--------------------------------------------------------------------------
-- Modification vs. acquisition
--
-- Card:set_ability (card.lua:255) re-applies a card by calling
-- remove_from_deck() -- which clears the added_to_deck guard (card.lua:834) --
-- and adding it back at :486. So enhancing an existing card through a tarot is
-- indistinguishable from acquiring a new one unless the round trip is marked.
-- The same run above logged 40 such enhancements as card.add.
--------------------------------------------------------------------------

util.hook_around(Card, 'set_ability', function(args)
    local card = args[1]
    if not card then return nil end
    card.bdb_reapplying = true
    return { card = util.card(card) }
end, function(args, _, pre)
    local card = args[1]
    if not card then return end
    card.bdb_reapplying = nil
    if not (pre and card.added_to_deck) then return end
    emit('card.modify', { from = pre.card, to = util.card(card), what = 'ability' })
end)

-- card.lua:608. Seals are set directly rather than through set_ability, so
-- they need their own observation point.
util.hook_around(Card, 'set_seal', function(args)
    local card = args[1]
    return card and { seal = card.seal } or nil
end, function(args, _, pre)
    local card = args[1]
    if not (pre and card and card.added_to_deck) then return end
    if pre.seal == card.seal then return end
    emit('card.modify', { to = util.card(card), what = 'seal', from_seal = pre.seal })
end)

util.hook_around(Card, 'add_to_deck', function(args)
    local card = args[1]
    if not card then return nil end
    -- Mid-set_ability: this is the card being put back, not acquired.
    if card.bdb_reapplying then return nil end
    -- add_to_deck is idempotent via this flag (card.lua:752); only the first
    -- call is a real acquisition.
    return not card.added_to_deck or nil
end, function(args, _, pre)
    if not pre then return end
    local card = args[1]
    emit(card_event_name(card) .. '.add', {
        card = util.card(card),
        area = card.area == G.jokers and 'jokers'
            or card.area == G.consumeables and 'consumables'
            or card.area == G.deck and 'deck' or nil,
    })
end)

util.hook_around(Card, 'remove', function(args)
    local card = args[1]
    -- Tearing a run down removes every card and joker (Game:delete_run sets
    -- this at game.lua:1178). Those are not in-game destructions and logging
    -- them would report every run's entire final deck as destroyed.
    if G.in_delete_run then return nil end
    if card and card.bdb_reapplying then return nil end
    -- Only cards that were actually part of the run. Screen-wipe cards
    -- (button_callbacks.lua:3237) and the unlock-overlay card
    -- (UI_definitions.lua:4524) are real Card objects that dissolve through
    -- the same path, and would otherwise log as phantom removals.
    if not (card and card.added_to_deck) then return nil end
    return {
        card = util.card(card),
        reason = card.bdb_selling and 'sold' or 'destroyed',
        name = card_event_name(card),
    }
end, function(_, _, pre)
    if not pre then return end
    emit(pre.name .. '.remove', { card = pre.card, reason = pre.reason })
end)

--------------------------------------------------------------------------
-- Snapshots
--
-- Backstop against a hook going missing after a game update. Note that
-- save_run is not a timer: it is called at discrete moments and refuses to run
-- during every booster-pack state (misc_functions.lua:1588), so this is a
-- floor on coverage, not a guarantee. The per-hand and per-round joker samples
-- are what actually make the derived-joker maxima exact.
--------------------------------------------------------------------------

local SNAPSHOT_INTERVAL = 30

util.hook(_G, 'save_run', function()
    if not state.active then return end
    local now = love.timer.getTime()
    if now - last_snapshot < SNAPSHOT_INTERVAL then return end
    last_snapshot = now

    local game = G.GAME or {}

    local hands_played = {}
    for name, h in pairs(game.hands or {}) do
        if h.played and h.played > 0 then
            hands_played[name] = { played = h.played, level = util.num(h.level) }
        end
    end

    emit('snapshot', {
        dollars   = util.num(game.dollars),
        chips     = util.num(game.chips),
        deck_size = deck_size(),
        jokers    = joker_sample(),
        consumables = util.nonempty(util.cards(G.consumeables)),
        hands     = util.nonempty(hands_played),
        skips     = game.skips,
        consumeable_usage = util.nonempty(game.consumeable_usage),
        -- consumeable_usage_total is what Fortune Teller actually reads.
        consumeable_usage_total = util.nonempty(game.consumeable_usage_total),
        vouchers  = util.nonempty(game.used_vouchers),
        tag_tally = game.tag_tally,
    })
    -- save_run is already a disk moment, so flushing here costs nothing extra.
    log.flush()
end)

--------------------------------------------------------------------------
-- Shutdown
--
-- Quitting the process from inside a run never passes through MAIN_MENU, so
-- the stage watcher does not fire. Closing the run here is what distinguishes
-- a deliberate quit from a crash in the log.
--
-- The wrapper returns no values, and LÖVE treats a truthy love.quit return as
-- "cancel the quit" (main.lua:964), so this cannot trap the player in the game.
--------------------------------------------------------------------------

if type(love.quit) == 'function' then
    util.hook(love, 'quit', function()
        if state.active then
            end_run(save_exists() and 'suspended' or 'exited')
        else
            log.flush()
        end
    end)
else
    love.quit = function()
        if state.active then end_run(save_exists() and 'suspended' or 'exited') else log.flush() end
    end
end

return hooks
