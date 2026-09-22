--- A BalatroDB button on the main menu that starts the dashboard.
---
--- Steamodded has no API for adding a main-menu button; it adds its own by
--- wrapping the global that builds them (smods/src/ui.lua:2025), so this does
--- the same. The node path into the button column is undocumented and
--- load-bearing, hence the guards -- a layout change upstream should cost the
--- button, not the mod.
---
--- The launcher is written by `python ingest/install.py`. The mod cannot work
--- out where the repo is: it sees itself through a junction at
--- Mods/BalatroDB, and on Windows `..` from a junction resolves against the
--- junction's own path rather than its target. Setup records the answer in the
--- data directory instead, which the mod already knows how to find.

local util = BalatroDB.util
local NFS = SMODS.NFS or require('nativefs')

local menu = {}

local LAUNCHER = 'BalatroDB/launch-dashboard.bat'   -- relative to the save dir

local function launcher_path()
    local info = NFS.getInfo and NFS.getInfo(LAUNCHER)
    if not info then return nil end
    return love.filesystem.getSaveDirectory() .. '/' .. LAUNCHER
end

--- Launch detached, so the game never waits on it.
function menu.launch()
    local path = launcher_path()
    if not path then
        return false, 'run: python ingest/install.py'
    end
    -- The .bat itself uses `start`, so os.execute returns immediately. Quoted
    -- because the path runs through %APPDATA% and will contain spaces.
    local ok = pcall(os.execute, ('start "" /MIN "%s"'):format(path))
    if not ok then
        return false, 'could not start the dashboard'
    end
    return true, 'dashboard starting...'
end

G.FUNCS.bdb_open_dashboard = function(e)
    local ok, msg = menu.launch()
    -- Report on the button itself: there is no console to look at, and a
    -- button that silently does nothing is indistinguishable from a broken
    -- one.
    local label = e and e.children and e.children[1] and e.children[1].children
        and e.children[1].children[1]
    if label and label.config then
        label.config.text = ok and 'Opening...' or 'Setup needed'
        label.config.colour = ok and G.C.GREEN or G.C.ORANGE
    end
    if ok then
        sendInfoMessage(msg, 'BalatroDB')
    else
        sendWarnMessage(msg, 'BalatroDB')
    end
end

--- Add the button by wrapping the menu builder, the way Steamodded does.
function menu.install()
    if type(create_UIBox_main_menu_buttons) ~= 'function' then
        sendWarnMessage('main menu builder missing; no dashboard button', 'BalatroDB')
        return false
    end
    local ref = create_UIBox_main_menu_buttons
    create_UIBox_main_menu_buttons = function(...)
        local m = ref(...)
        util.try(function()
            -- smods/src/ui.lua:2034 inserts into this same column.
            local col = m and m.nodes and m.nodes[1] and m.nodes[1].nodes
                and m.nodes[1].nodes[1] and m.nodes[1].nodes[1].nodes
            if not col then return end
            col[#col + 1] = UIBox_button({
                id = 'bdb_button',
                button = 'bdb_open_dashboard',
                label = { 'BalatroDB' },
                colour = SMODS.Gradients and G.C.BOOSTER or G.C.BOOSTER,
                minh = 1.55, minw = 1.85, col = true, scale = 0.45 * 1.2,
            })
        end)
        return m
    end
    return true
end

return menu
