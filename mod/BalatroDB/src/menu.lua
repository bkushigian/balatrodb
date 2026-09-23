--- A BalatroDB entry in the Options menu that starts the dashboard.
---
--- Steamodded has no API for adding one, so this wraps the global that builds
--- the menu, the way Steamodded adds its own buttons (smods/src/ui.lua:2025).
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
    --
    -- `cmd /c` is explicit because START runs a .BAT under `cmd /K`, which
    -- leaves the console open forever once the batch finishes. Those shells
    -- stay in Balatro's process tree, so Steam goes on reporting the game as
    -- running long after it has quit.
    local ok = pcall(os.execute, ('start "" /MIN cmd /c "%s"'):format(path))
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

--- Put the button in the Options menu rather than on the main menu.
---
--- The main menu column is shared with Steamodded's own Mods button and
--- whatever else is installed, and every addition squeezes the rest sideways.
--- Options has room, is reachable from both the main menu and a run, and is
--- where a player looks for a tool rather than for something to play.
---
--- create_UIBox_options (UI_definitions.lua:2208) hands its buttons to
--- create_UIBox_generic_options, which nests them at
---     ROOT > R > C > R.nodes
--- with the Back button as a sibling of that R, so appending here lands the
--- button above Back and below the game's own entries. The path is
--- undocumented, hence the guard: a layout change upstream should cost the
--- button, not the mod.
function menu.install()
    if type(create_UIBox_options) ~= 'function' then
        sendWarnMessage('options builder missing; no dashboard button', 'BalatroDB')
        return false
    end
    local ref = create_UIBox_options
    create_UIBox_options = function(...)
        local ui = ref(...)
        util.try(function()
            local row = ui and ui.nodes and ui.nodes[1] and ui.nodes[1].nodes
                and ui.nodes[1].nodes[1] and ui.nodes[1].nodes[1].nodes
                and ui.nodes[1].nodes[1].nodes[1]
            local list = row and row.nodes
            if not list then return end
            list[#list + 1] = UIBox_button({
                id = 'bdb_button',
                button = 'bdb_open_dashboard',
                label = { 'BalatroDB' },
                -- minw 5 is what every other row in this menu uses.
                minw = 5,
            })
        end)
        return ui
    end
    return true
end

return menu
