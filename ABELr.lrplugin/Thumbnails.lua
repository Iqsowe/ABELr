--[[
    Thumbnails.lua — fetches JPEG thumbnails via requestJpegThumbnail.

    Writes each thumbnail to {projectRoot}/tmp_thumbs/{photo_id}_{gen}.jpg so the
    Python App can read them directly (same machine, no base64 encoding).
    {gen} = fetch generation (unique name per call, cf. Fable 5 review L-02);
    files from past generations are purged two fetches later.

    requestJpegThumbnail is async: we wait for callbacks via LrTasks.sleep.
    Timeout of THUMB_TIMEOUT seconds if Lr doesn't generate the thumbnail (missing preview).
]]

local LrApplication = import 'LrApplication'
local LrDate        = import 'LrDate'
local LrFileUtils   = import 'LrFileUtils'
local LrPathUtils   = import 'LrPathUtils'
local LrTasks       = import 'LrTasks'
local Utils         = require 'Utils'

local Thumbnails = {}

local THUMB_TIMEOUT = 15  -- floor: max seconds for a small batch of thumbnails
-- Fallback per-photo budget when the App doesn't ship a "timeout_s" (older
-- App, MCP tools) — PLAN.md N3c. The shared app.server.budget module is the
-- source of truth; this is only the resolution-scaled fallback formula,
-- kept in sync by test_lua_contract.py.
local THUMB_SECONDS_PER_PHOTO = 0.4
-- Budget shipped by the App (PLAN.md N3a/b) is a total job timeout; Lua only
-- waits a FRACTION of it — the App must see a partial result with per-photo
-- errors before it gives up, or a bare Python-side timeout carries zero
-- diagnostic. Mirrors app.server.budget.LUA_BUDGET_FRACTION.
local LUA_BUDGET_FRACTION = 0.8
-- Delay given to Lr to regenerate the preview after an applyDevelopSettings, before
-- requesting the probed thumbnail (cf. Thumbnails.fetchProbe).
local SETTLE = 0.6

-- Fetch generation: suffixes output files (a unique name per call) and arms
-- the anti-late-callback guard (Fable 5 review L-01/L-02). Without it, a
-- callback arriving after timeout could overwrite the next job's fresh file
-- (the App would measure stale pixels) or mutate a `results` already returned.
--
-- Persisted in _G.ABELR_THUMB_GEN, not a plain module-local (PLAN.md N4b):
-- a plain local resets to 0 on every plugin reload, so generation numbers get
-- reused across a reload — on-disk evidence showed 22 files named `*_3.jpg`
-- written across two different days. Not a live correctness bug today (a
-- path is only returned from a successful callback), but the late-callback
-- guard (`gen ~= fetchGen` below) shouldn't be the only protection left
-- standing against a stale generation number colliding with a fresh one.
local function nextGen()
    _G.ABELR_THUMB_GEN = (_G.ABELR_THUMB_GEN or 0) + 1
    return _G.ABELR_THUMB_GEN
end

-- Files written per generation, purged two generations later (by then the App
-- has consumed the JPEGs — it reads them as soon as the job returns). Reset
-- on reload like any module-local — the startup sweep below (N4b) is the
-- backstop for files a purge never got to run for (crash, reload mid-fetch).
local staleFiles = {}

local function purgeStaleFiles(currentGen)
    for g, paths in pairs(staleFiles) do
        if g <= currentGen - 2 then
            for _, p in ipairs(paths) do
                LrFileUtils.delete(p)
            end
            staleFiles[g] = nil
        end
    end
end

-- Sweeps tmp_thumbs of files older than `maxAgeHours` (PLAN.md N4b) — the
-- backstop for the leak the in-memory staleFiles/gen tracking above cannot
-- cover across a plugin reload or a crash. LrDate.currentTime() and
-- LrFileUtils.fileAttributes().fileModificationDate share the same epoch
-- (Cocoa time), unlike os.time() (Unix epoch) — comparing across the two
-- would misjudge every file's age by decades.
local function sweepOldFiles(maxAgeHours)
    local dir = Utils.thumbsDir()
    local cutoff = maxAgeHours * 3600
    local now = LrDate.currentTime()
    local swept = 0
    for path in LrFileUtils.files(dir) do
        local attrs = LrFileUtils.fileAttributes(path)
        local mtime = attrs and attrs.fileModificationDate
        if mtime and (now - mtime) > cutoff then
            LrFileUtils.delete(path)
            swept = swept + 1
        end
    end
    if swept > 0 then
        Utils.logf('Thumbnails: swept %d stale file(s) from tmp_thumbs (>%gh old)', swept, maxAgeHours)
    end
