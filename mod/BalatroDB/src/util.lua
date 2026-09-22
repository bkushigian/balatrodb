--- Serialization helpers and defensive plumbing.
---
--- Everything here exists to satisfy one rule: BalatroDB must never break a
--- run. A stats mod that crashes the game is worse than no stats mod.

local util = {}

--- The JSON encoder formats numbers with "%.14g"
--- (smods/libs/json/json.lua:110), so anything needing more than 14
--- significant digits is written lossily and silently. That is well below the
--- 2^53 where doubles actually stop being exact, and it is squarely inside the
--- range Balatro scores reach around ante 9-12.
---
--- A magnitude threshold is NOT sufficient: 12345678901234.5 is below 1e14 and
--- still loses its fraction to %.14g. The only honest test is to perform the
--- round trip and compare.
local function round_trips(v)
    return tonumber(string.format('%.14g', v)) == v
end

--- Out-of-range values are emitted as {s = exact text, l = signed log10}.
---
--- The exact value has to survive, but a bare string is not enough: SQL MAX()
--- over text is lexicographic, so '9' outranks '1000' and every per-joker
--- maximum would be silently wrong. `l` gives the ingester something totally
--- ordered to sort on while `s` keeps the value. Signed, so that negatives
--- order correctly too (-1e20 -> -20 sorts below -1e5 -> -5).
local function big(exact, log10)
    return { s = exact, l = log10 }
end

--- LuaJIT (which Balatro runs) has math.log10; it was removed in Lua 5.3, and
--- the two-argument math.log that replaced it does not exist in 5.1. Cover
--- both so this file behaves identically in the game and under test.
local LOG10 = math.log(10)
local function log10(x)
    if math.log10 then return math.log10(x) end
    return math.log(x) / LOG10
end

--- The ordering key: sign(x) * log10(1 + |x|).
---
--- It has to be exactly the transform the ingester applies to ordinary
--- numbers, because wrapped and unwrapped values are sorted against each
--- other. A bare log10(|x|) is not interchangeable with it: for |x| < 1 it is
--- negative, so a small positive number sorted below zero and below every
--- negative one.
local function ord(x)
    local a = x < 0 and -x or x
    local l = log10(1 + a)
    return x < 0 and -l or l
end

--- Pull an exact string and a log10 out of a Talisman-style big number.
--- Their __tostring yields a numeral like "1.234e+567"; anything we cannot
--- parse still keeps its exact text and simply sorts last.
local function from_big_table(v)
    local ok, s = pcall(tostring, v)
    if not ok or type(s) ~= 'string' then return nil end

    local n = tonumber(s)
    if n and n == n and n ~= math.huge and n ~= -math.huge and round_trips(n) then
        return n
    end

    -- Representable as a double, just not by %.14g: the exact key applies.
    if n and n == n and n ~= math.huge and n ~= -math.huge then
        return big(s, ord(n))
    end

    -- Beyond a double (Talisman territory). Only the exponent is available,
    -- and at that magnitude log10(1 + x) and log10(x) are indistinguishable.
    local mant, exp = s:match('^(%-?[%d%.]+)[eE]%+?(%-?%d+)$')
    if mant and exp then
        local m, e = tonumber(mant), tonumber(exp)
        if m and e then
            local sign = m < 0 and -1 or 1
            local am = m < 0 and -m or m
            local l = e + (am > 0 and log10(am) or 0)
            return big(s, sign * l)
        end
    end
    return big(s, nil)
end

--- Coerce a value into something json.encode will accept without losing it.
function util.num(v)
    local t = type(v)

    if t == 'number' then
        if v ~= v then return big('nan', nil) end
        if v == math.huge then return big('inf', 1e308) end
        if v == -math.huge then return big('-inf', -1e308) end
        if not round_trips(v) then
            return big(string.format('%.17g', v), ord(v))
        end
        return v
    end

    if t == 'nil' or t == 'boolean' or t == 'string' then return v end

    if t == 'table' then
        local r = from_big_table(v)
        if r ~= nil then return r end
    end

    return tostring(v)
end

--- True for anything util.num can turn into a meaningful number: a Lua number,
--- or a big-number table (which always carries a metatable).
local function is_numeric(v)
    local t = type(v)
    return t == 'number' or (t == 'table' and getmetatable(v) ~= nil)
