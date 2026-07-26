--[[
    Utils.lua — shared helpers: logger, project paths.
]]

local LrPathUtils = import 'LrPathUtils'
local LrFileUtils = import 'LrFileUtils'
local LrLogger    = import 'LrLogger'
local LrDialogs   = import 'LrDialogs'

local Utils = {}

-- Logger: visible via log file + Lua Console.
local log = LrLogger('ABELr')
log:enable('print')   -- 'print' → Lua Console; switch to 'logfile' for a file
Utils.log = log

function Utils.logf(fmt, ...)
    log:trace(string.format(fmt, ...))
end

-- Project root = the plugin itself: since the "self-sufficient plugin" redesign,
-- ABELr.lrplugin/ embeds everything (app/, launch.ps1, bootstrap.ps1) — no more
-- need for a parent folder with the full project. Copying this single .lrplugin
-- folder is enough to install on another machine.
--   _PLUGIN.path = .../ABELr.lrplugin
-- (must stay consistent with AppLauncher.buildLaunchCommand, which computes
--  the root the same way.)
function Utils.projectRoot()
    return _PLUGIN.path
end

-- Python app folder (.../ABELr.lrplugin/app).
function Utils.appDir()
    return LrPathUtils.child(Utils.projectRoot(), 'app')
end

-- Temporary thumbnails directory (.../ABELr.lrplugin/tmp_thumbs), created on
-- first use. Single owner of this path + creation (PLAN.md N4b — was
-- duplicated as a private thumbsDir() inside Thumbnails.lua).
function Utils.thumbsDir()
    local dir = LrPathUtils.child(Utils.projectRoot(), 'tmp_thumbs')
    if not LrFileUtils.exists(dir) then
        LrFileUtils.createDirectory(dir)
    end
    return dir
end

function Utils.test()
    LrDialogs.message('ABELr', 'Hello World', 'info')
end

-- "key=value" listing of a table (diagnostic for received develop settings).
function Utils.dumpKeys(t)
    if type(t) ~= 'table' then return tostring(t) end
    local parts = {}
    for k, v in pairs(t) do
        parts[#parts + 1] = tostring(k) .. '=' .. tostring(v)
    end
    return table.concat(parts, ', ')
end

return Utils
