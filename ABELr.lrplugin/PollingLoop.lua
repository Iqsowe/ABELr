--[[
    PollingLoop.lua — plugin ↔ App bridge.

    Async loop: GET /jobs/pending every 300ms, executes the job via the SDK,
    POST the result to /jobs/{id}/result. Runs inside postAsyncTaskWithContext
    (required by LrHttp.post). Automatically reconnects if the App restarts.

    Anti-duplicate guard via a global flag: only one active bridge per Lr session.

    Hot-reload: `dispatch` is stored in _G.ABELR_DISPATCH and updated on every
    module reload. The running loop (`pollOnce`) calls it via the global — it
    automatically picks up the new code without a restart.
]]

local LrApplication     = import 'LrApplication'
local LrTasks           = import 'LrTasks'
local LrFunctionContext = import 'LrFunctionContext'

local HttpClient = require 'HttpClient'
local PhotoData  = require 'PhotoData'
local Adjustments= require 'Adjustments'
local Thumbnails = require 'Thumbnails'
local Metadata   = require 'Metadata'
local Collections= require 'Collections'
local Presets    = require 'Presets'
local Json       = require 'Json'
local Utils      = require 'Utils'

local PollingLoop = {}

local POLL_INTERVAL = 0.3
-- If no loop iteration has happened within this delay, the bridge is considered
-- dead (context killed without cleanup, fatal error…) and can be restarted.
local HEARTBEAT_TIMEOUT = 5

-- The bridge is "alive" if a loop has polled recently (fresh heartbeat).
-- We do NOT rely on a shared boolean flag: a dying loop must never be able
-- to shut down a more recent loop. The heartbeat alone is the source of
-- truth — it goes stale on its own once no loop is running anymore.
local function bridgeAlive()
    local hb = _G.ABELR_BRIDGE_HEARTBEAT or 0
    return (os.time() - hb) < HEARTBEAT_TIMEOUT
end