end

--- Run fn, swallowing any error into the log.
function util.try(fn, ...)
    local ok, err = pcall(fn, ...)
    if not ok then
        sendWarnMessage('hook error: ' .. tostring(err), 'BalatroDB')
    end
    return ok
end

local function pack(...) return select('#', ...), { ... } end

--- Wrap tbl[key] with observers that run before and/or after the original,
--- without disturbing its return values or letting our errors escape.
---
--- `before(args)` may return a value, handed to `after(args, rets, pre)`. That
--- matters for anything destructive: cards are consumed by the time play, sell
--- and use functions return, so their state has to be captured on the way in.
---
--- Return arity is preserved exactly, via select('#'), because several hooked
--- functions signal refusal by returning `false` and that has to reach the
--- caller intact.
---
--- Warns rather than erroring when the target is missing: a renamed function in
--- a future patch should cost us one event type, not the whole mod.
function util.hook_around(tbl, key, before, after)
    local ref = tbl and tbl[key]
    if type(ref) ~= 'function' then
        sendWarnMessage(('hook target %s is missing; events from it will not be recorded'):format(tostring(key)), 'BalatroDB')
        return false
    end
    tbl[key] = function(...)
        local args = { ... }
        local pre
        if before then
            local ok, v = pcall(before, args)
            if ok then pre = v
            else sendWarnMessage('pre-hook error: ' .. tostring(v), 'BalatroDB') end
        end
        local n, rets = pack(ref(...))
        if after then util.try(after, args, rets, pre) end
        return unpack(rets, 1, n)
    end
    return true
end

--- Wrap tbl[key] with an after-observer only.
function util.hook(tbl, key, observe)
    return util.hook_around(tbl, key, nil, observe)
end

--- Ability fields that hold accumulated or derived numeric state. Captured so
--- that a joker's value is recoverable from any sample, which is what lets
--- snapshots act as a backstop if a scaling hook ever breaks, and is the only
--- way to observe jokers that never call SMODS.scale_card at all.
---
--- Mapped to their inert value. Every card carries the whole set whether or
--- not it uses any of them, so emitting them unconditionally made default
--- zeros 44% of a real log -- a 40-card deck sample repeated these thirteen
--- fields forty times, all inert. An absent field means the default.
local ABILITY_NUMERIC = {
    mult = 0, x_mult = 1, chips = 0, x_chips = 1,
    h_mult = 0, h_x_mult = 0, h_chips = 0,
    t_mult = 0, t_chips = 0,
    perma_bonus = 0, perma_h_x_mult = 0, bonus = 0,
    p_dollars = 0, h_dollars = 0,
    extra_value = 0, caino_xmult = 1, invis_rounds = 0,
    -- Stone Joker's value is extra * stone_tally (card.lua:4315); without the
    -- tally the joker's contribution is unrecoverable from a sample.
    stone_tally = 0,
}

local STICKERS = { 'eternal', 'perishable', 'rental' }

