--[[
    Thumbnails.lua — fetches JPEG thumbnails via requestJpegThumbnail.

    Writes each thumbnail to {projectRoot}/tmp_thumbs/{photo_id}_{gen}.jpg so the
    Python App can read them directly (same machine, no base64 encoding).
    {gen} = fetch generation (unique name per call, cf. Fable 5 review L-02).

    Files are purged **by age** (RETENTION_HOURS), never by "two fetches later":
    the App does NOT consume every JPEG at the end of the job that produced it.
    `fetch_thumbnails_chunked` (fresh render) collects paths across N chunks and
    only decodes them once the whole selection is fetched — a gen-based purge
    deleted chunk 1's files while chunk 3 was still being fetched, so the
    measurement silently fell back to the passive Previews.lrdata tier (that is
    the source of the `484x322` undersized renders in abelr_app.log).

    requestJpegThumbnail is async: we wait for callbacks via LrTasks.sleep.
    Lr serves the best preview tier it has *at that moment* and may call back
    again with a better one — so a callback below the requested size is kept as
    "best so far" and does NOT end the wait for that photo (UPGRADE_GRACE).
    Timeout of THUMB_TIMEOUT seconds if Lr doesn't generate the thumbnail (missing preview).
]]

local LrApplication   = import 'LrApplication'
local LrExportSession = import 'LrExportSession'
local LrFileUtils     = import 'LrFileUtils'
local LrPathUtils     = import 'LrPathUtils'
local LrTasks         = import 'LrTasks'
local Utils           = require 'Utils'

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
-- A callback whose JPEG long edge is below `requested * SIZE_TOLERANCE` is a
-- lower preview tier, not the render we asked for. Tolerance absorbs Lr's
-- rounding on the fit-box (2048 box on a 3:2 frame -> 2048x1365).
local SIZE_TOLERANCE = 0.98
-- Once every photo has delivered *something*, keep waiting this long for Lr to
-- call back with a better tier before giving up on the full size. Bounded and
-- short: it only costs time when the batch is undersized.
local UPGRADE_GRACE = 4.0
-- tmp_thumbs retention: files are purged by age at the start of each fetch.
-- Must outlive a full Analyze pass (fetch every chunk, THEN measure) — the
-- App reads a JPEG long after the job that wrote it returned.
local RETENTION_HOURS = 6
-- Quality of the LrExportSession fallback render (Thumbnails.exportFallback).
-- Favors measurement accuracy over file size — these get analyzed, not viewed.
local EXPORT_JPEG_QUALITY = 0.92

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

-- Sweeps tmp_thumbs of files older than `maxAgeHours` (PLAN.md N4b) — the only
-- purge mechanism: called at the start of every fetch (RETENTION_HOURS) and at
-- module load (24h backstop for a crash/reload). Age-based on purpose, see the
-- header — the previous "purge generation-2" deleted files the App had not read
-- yet whenever a caller batched several fetches before decoding.
--
-- "now" is NOT taken from a clock API: it is read back from a marker file this
-- function just wrote, so both sides of the subtraction come from
-- fileAttributes().fileModificationDate and the epoch cancels out. The previous
-- version compared LrDate.currentTime() (Cocoa epoch, 2001) against a
-- fileModificationDate assumed to use the same epoch — an assumption never
-- verified live, and the observed behaviour contradicts it: 831 files, all
-- under 11h old, were wiped by a module-load sweep whose cutoff was 24h. A
-- cross-epoch subtraction is off by ~31 years, i.e. every file always reads as
-- ancient. Cheap to make unfalsifiable, so it is.
local SWEEP_MARKER = '.sweep_now'

