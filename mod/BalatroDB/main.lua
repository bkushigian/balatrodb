--- BalatroDB
--- Append-only run event logger. See docs/event-schema.md.
---
--- Load order matters: util has no dependencies, log needs util, state needs
--- log, env needs BalatroDB.VERSION, hooks needs all of them. hooks installs
--- itself on load and returns a small table.
---
--- The whole of setup runs inside a pcall. Steamodded executes mod main files
--- with no protection of its own -- `assert(load(...))()` at
--- smods/src/preflight/loader.lua:783, reached from Game:start_up with no
--- pcall anywhere on the chain -- so an error raised here does not disable
--- BalatroDB, it stops Balatro from booting. A broken logger must leave the
--- game playable.

BalatroDB = {
    VERSION = '0.4.3',
    SCHEMA = 1,
    mod = SMODS.current_mod,
    ok = false,
}

local function setup()
    local function module(rel)
        local chunk, err = SMODS.load_file(rel)
        if not chunk then
            error(('could not load %s: %s'):format(rel, tostring(err)), 0)
        end
        return chunk()
    end

    BalatroDB.util  = module('src/util.lua')
    BalatroDB.log   = module('src/log.lua')
    BalatroDB.env   = module('src/env.lua')
    BalatroDB.state = module('src/state.lua')
    BalatroDB.hooks = module('src/hooks.lua')
    BalatroDB.menu  = module('src/menu.lua')
    BalatroDB.menu.install()
end

local ok, err = pcall(setup)

if ok then
    BalatroDB.ok = true
    sendInfoMessage('BalatroDB ' .. BalatroDB.VERSION .. ' loaded', 'BalatroDB')
else
    -- Inert, not fatal. Any hooks installed before the failure remain, but
    -- they all no-op because log.emit returns early while no file is open.
    sendWarnMessage('BalatroDB disabled: ' .. tostring(err), 'BalatroDB')
end