--- Compact card serialization. Only non-default fields are emitted, so an
--- unmodified playing card is just {id, rank, suit}.
function util.card(c)
    if type(c) ~= 'table' then return nil end
    local out = {}

    -- Balatro's own identity, assigned in Card:init (card.lua:24) and
    -- round-tripped through saves. The game uses it for its own one-action
    -- replay, and without it two copies of the same joker are indistinguishable.
    out.id = c.sort_id

    local center = c.config and c.config.center
    if center then
        out.key = center.key
        out.name = center.name
    end

    if c.base then
        out.rank = c.base.value
        out.suit = c.base.suit
    end

    local ab = c.ability
    if ab then
        -- 'Default' is an unmodified playing card, which rank and suit already
        -- say; only the interesting sets are worth a field.
        if ab.set and ab.set ~= 'Default' then out.set = ab.set end
        -- ability.name is the center's name for every card type (card.lua:345),
        -- so it only means "enhancement" for actually-enhanced playing cards.
        -- Reading it unconditionally labels every joker with an enhancement.
        if ab.set == 'Enhanced' and ab.name and ab.name ~= 'Default Base' then
            out.enhancement = ab.name
        end
        if out.name == 'Default Base' then out.name = nil end

        local st = {}
        for k, default in pairs(ABILITY_NUMERIC) do
            local v = ab[k]
            if is_numeric(v) and v ~= default then st[k] = util.num(v) end
        end
        if is_numeric(ab.extra) then
            st.extra = util.num(ab.extra)
        elseif type(ab.extra) == 'table' then
            -- extra is per-joker, so there is no default to compare against;
            -- it is kept whole.
            for k, v in pairs(ab.extra) do
                if is_numeric(v) then st['extra.' .. tostring(k)] = util.num(v) end
            end
        end
        out.state = next(st) and st or nil

        local stickers
        for _, k in ipairs(STICKERS) do
            if ab[k] then
                stickers = stickers or {}
                stickers[#stickers + 1] = k
            end
        end
        -- pinned lives on the card, not on ability (card.lua:1148).
        if c.pinned then
            stickers = stickers or {}
            stickers[#stickers + 1] = 'pinned'
        end
        out.stickers = stickers
    end

    if c.edition then out.edition = c.edition.key or c.edition.type end
    if c.seal then out.seal = c.seal end
    if c.debuff then out.debuffed = true end
    if c.sell_cost then out.sell_cost = util.num(c.sell_cost) end

    return out
end

--- Serialize a CardArea (or any list of cards).
---
--- Uses a running index rather than preserving positions: assigning nil into a
--- numeric slot produces a sparse array, and the JSON encoder rejects those
--- outright (json.lua:77), which would cost the whole event rather than one
--- card.
function util.cards(area)
    local list = area and (area.cards or area)
    if type(list) ~= 'table' then return nil end
    local out, n = {}, 0
    for _, c in ipairs(list) do
        local s = util.card(c)
        if s then
            n = n + 1
            out[n] = s
        end
    end
    return out
end

--- nil for an empty table, the table otherwise.
---
--- The encoder treats any table with no entries as an array (json.lua:68), so
--- an empty map serializes as [] and an ingester expecting an object gets a
--- list. Omitting it is unambiguous.
function util.nonempty(t)
    if type(t) ~= 'table' then return t end
    return next(t) and t or nil
end

--- Reverse lookup of G.STATES, built once and cached.
local state_names
function util.state_name(n)
    if not state_names then
        state_names = {}
        for k, v in pairs(G.STATES or {}) do state_names[v] = k end
    end
    return state_names[n] or tostring(n)
end

--- A private generator, deliberately NOT math.random.
---
--- Balatro draws on math.random for real gameplay decisions (get_pack,
--- common_events.lua:2277), so consuming from the shared stream would shift
--- outcomes. Observing a run must not change it.
---
--- MINSTD (Lehmer) rather than a power-of-two LCG, for two reasons. LuaJIT has
--- no integers -- every number is a double -- so the product has to stay under
--- 2^53 or the low bits are silently rounded away: 16807 * (2^31-2) is about
--- 3.6e13, comfortably inside. And a prime modulus avoids the short low-bit
--- cycles that make `% 16` of a mod-2^31 generator nearly constant.
local RNG_M = 2147483647  -- 2^31 - 1, prime
local RNG_A = 16807
local rng_state = nil

local function rng()
    if not rng_state then
        local t = os.time() or 0
        local c = math.floor(((os.clock() or 0) * 1000000) % 1000000)
        rng_state = ((t % RNG_M) * 1000 + c) % (RNG_M - 1) + 1
    end
    rng_state = (RNG_A * rng_state) % RNG_M
    return rng_state
end

local HEX = '0123456789abcdef'
function util.short_id(len)
    local s = ''
    for _ = 1, (len or 4) do
        -- Draw from the high bits: they mix much faster than the low ones.
        local i = math.floor(rng() / 65536) % 16 + 1
        s = s .. HEX:sub(i, i)
    end
    return s
end

--- Exposed so a test can assert the double-safety invariant directly. The
--- in-game failure mode is silent, and the host interpreter used for testing
--- has real integers, so it cannot reproduce it by running the generator.
util.RNG_MAX_PRODUCT = RNG_A * (RNG_M - 1)

return util