local function sweepOldFiles(maxAgeHours)
    local dir = Utils.thumbsDir()
    local cutoff = maxAgeHours * 3600

    -- Marker written now -> its own mtime IS "now" in fileAttributes units.
    local markerPath = LrPathUtils.child(dir, SWEEP_MARKER)
    local mf = io.open(markerPath, 'wb')
    if mf then
        mf:write('.')
        mf:close()
    end
    local markerAttrs = LrFileUtils.fileAttributes(markerPath)
    local now = markerAttrs and markerAttrs.fileModificationDate
    if not now then
        -- No reference clock -> deleting would be guesswork. Skip: a leaked
        -- JPEG costs disk, a wrongly-deleted one costs a whole analysis pass.
        Utils.logf('Thumbnails: sweep skipped (no mtime on the marker file)')
        return
    end

    local swept = 0
    for path in LrFileUtils.files(dir) do
        if path ~= markerPath then
            local attrs = LrFileUtils.fileAttributes(path)
            local mtime = attrs and attrs.fileModificationDate
            local age = mtime and (now - mtime)
            -- age < 0 (clock skew) or > 10 years (unit mismatch) => do not trust it.
            if age and age > cutoff and age < 10 * 365 * 24 * 3600 then
                LrFileUtils.delete(path)
                swept = swept + 1
            end
        end
    end
    if swept > 0 then
        Utils.logf('Thumbnails: swept %d stale file(s) from tmp_thumbs (>%gh old)', swept, maxAgeHours)
    end
end

-- SOFn markers carrying the frame dimensions (baseline/progressive/lossless);
-- DHT/DAC/RSTn/SOS are deliberately absent.
local SOF_MARKERS = {
    [0xC0] = true, [0xC1] = true, [0xC2] = true, [0xC3] = true,
    [0xC5] = true, [0xC6] = true, [0xC7] = true,
    [0xC9] = true, [0xCA] = true, [0xCB] = true,
    [0xCD] = true, [0xCE] = true, [0xCF] = true,
}

--[[
    jpegSize(data) -> width, height (nil, nil if unparseable)

    Walks the JPEG segment chain to the first SOFn. Needed because
    requestJpegThumbnail does NOT honour the requested size: it serves whichever
    preview tier Lr has cached (live catalog: 1936x1290 or 484x322 against a
    2048 request — see cache.py ANALYSIS_VERSION "v8-grid-enforced"). Without
    reading the size here, Lua cannot tell "the render we asked for" from "a
    lower tier", so it always accepts the first callback and the App discovers
    the sub-grid render only after decoding it.

    SOFn layout, from the 0xFF byte: FF | Cx | len(2) | precision | H(2) | W(2).
]]
local function jpegSize(data)
    local n = #data
    local pos = 3   -- skips SOI (FF D8)
    while pos + 3 <= n do
        if data:byte(pos) ~= 0xFF then
            pos = pos + 1              -- resync on the next marker prefix
        else
            local marker = data:byte(pos + 1)
            if marker == 0xFF then
                pos = pos + 1          -- fill byte
            elseif marker >= 0xD0 and marker <= 0xD9 then
                pos = pos + 2          -- standalone marker (RSTn/SOI/EOI), no length
            else
                local len = data:byte(pos + 2) * 256 + data:byte(pos + 3)
                if SOF_MARKERS[marker] then
                    if pos + 8 > n then return nil, nil end
                    return data:byte(pos + 7) * 256 + data:byte(pos + 8),
                           data:byte(pos + 5) * 256 + data:byte(pos + 6)
                end
                if len < 2 then return nil, nil end
                pos = pos + 2 + len
            end
        end
    end
    return nil, nil
end