end

--[[
    Thumbnails.fetch(photos, width, height, budget)

    `photos`: table of LrPhoto (e.g. catalog:getTargetPhotos()).
    `width`, `height`: max thumbnail size (default 512×512).
    `budget`: total job timeout in seconds, shipped by the App
    (PLAN.md N3, `payload.timeout_s`) — Lua waits `budget * LUA_BUDGET_FRACTION`.
    When absent (older App, MCP tools), falls back to a resolution-scaled
    default (N3c) — `n * THUMB_SECONDS_PER_PHOTO`, scaled by pixel count
    against the 512×512 baseline it was originally measured at.

    Returns an array of tables:
        { photo_id, thumbnail_path, error }
    thumbnail_path = absolute path of the written JPEG, or nil on error.
]]
function Thumbnails.fetch(photos, width, height, budget)
    width  = width  or 512
    height = height or 512

    local dir     = Utils.thumbsDir()
    local pending = #photos
    local results = {}
    local timeout
    if budget then
        timeout = math.max(THUMB_TIMEOUT, budget * LUA_BUDGET_FRACTION)
    else
        timeout = math.max(THUMB_TIMEOUT,
            #photos * THUMB_SECONDS_PER_PHOTO * (width * height) / (512 * 512))
    end

    local gen  = nextGen()
    local done = false      -- true after the wait: late callbacks stop writing anything
    purgeStaleFiles(gen)

    -- Retention of request objects (L-01): the return value of
    -- requestJpegThumbnail must stay referenced for the whole wait, otherwise
    -- the GC could collect it and the callback never fires (phantom timeouts).
    local requests = {}

    for i, photo in ipairs(photos) do
        local photoId = photo:getRawMetadata('uuid')
        -- Unique name per call (L-02): a late callback from job N writes into
        -- job N's file, never into job N+1's.
        local outPath = LrPathUtils.child(dir, string.format('%s_%d.jpg', photoId, gen))
        results[i]    = { photo_id = photoId, thumbnail_path = nil, error = nil }

        -- requestJpegThumbnail is async: callback fires when the thumbnail is ready.
        requests[i] = photo:requestJpegThumbnail(width, height, function(jpeg, err)
            if done or gen ~= (_G.ABELR_THUMB_GEN or 0) then
                Utils.logf('Thumbnail: late callback ignored (gen %d) for %s', gen, photoId)
                return
            end
            if jpeg and #jpeg > 0 then
                local f = io.open(outPath, 'wb')
                if f then
                    f:write(jpeg)
                    f:close()
                    results[i].thumbnail_path = outPath
                    Utils.logf('Thumbnail written: %s (%d bytes)', outPath, #jpeg)
                else
                    results[i].error = 'io.open failed: ' .. outPath
                    Utils.logf('Thumbnail: io.open failed -> %s', outPath)
                end
            else
                results[i].error = tostring(err or 'no JPEG returned')
                Utils.logf('Thumbnail missing for %s: %s', photoId, results[i].error)
            end
            pending = pending - 1
        end)
    end

    -- Cooperative wait: LrTasks.sleep yields to Lr so it can process the callbacks.
    -- The heartbeat is refreshed during the wait (L-05): a long batch must not
    -- make the App think the bridge is dead (5s threshold < duration of a big fetch).
    local elapsed = 0
    while pending > 0 and elapsed < timeout do
        _G.ABELR_BRIDGE_HEARTBEAT = os.time()
        LrTasks.sleep(0.1)
        elapsed = elapsed + 0.1
    end
    done = true

    if pending > 0 then
        Utils.logf('Thumbnails.fetch: timeout (%.1fs), %d still pending', timeout, pending)
        -- Marks still-pending entries as errors.
        for i = 1, #results do
            if results[i].thumbnail_path == nil and results[i].error == nil then
                results[i].error = 'timeout'
            end
        end
    end

    -- Remembers written files for deferred purge (gen + 2).
    local written = {}
    for i = 1, #results do
        if results[i].thumbnail_path then written[#written + 1] = results[i].thumbnail_path end
    end
    staleFiles[gen] = written

    -- `requests` intentionally kept alive up to this point (retention L-01).
    requests = nil

    return results
end

--[[
    Thumbnails.fetchByIds(photoIds, width, height, budget)

    Resolves `photoIds` (uuids) against the current selection, with a
    catalog:findPhotoByUuid fallback (same pattern as Adjustments.apply /
    Thumbnails.fetchProbe), then fetches ONLY those photos.

    Needed because get_thumbnails is submitted in chunks
    (fresh_render_worker.fetch_thumbnails_chunked): fetching the whole
    current selection on every chunk made Thumbnails.fetch's internal
    timeout scale with the FULL selection size instead of the chunk size,
    so it always lost the race against the App's per-chunk timeout on any
    non-trivial selection (e.g. 693 photos -> 277s internal wait vs a 24s
    per-chunk budget on the App side).

    `budget`: forwarded to Thumbnails.fetch (PLAN.md N3, `payload.timeout_s`).

    Unresolved uuids get their own error row (never silently dropped),
    same convention as fetchProbe.
]]
function Thumbnails.fetchByIds(photoIds, width, height, budget)
    local catalog = LrApplication.activeCatalog()
    local byUuid = {}
    for _, photo in ipairs(catalog:getTargetPhotos()) do
        byUuid[photo:getRawMetadata('uuid')] = photo
    end

    local photos, unresolved = {}, {}
    for _, id in ipairs(photoIds) do
        local photo = byUuid[id] or catalog:findPhotoByUuid(id)
        if photo then
            photos[#photos + 1] = photo
        else
            unresolved[#unresolved + 1] = id
        end
    end

    local results = Thumbnails.fetch(photos, width, height, budget)
    for _, id in ipairs(unresolved) do
        results[#results + 1] = { photo_id = id, thumbnail_path = nil, error = 'uuid not found' }
    end
    return results
end

--[[
    Thumbnails.fetchProbe(adjustments, width, height, settle, budget)

    PROBED render: applies temporary settings, renders the thumbnail of the
    resulting state, then RESTORES the original develop state. Used to calibrate
    the render/slider response (∂render/∂slider) on the App side (core.response)
    and for the neutral anchor render (NeutralPreview: WB As Shot + Exp 0 + HSL 0).

    `adjustments`: list of { photo_id = uuid, develop = { PascalCase = value } }.
    `settle`      : seconds given to Lr to regenerate the preview after the apply
                    (default SETTLE) — the App can increase it if the render is stale.
    `budget`      : total job timeout (PLAN.md N3, `payload.timeout_s`) — the time
                    already spent on the apply transaction + settle is subtracted
                    before delegating the remainder to Thumbnails.fetch, so the
                    render wait doesn't also eat the App's per-photo margin.
    Returns the same format as Thumbnails.fetch, enriched with `asshot_temp` /
    `asshot_tint`: numeric Temperature/Tint read back AFTER the apply — if the probe
    contains WhiteBalance='As Shot', this is the only chance to observe the As Shot's
    numeric value (basis for an absolute WB correction on the App side).

    ⚠️ BLOCKING ASSUMPTION TO VERIFY FOR REAL: requestJpegThumbnail must reflect
    the settings we just applied, not a stale cached preview. If Lr returns the old
    render, this path is unusable and we'd have to fall back to an export
    (LrExportSession). The settle delay gives Lr time to regenerate the preview before
    the request.

    Mutates the develop history (apply then restore) -> reserved for occasional
    calibration, not for bulk per-photo processing.
]]
function Thumbnails.fetchProbe(adjustments, width, height, settle, budget)
    width  = width  or 512
    height = height or 512
    settle = settle or SETTLE
    local probeStart = os.time()
    local catalog = LrApplication.activeCatalog()

    -- uuid -> photo index over the current selection, with findPhotoByUuid fallback:
    -- the probe must not depend on the selection at the moment the job arrives.
    local byUuid = {}
    for _, photo in ipairs(catalog:getTargetPhotos()) do
        byUuid[photo:getRawMetadata('uuid')] = photo
    end

    -- Captures the original state + lists the valid targets. Unresolvable
    -- photo_id's are NOT silently dropped (Fable 5-style review N2b): they
    -- get their own result row further down, mirroring Adjustments.lua's
    -- "uuid not found" handling for apply_adjustments.
    local targets, original, unresolved = {}, {}, {}
    for _, adj in ipairs(adjustments) do
        local photo = byUuid[adj.photo_id]
        if photo == nil then
            photo = catalog:findPhotoByUuid(adj.photo_id)
        end
        if photo and adj.develop then
            original[adj.photo_id] = photo:getDevelopSettings()  -- full snapshot
            targets[#targets + 1]  = { photo = photo, id = adj.photo_id, develop = adj.develop }
        else
            unresolved[#unresolved + 1] = adj.photo_id
        end
    end

    -- 1. Applies the probed settings (transaction). A failed apply leaves the
    -- photo in its ORIGINAL state, but the thumbnail rendered below is then
    -- indistinguishable from a real "neutral" anchor unless the failure is
    -- surfaced — poisons embedded mode exactly like a stale probe, silently.
    local applyErrors = {}
    catalog:withWriteAccessDo('ABELr: probe (apply)', function()
        for _, t in ipairs(targets) do
            local ok, err = LrTasks.pcall(function() t.photo:applyDevelopSettings(t.develop) end)
            if not ok then
                applyErrors[t.id] = tostring(err or 'apply failed')
                Utils.logf('fetchProbe: APPLY FAILED for %s: %s', t.id, tostring(err))
            end
            -- N4a: a slow probe (many photos, or a slow apply) must not let the
            -- heartbeat go stale — a stale heartbeat makes /bridge report
            -- disconnected and blocks the NEXT user action, not just this one.
            _G.ABELR_BRIDGE_HEARTBEAT = os.time()
        end
    end)

    -- Reads back the post-apply numeric values (As Shot Temperature/Tint).
    local asshotById = {}
    for _, t in ipairs(targets) do
        local ok, s = LrTasks.pcall(function() return t.photo:getDevelopSettings() end)
        if ok and s then
            asshotById[t.id] = { temp = s.Temperature, tint = s.Tint }
        end
        _G.ABELR_BRIDGE_HEARTBEAT = os.time()
    end

    -- Lets Lr regenerate the preview before requesting the thumbnails.
    -- Sliced into <=1s steps (N4a) so the heartbeat stays fresh through a
    -- long settle, mirroring Thumbnails.fetch's own wait loop.
    local settleLeft = settle
    while settleLeft > 0 do
        local step = math.min(1, settleLeft)
        LrTasks.sleep(step)
        settleLeft = settleLeft - step
        _G.ABELR_BRIDGE_HEARTBEAT = os.time()
    end

    -- 2. Renders the thumbnails of the probed state. Remaining budget = the
    -- job's total timeout minus what the apply transaction + settle already
    -- spent (both counted against the same budget the App is waiting on).
    local photos = {}
    for _, t in ipairs(targets) do photos[#photos + 1] = t.photo end
    local remainingBudget = budget and (budget - (os.time() - probeStart)) or nil
    local results = Thumbnails.fetch(photos, width, height, remainingBudget)

    -- Unresolvable photo_id's get their own result row (never silently dropped).
    for _, id in ipairs(unresolved) do
        results[#results + 1] = { photo_id = id, thumbnail_path = nil, error = 'uuid not found' }
    end

    -- 3. Restores the original state (transaction). A restore failure leaves the
    -- photo in a NEUTRAL state (WB As Shot / Exp 0 / HSL 0): it must surface in
    -- the job result, never be swallowed silently (Fable 5 review L-03).
    local restoreErrors = {}
    catalog:withWriteAccessDo('ABELr: probe (restore)', function()
        for _, t in ipairs(targets) do
            local orig = original[t.id]
            if orig then
                local ok, err = LrTasks.pcall(function() t.photo:applyDevelopSettings(orig) end)
                if not ok then
                    restoreErrors[t.id] = tostring(err or 'restore failed')
                    Utils.logf('fetchProbe: RESTORE FAILED for %s -- photo left in neutral state: %s',
                        t.id, tostring(err))
                end
            end
            _G.ABELR_BRIDGE_HEARTBEAT = os.time()
        end
    end)

    -- Enriches the results with the read-back As Shot values + restore errors.
    for i = 1, #results do
        local asshot = asshotById[results[i].photo_id]
        if asshot then
            results[i].asshot_temp = asshot.temp
            results[i].asshot_tint = asshot.tint
        end
        local restoreErr = restoreErrors[results[i].photo_id]
        if restoreErr then
            results[i].restore_error = restoreErr
            -- The restore failure takes priority: the rendered thumbnail is that of
            -- a state the photo will not leave -- a strong signal for the App.
            results[i].error = results[i].error or ('restore failed: ' .. restoreErr)
        end
        local applyErr = applyErrors[results[i].photo_id]
        if applyErr then
            -- The render below reflects the CURRENT (unchanged) style, not the
            -- probed one -- for a neutral-anchor probe this looks like a valid
            -- anchor while silently being stale. Must not be swallowed.
            results[i].error = results[i].error or ('apply failed: ' .. applyErr)
        end
    end

    return results
end

-- Startup sweep (PLAN.md N4b): runs once when the module is (re)loaded —
-- catches files a mid-session purge never got to (crash, reload mid-fetch).
sweepOldFiles(24)

return Thumbnails
