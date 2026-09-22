--- Environment capture: which mods were active for this run.
---
--- A statistic is only comparable against runs from a comparable environment,
--- so every run records what was loaded. v1 records identity and version only;
--- per-mod configuration is deliberately out of scope.

local env = {}

--- Steamodded registers two synthetic entries in SMODS.Mods, 'Balatro' and
--- 'Lovely', whose versions are the game build and the injector build
--- (smods/src/preflight/loader.lua:55-66). That gives us exact platform
--- versions without parsing anything ourselves.
local META = { Balatro = 'game', Lovely = 'lovely' }

function env.capture()
    local out = { mods = {}, balatrodb = BalatroDB.VERSION }

    for id, mod in pairs(SMODS.Mods or {}) do
        -- An id maps to an array instead of a mod when several versions of the
        -- same mod are installed; the loader resolves one (loader.lua:485),
        -- and that resolved entry is what carries .id.
        if type(mod) == 'table' and mod.id then
            -- Steamodded's own entry has no can_load field, so it has to be
            -- matched before the active test or it would be dropped.
            if META[id] then
                out[META[id]] = mod.version
            elseif id == (SMODS.id or 'Steamodded') then
                out.smods = mod.version
            elseif id == 'BalatroDB' then
                -- Already reported as out.balatrodb, from the constant the
                -- code actually runs on rather than the manifest.
            elseif mod.can_load and not mod.disabled then
                out.mods[#out.mods + 1] = {
                    id = mod.id,
                    name = mod.name ~= mod.id and mod.name or nil,
                    version = mod.version,
                }
            end
        end
    end

    table.sort(out.mods, function(a, b) return a.id < b.id end)
    return out
end

--- The stake's key, not just its index.
---
--- G.GAME.stake is a position in G.P_CENTER_POOLS.Stake, and that ordering
--- shifts when stake-adding mods come and go -- so "stake 8" stops meaning
--- Gold across your own history. The key is stable.
function env.stake_key()
    local pool = G.P_CENTER_POOLS and G.P_CENTER_POOLS.Stake
    local stake = G.GAME and G.GAME.stake
    local entry = pool and stake and pool[stake]
    return entry and entry.key or nil
end

return env