-- Whole-file read for `jpegSize` on an LrExportSession output (we only have a
-- path from Lr, not the in-memory bytes requestJpegThumbnail's callback gives us).
local function readFileBytes(path)
    local f = io.open(path, 'rb')
    if not f then return nil end
    local data = f:read('*a')
    f:close()
    return data
end

--[[
    exportViaSession(photo, longEdge) -> path, err

    Last-resort render for a photo `requestJpegThumbnail` could not deliver at
    the required size — either a hard failure ("error loading thumb", PLAN.md
    N5: a photo open large in Develop failed 5/5 independent
    requestJpegThumbnail calls, all settle/size combos, while Lr rendered it
    fine in its own Develop module — theorized contention with the interactive
    renderer) or a persistent sub-grid tier (Standard Preview Size capped
    below the measurement grid). `LrExportSession` runs Lr's own export
    pipeline, a completely different code path from the async preview cache,
    so it is unaffected by whatever blocks `requestJpegThumbnail`.

    Renders using the photo's CURRENT develop settings — the same contract
    `requestJpegThumbnail` has, and what a `fetchProbe` caller needs (it calls
    into `Thumbnails.fetch` while the probed settings are still applied, before
    its own restore step).

    Blocking (`doExportOnCurrentTask`): acceptable only because this runs for
    the handful of leftover stuck photos after `Thumbnails.fetch`'s normal
    wait, never for a whole batch. Single attempt, no retry — mirrors the
    hard-failure retry that was removed from `neutral_preview_worker.py`
    (0/1 recovered on every live attempt): a photo that fails here fails once,
    loudly, rather than paying a second full export for nothing.

    ⚠️ UNVERIFIED IN LIVE LR (same convention as the Collections.lua/Metadata.lua
    headers): the `LR_*` export-settings keys below are the widely-used
    undocumented keys behind Lr's Export dialog — `documentation/Lr_SDK_API`
    documents `LrExportSession`/`LrExportRendition` themselves but not this
    settings table (`LrExportSettings.html` only covers video presets). Needs
    one live reload + a real stuck photo to confirm before relying on it.
]]
local function exportViaSession(photo, longEdge)
    local ok, path, err = LrTasks.pcall(function()
        local session = LrExportSession {
            photosToExport = { photo },
            exportSettings = {
                LR_format                     = 'JPEG',
                LR_jpeg_quality               = EXPORT_JPEG_QUALITY,
                LR_size_doConstrain           = true,
                LR_size_resizeType            = 'longEdge',
                LR_size_maxWidth              = longEdge,
                LR_size_maxHeight             = longEdge,
                LR_size_units                 = 'pixels',
                LR_export_destinationType     = 'specificFolder',
                LR_export_destinationPathPrefix = Utils.thumbsDir(),
                LR_export_useSubfolder        = false,
                LR_collisionHandling          = 'overwrite',
                -- Must NOT come back into the catalog as a new photo.
                LR_reimportExportedPhoto      = false,
                -- Closer to what requestJpegThumbnail serves (no print/screen
                -- output sharpening pass) — keeps the fallback render
                -- comparable to the normal path for the same photo.
                LR_outputSharpeningOn         = false,
            },
        }
        session:doExportOnCurrentTask()
        for _, rendition in session:renditions() do
            local success, message = rendition:waitForRender()
            -- destinationPath is read back from the rendition, never assumed:
            -- Lr — not us — resolves the final filename (collision suffixing).
            if success and rendition.destinationPath and LrFileUtils.exists(rendition.destinationPath) then
                return rendition.destinationPath, nil
            end
            return nil, message or 'export rendition failed'
        end
        return nil, 'export session produced no rendition'
    end)
    if not ok then
        return nil, tostring(path or 'export session error')
    end
    return path, err
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
        { photo_id, thumbnail_path, error, width, height, is_export }
    thumbnail_path = absolute path of the written JPEG, or nil on error.
    width/height   = actual pixel size of that JPEG (nil if unparseable) — the
                     App rejects sub-grid renders, so it must be able to say
                     WHICH size it got, not just "undersized" after decoding.
    is_export      = true if requestJpegThumbnail never delivered a full-size
                     render and this JPEG came from the LrExportSession
                     fallback instead (see exportViaSession) — the App deletes
                     this file itself right after decoding it (it is a
                     one-shot render, not a cached tier another chunk might
                     still want, so there is no reason to wait for the
                     age-based sweep).

    Size handling: a photo is "done" only once Lr delivers a JPEG whose long
    edge reaches `max(width, height) * SIZE_TOLERANCE`. A smaller callback is
    written to disk and kept as best-so-far, but the wait continues — Lr calls
    back again with a better tier when it finishes rendering one. When every
    photo has delivered something and only undersized ones remain, the wait
    stops UPGRADE_GRACE seconds later instead of burning the whole budget —
    and whatever is still missing then goes through the export fallback.
]]
function Thumbnails.fetch(photos, width, height, budget)
    width  = width  or 512
    height = height or 512

    local dir     = Utils.thumbsDir()
    local wanted  = #photos       -- photos still without a full-size render
    local settled = 0             -- photos that delivered something (any tier) or failed
    local results = {}
    local timeout
    if budget then
        timeout = math.max(THUMB_TIMEOUT, budget * LUA_BUDGET_FRACTION)
    else
        timeout = math.max(THUMB_TIMEOUT,
            #photos * THUMB_SECONDS_PER_PHOTO * (width * height) / (512 * 512))
    end
    local minLongEdge = math.max(width, height) * SIZE_TOLERANCE

    local gen  = nextGen()
    local done = false      -- true after the wait: late callbacks stop writing anything
    sweepOldFiles(RETENTION_HOURS)

    -- Retention of request objects (L-01): the return value of
    -- requestJpegThumbnail must stay referenced for the whole wait, otherwise
    -- the GC could collect it and the callback never fires (phantom timeouts).
    local requests = {}
    -- Per-photo bookkeeping. Every flag is one-shot: Lr can invoke the callback
    -- SEVERAL times for one photo (progressive tiers), and the previous bare
    -- `pending = pending - 1` per callback drove the counter negative, ending
    -- the wait before the other photos had answered at all.
    --   area   = pixel area of the best tier written so far
    --   full   = that tier reaches the requested size (nothing more to wait for)
    --   closed = the photo answered at least once (any tier, or a hard error)
    local state = {}

    -- Ends the wait for one photo: `closed` feeds the upgrade grace, `stopped`
    -- decrements the outstanding count exactly once.
    local function stopWaiting(st)
        if not st.closed then
            st.closed = true
            settled = settled + 1
        end
        if not st.stopped then
            st.stopped = true
            wanted = wanted - 1
        end
    end

    for i, photo in ipairs(photos) do
        local photoId = photo:getRawMetadata('uuid')
        -- Unique name per call (L-02): a late callback from job N writes into
        -- job N's file, never into job N+1's.
        local outPath = LrPathUtils.child(dir, string.format('%s_%d.jpg', photoId, gen))
        results[i]    = { photo_id = photoId, thumbnail_path = nil, error = nil }
        state[i]      = { area = 0, full = false, closed = false, stopped = false }

        -- requestJpegThumbnail is async: callback fires when the thumbnail is
        -- ready, and again if Lr later produces a better tier.
        requests[i] = photo:requestJpegThumbnail(width, height, function(jpeg, err)
            if done or gen ~= (_G.ABELR_THUMB_GEN or 0) then
                Utils.logf('Thumbnail: late callback ignored (gen %d) for %s', gen, photoId)
                return
            end
            local st = state[i]
            if jpeg and #jpeg > 0 then
                local w, h = jpegSize(jpeg)
                local area = (w and h) and (w * h) or #jpeg
                if area <= st.area then
                    -- Same tier or worse (Lr re-delivering what we already have).
                    return
                end
                local f = io.open(outPath, 'wb')
                if f then
                    f:write(jpeg)
                    f:close()
                    st.area = area
                    results[i].thumbnail_path = outPath
                    results[i].width  = w
                    results[i].height = h
                    results[i].error  = nil
                    Utils.logf('Thumbnail written: %s (%d bytes, %sx%s)',
                        outPath, #jpeg, tostring(w), tostring(h))
                    if not st.closed then
                        st.closed = true
                        settled = settled + 1
                    end
                    if w and h and math.max(w, h) >= minLongEdge then
                        st.full = true
                        stopWaiting(st)
                    end
                else
                    results[i].error = 'io.open failed: ' .. outPath
                    Utils.logf('Thumbnail: io.open failed -> %s', outPath)
                    stopWaiting(st)
                end
            else
                -- Hard failure from Lr ("error loading thumb"…): terminal for
                -- this photo, unless a previous callback already delivered one.
                if results[i].thumbnail_path == nil then
                    results[i].error = tostring(err or 'no JPEG returned')
                    Utils.logf('Thumbnail missing for %s: %s', photoId, results[i].error)
                end
                stopWaiting(st)
            end
        end)
    end

    -- Cooperative wait: LrTasks.sleep yields to Lr so it can process the callbacks.
    -- The heartbeat is refreshed during the wait (L-05): a long batch must not
    -- make the App think the bridge is dead (5s threshold < duration of a big fetch).
    -- Two exits: every photo full-size, or every photo answered *something* and
    -- the short upgrade grace expired (Lr will not produce a better tier).
    local elapsed = 0
    local graceLeft = nil
    while wanted > 0 and elapsed < timeout do
        _G.ABELR_BRIDGE_HEARTBEAT = os.time()
        LrTasks.sleep(0.1)
        elapsed = elapsed + 0.1
        if settled >= #photos then
            graceLeft = (graceLeft or UPGRADE_GRACE) - 0.1
            if graceLeft <= 0 then break end
        else
            graceLeft = nil   -- a new arrival re-opens the upgrade window
        end
    end
    done = true

    -- Export fallback: last resort for whatever requestJpegThumbnail could not
    -- deliver at full size (hard failure or a persistent sub-grid tier) —
    -- see exportViaSession's header. Single attempt per photo, only for the
    -- leftover stuck ones (never the whole batch).
    local exportAttempts, exportOk = 0, 0
    for i, photo in ipairs(photos) do
        if not state[i].full then
            exportAttempts = exportAttempts + 1
            _G.ABELR_BRIDGE_HEARTBEAT = os.time()
            local expPath, expErr = exportViaSession(photo, math.max(width, height))
            _G.ABELR_BRIDGE_HEARTBEAT = os.time()
            if expPath then
                local data = readFileBytes(expPath)
                local w, h = data and jpegSize(data) or nil, nil
                results[i].thumbnail_path = expPath
                results[i].width  = w
                results[i].height = h
                results[i].error  = nil
                results[i].is_export = true
                state[i].full = true
                exportOk = exportOk + 1
                Utils.logf('Thumbnails.fetch: export fallback succeeded for %s (%sx%s)',
                    results[i].photo_id, tostring(w), tostring(h))
            else
                Utils.logf('Thumbnails.fetch: export fallback failed for %s: %s',
                    results[i].photo_id, tostring(expErr))
                if results[i].error == nil then
                    results[i].error = 'export fallback failed: ' .. tostring(expErr)
                end
            end
        end
    end
    if exportAttempts > 0 then
        Utils.logf('Thumbnails.fetch: export fallback used for %d/%d photo(s), %d succeeded',
            exportAttempts, #photos, exportOk)
    end

    local undersized, stillMissing = 0, 0
    for i = 1, #results do
        local r = results[i]
        if r.thumbnail_path == nil and r.error == nil then
            r.error = 'timeout'
        elseif r.thumbnail_path and not state[i].full then
            -- Kept, not discarded: the App decides (it rejects sub-grid renders
            -- itself). Logged here because this is where the actual tier is known.
            undersized = undersized + 1
        end
        if not state[i].full then
            stillMissing = stillMissing + 1
        end
    end
    if stillMissing > 0 then
        Utils.logf('Thumbnails.fetch: stopped after %.1fs (timeout %.1fs), %d of %d '
            .. 'photo(s) without a full-size render after export fallback (%d undersized '
            .. 'tier(s), requested %dx%d)',
            elapsed, timeout, stillMissing, #photos, undersized, width, height)
    end

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

    Verified live 2026-07-26 (PLAN.md N5): 32 real photos, 29 with |Exposure2012| >=
    0.3 already applied. 11 had a prior current-state PreviewJPEG to diff against —
    10/11 anchors landed 5-21 L* points away from that baseline (matches the removed
    exposure); the JPEG bytes differ probe-to-probe (sha256, not a served cache) even
    for the 1 case whose L* delta stayed small (a mask-specific tone-resistance false
    positive in `_anchor_suspect`, not a stale render). requestJpegThumbnail reflects
    the just-applied settings when it returns one at all. The settle delay gives Lr
    time to regenerate the preview before the request.

    ⚠️ STILL OPEN (same day, live): requestJpegThumbnail can fail hard and repeatedly
    for a single photo — 5 independent calls, all settle/size combos, zero thumbnails —
    while Lr renders that same photo fine in its own Develop module. The failing photo
    was the one open large in Develop at the time; unconfirmed whether that's the
    trigger (contention with the interactive renderer) or coincidence. Not a corrupted
    RAW. The App-side retry that was added for this is GONE (it recovered 0/1 on every
    live attempt, cf. abelr_app.log 13:36-13:42): a photo failing this way now fails
    once, loudly, instead of paying a second full probe for nothing.

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
