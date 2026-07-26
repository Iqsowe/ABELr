--[[
    Actions.lua — high-level actions triggered by menu / UI buttons.

    Each action runs inside postAsyncTaskWithContext (required: HTTP GET + POST,
    sleep). Displays user feedback via LrDialogs.
]]

local LrFunctionContext = import 'LrFunctionContext'
local LrDialogs         = import 'LrDialogs'

local AppLauncher = require 'AppLauncher'
local PollingLoop = require 'PollingLoop'
local HttpClient  = require 'HttpClient'

local Actions = {}

local function runAsync(name, fn)
    LrFunctionContext.postAsyncTaskWithContext(name, fn)
end

-- Starts the App (if needed), then the bridge. Full connection.
function Actions.connect()
    runAsync('ABELrConnect', function()
        local ok, msg = AppLauncher.start()
        -- Starts the bridge no matter what: the polling loop (re)connects
        -- on its own as soon as the App responds, even if the healthcheck timed out.
        PollingLoop.start()
        msg = msg .. '\nBridge active (polling 300ms).'
        -- LrDialogs.message('ABELr', msg, ok and 'info' or 'warning')
    end)
end

-- Relaunches the App (clean stop + restart), reactivates the bridge.
function Actions.relaunch()
    runAsync('ABELrRelaunch', function()
        local ok, msg = AppLauncher.relaunch()
        -- Same here: the bridge always starts and reconnects on its own.
        PollingLoop.start()
        msg = msg .. '\nBridge active (polling 300ms).'
        -- LrDialogs.message('ABELr', msg, ok and 'info' or 'warning')
    end)
end

-- Checks App + bridge status without launching anything.
function Actions.checkStatus()
    runAsync('ABELrStatus', function()
        local alive  = HttpClient.isAlive()
        local bridge = PollingLoop.isRunning()
        local msg = string.format(
            'Application: %s\nBridge: %s',
            alive and 'connected' or 'not started',
            bridge and 'active' or 'stopped')
        LrDialogs.message('ABELr — status', msg, 'info')
    end)
end

return Actions