-- Joins up to 5 error strings with ' | ', appending a "+N more" suffix beyond
-- that. Shared by every job type that can partially fail (Fable 5 L-04):
-- batchResult, apply_adjustments, get_thumbnails, render_probe (N2a).
local function summarizeErrors(errors)
    if not errors or #errors == 0 then return nil end
    local parts = {}
    for i = 1, math.min(5, #errors) do parts[#parts + 1] = errors[i] end
    local summary = table.concat(parts, ' | ')
    if #errors > 5 then
        summary = summary .. string.format(' | +%d more', #errors - 5)
    end
    return summary
end

-- Builds the standard result table for a batch job (set_rating, set_keywords,
-- add_to_collection, apply_develop_preset…) from a report
-- { applied, total, errors }. Same errors_summary logic as apply_adjustments
-- (Fable 5 review L-04): a PARTIAL run doesn't lose the failure causes.
local function batchResult(jobId, report)
    local total   = report.total or 0
    local applied = report.applied or 0
    local errors  = report.errors or {}
    local status  = (applied > 0 or total == 0) and 'ok' or 'error'
    local errorsSummary = summarizeErrors(errors)
    local errMsg = nil
    if status == 'error' then
        errMsg = string.format('0/%d applied. %s',
            total, errors[1] or 'no matching photo')
    end
    return {
        job_id  = jobId,
        status  = status,
        error   = errMsg,
        errors_summary = errorsSummary,
        applied = applied,
        total   = total,
        photos  = Json.array({}),
    }
end

-- Executes a job, returns the result table to send back to the App.
local function dispatch(job)
    local jobId = job.job_id
    local jobType = job.type

    if jobType == 'test' then
        -- Test popup: displayed outside the loop so it doesn't block polling.
        LrTasks.startAsyncTask(function() Utils.test() end)
        return {
            job_id = jobId,
            status = 'ok',
            photos = Json.array({}),
        }
    elseif jobType == 'get_selected_photos' then
        return {
            job_id = jobId,
            status = 'ok',
            photos = PhotoData.getSelectedPhotos(),
        }
    elseif jobType == 'get_catalog_photos' then
        return {
            job_id = jobId,
            status = 'ok',
            photos = PhotoData.getAllPhotos(),
        }
    elseif jobType == 'get_thumbnails' then
        local payload  = job.payload or {}
        local width    = payload.width  or 512
        local height   = payload.height or 512
        -- Uses the current selection (the same list as get_selected_photos).
        local catalog  = LrApplication.activeCatalog()
        local photos   = catalog:getTargetPhotos()
        local thumbs   = Thumbnails.fetch(photos, width, height)
        -- Optional filter: if payload.photo_ids is provided, only return those.
        local filter   = {}
        if payload.photo_ids and #payload.photo_ids > 0 then
            for _, id in ipairs(payload.photo_ids) do filter[id] = true end
        end
        local out, errors, nOk = Json.array({}), {}, 0
        for _, t in ipairs(thumbs) do
            if not payload.photo_ids or #payload.photo_ids == 0 or filter[t.photo_id] then
                out[#out + 1] = {
                    photo_id       = t.photo_id,
                    thumbnail_path = t.thumbnail_path,
                    error          = t.error,
                }
                if t.thumbnail_path then
                    nOk = nOk + 1
                else
                    errors[#errors + 1] = t.error or ('unknown error: ' .. tostring(t.photo_id))
                end
            end
        end
        local total  = #out
        -- Real status (N2a): a batch where every thumbnail failed must not
        -- report 'ok' — the App used to trust this blindly.
        local status = (nOk > 0 or total == 0) and 'ok' or 'error'
        return {
            job_id         = jobId,
            status         = status,
            error          = status == 'error'
                and string.format('0/%d thumbnail(s). %s', total, errors[1] or 'unknown error')
                or nil,
            errors_summary = summarizeErrors(errors),
            applied        = nOk,
            total          = total,
            thumbnails     = out,
            photos         = Json.array({}),
        }
    elseif jobType == 'render_probe' then
        -- Probe render: applies temporary settings, renders the thumbnail, restores.
        -- Used to calibrate the ∂render/∂slider response and the neutral anchor render.
        local payload     = job.payload or {}
        local adjustments = payload.adjustments or {}
        local width       = payload.width  or 512
        local height      = payload.height or 512
        local settle      = payload.settle
        local thumbs      = Thumbnails.fetchProbe(adjustments, width, height, settle)
        local out, errors, nOk = Json.array({}), {}, 0
        for _, t in ipairs(thumbs) do
            out[#out + 1] = {
                photo_id       = t.photo_id,
                thumbnail_path = t.thumbnail_path,
                error          = t.error,
                asshot_temp    = t.asshot_temp,
                asshot_tint    = t.asshot_tint,
                restore_error  = t.restore_error,
            }
            if t.thumbnail_path then
                nOk = nOk + 1
            else
                errors[#errors + 1] = t.error or ('unknown error: ' .. tostring(t.photo_id))
            end
        end
        local total  = #out
        -- Real status (N2a): a probe batch where nothing rendered must not
        -- report 'ok' — mcp/tools.py already raises on status != 'ok', so a
        -- probe that used to "succeed" silently now fails loudly (the point).
        local status = (nOk > 0 or total == 0) and 'ok' or 'error'
        return {
            job_id         = jobId,
            status         = status,
            error          = status == 'error'
                and string.format('0/%d render(s). %s', total, errors[1] or 'unknown error')
                or nil,
            errors_summary = summarizeErrors(errors),
            applied        = nOk,
            total          = total,
            thumbnails     = out,
            photos         = Json.array({}),
        }
    elseif jobType == 'apply_adjustments' then
        local payload = job.payload or {}
        local adjustments = payload.adjustments or {}
        local report = Adjustments.apply(adjustments)
        local status = (report.applied > 0 or report.total == 0) and 'ok' or 'error'
        -- Error summary ALWAYS attached when there are any (Fable 5 review L-04):
        -- a PARTIAL apply (status='ok' with failures) no longer loses the causes.
        local errorsSummary = summarizeErrors(report.errors)
        local errMsg = nil
        if status == 'error' then
            errMsg = string.format('0/%d applied (%d matched). %s',
                report.total, report.matched,
                report.errors[1] or 'no matching photo')
        end
        return {
            job_id  = jobId,
            status  = status,
            error   = errMsg,
            errors_summary = errorsSummary,
            applied = report.applied,
            matched = report.matched,
            total   = report.total,
            photos  = Json.array({}),
        }

    -- ------------------------------------------------------------------ --
    -- Phase 2: ratings / flags / keywords / collections / develop presets
    -- ------------------------------------------------------------------ --
    elseif jobType == 'set_rating' then
        local p = job.payload or {}
        return batchResult(jobId, Metadata.setRating(p.photo_ids or {}, p.rating))
    elseif jobType == 'set_flag_color' then
        local p = job.payload or {}
        return batchResult(jobId, Metadata.setFlagColor(p.photo_ids or {}, p.flag, p.color))
    elseif jobType == 'set_keywords' then
        local p = job.payload or {}
        return batchResult(jobId, Metadata.setKeywords(p.photo_ids or {}, p.add or {}, p.remove or {}))
    elseif jobType == 'add_to_collection' then
        local p = job.payload or {}
        return batchResult(jobId, Collections.addPhotos(p.collection, p.photo_ids or {}))
    elseif jobType == 'apply_develop_preset' then
        local p = job.payload or {}
        return batchResult(jobId, Presets.apply(p.photo_ids or {}, p.preset))
    elseif jobType == 'list_collections' then
        return {
            job_id = jobId, status = 'ok', photos = Json.array({}),
            data = { collections = Collections.list() },
        }
    elseif jobType == 'create_collection' then
        local p = job.payload or {}
        return {
            job_id = jobId, status = 'ok', photos = Json.array({}),
            data = Collections.create(p.name, p.parent),
        }
    elseif jobType == 'list_develop_presets' then
        return {
            job_id = jobId, status = 'ok', photos = Json.array({}),
            data = { presets = Presets.list() },
        }
    end

    return {
        job_id = jobId,
        status = 'error',
        error  = 'unknown job type: ' .. tostring(jobType),
        photos = Json.array({}),
    }
end

local function pollOnce()
    local job, status, rawBody = HttpClient.get('/jobs/pending', 5)
    if status == nil then
        return false   -- App not started: will retry
    end
    if status == 204 then
        return true    -- connected, no job
    end
    if job == nil then
        -- 200 with an undecodable body ≠ "no job": the job was just popped
        -- on the App side (IN_PROGRESS) and would be lost until the 900s TTL. We
        -- can't recover it here, but we LOG it (Fable 5 review L-06).
        if status == 200 then
            Utils.logf('pollOnce: HTTP 200 but undecodable body (%d bytes) — job lost? body=%s',
                rawBody and #rawBody or 0, string.sub(tostring(rawBody), 1, 200))
        end
        return true
    end

    Utils.logf('Job received: type=%s id=%s', tostring(job.type), tostring(job.job_id))

    -- Call via global: gets the most recent dispatch after a plugin reload.
    local currentDispatch = _G.ABELR_DISPATCH or dispatch
    local ok, result = LrTasks.pcall(currentDispatch, job)
    if not ok then
        Utils.logf('Dispatch error: %s', tostring(result))
        result = {
            job_id = job.job_id,
            status = 'error',
            error  = tostring(result),
            photos = Json.array({}),
        }
    else
        Utils.logf('Dispatch OK: %d photo(s)', type(result.photos) == 'table' and #result.photos or -1)
    end

    local encOk, payload = LrTasks.pcall(Json.encode, result)
    if not encOk then
        Utils.logf('Json.encode error: %s', tostring(payload))
        payload = Json.encode({
            job_id = job.job_id,
            status = 'error',
            error  = 'encode failed: ' .. tostring(payload),
            photos = Json.array({}),
        })
    end

    -- POST the result with retries (Fable 5 review L-07): the job has already been
    -- EXECUTED (including apply) — losing the POST times out the App worker even
    -- though the work is done. status nil = network loss → 2 retries with backoff.
    local postStatus
    for attempt = 1, 3 do
        local _, st = HttpClient.postJsonRaw('/jobs/' .. job.job_id .. '/result', payload, 10)
        postStatus = st
        if postStatus ~= nil then break end
        Utils.logf('POST result: network failure (attempt %d/3), retrying…', attempt)
        LrTasks.sleep(0.5 * attempt)
    end
    if postStatus == nil then
        Utils.logf('POST result: GIVING UP after 3 attempts — result for job %s lost (work already executed)',
            tostring(job.job_id))
    else
        Utils.logf('POST result → HTTP %s', tostring(postStatus))
    end
    return true
end

-- Starts the bridge. ALWAYS starts a fresh loop, identified by a unique
-- generation token (_G.ABELR_BRIDGE_GEN). Starting increments the token:
-- any earlier loop (older generation) withdraws on its own on the next
-- iteration. So there is at most ONE live loop, with no shared boolean flag
-- that a dying loop could reset to false to kill the active loop.
--
-- Consequence: re-clicking "connect" always repairs the bridge (the new
-- loop supersedes any zombie instead of refusing to start).
function PollingLoop.start()
    -- Retires any loop from an earlier version of the module: it was watching
    -- the ABELR_BRIDGE_RUNNING boolean flag (not the generation).
    _G.ABELR_BRIDGE_RUNNING = false

    local gen = (_G.ABELR_BRIDGE_GEN or 0) + 1
    _G.ABELR_BRIDGE_GEN = gen
    _G.ABELR_BRIDGE_HEARTBEAT = os.time()

    LrFunctionContext.postAsyncTaskWithContext('ABELrBridge', function(context)
        -- The cleanup does NOT touch any shared state: a dying loop cannot
        -- shut down a more recent loop. Logging only (diagnostic).
        context:addCleanupHandler(function()
            Utils.logf('Bridge (gen %d): context cleaned up.', gen)
        end)
        Utils.logf('Bridge started (gen %d) → %s', gen, HttpClient.BASE_URL)

        -- Runs as long as this loop remains the current generation.
        while _G.ABELR_BRIDGE_GEN == gen do
            _G.ABELR_BRIDGE_HEARTBEAT = os.time()   -- heartbeat
            local ok, err = LrTasks.pcall(pollOnce)
            if not ok then
                Utils.logf('Loop error: %s', tostring(err))
            end
            LrTasks.sleep(POLL_INTERVAL)
        end
        Utils.logf('Bridge (gen %d) withdrawn in favor of gen %s.',
            gen, tostring(_G.ABELR_BRIDGE_GEN))
    end)
    return true
end

-- Stops the bridge: increments the generation without starting a loop → the
-- current loop withdraws and none replaces it (the heartbeat goes stale).
function PollingLoop.stop()
    _G.ABELR_BRIDGE_RUNNING = false
    _G.ABELR_BRIDGE_GEN = (_G.ABELR_BRIDGE_GEN or 0) + 1
end

function PollingLoop.isRunning()
    return bridgeAlive()
end

-- ─── Hot-reload ─────────────────────────────────────────────────────────────
-- Publishes the current dispatch into a global: the live loop calls it via _G
-- (see pollOnce), so reloading the module updates job handling without
-- restarting the loop. The loop's lifecycle is managed by the generation
-- (PollingLoop.start) — no more fragile migration block here.
_G.ABELR_DISPATCH = dispatch
-- ────────────────────────────────────────────────────────────────────────────

return PollingLoop
