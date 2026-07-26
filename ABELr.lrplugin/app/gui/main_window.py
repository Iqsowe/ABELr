"""Main PySide6 window — references (seeds) + per-axis auto correction.

Refactor flow (see CLAUDE.md / workflow diagram):

- **Analyze Catalog**: index metadata (EXIF + develop) for all photos.
- **Mark + analyze references**: flags the selection `is_seed` in DB AND
  measures it (sharp-area GPU RAW + camera JPEG + **fresh render** via the
  plugin) → the seed is immediately usable by the k-NN matching (no more
  silent "marked but unusable").
- **Remove from references**: unflags `is_seed` (no measurement).
- **[Exposure] [WB] [HSL]** checkboxes: axes to correct.
- **[Ref = embedded JPEG]** checkbox: forces the camera JPEG as target
  (otherwise: k-NN over the closest seeds by RAW analysis).
- **Preview**: measures (fresh render) + plans, **shows the deltas without
  applying**.
- **Apply**: applies the Preview plan if one exists for the current
  selection, otherwise measures + plans + applies in one pass.

Key correctness point: **any measurement of the current state starts from a
fresh render requested from the plugin** (`get_thumbnails` → JPEG written by
Lightroom), no longer from the passive `Previews.lrdata` file (potentially
stale). If the plugin provides no thumbnail (previews missing), it falls
back to the passive channel (degraded, non-blocking).

Caution — hypothesis not yet validated live (open item): `requestJpegThumbnail`
is assumed to reflect the current develop state and not a stale cache.
Otherwise → fall back to `LrExportSession` on the plugin side (see
`core.measure`).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import cache as cachemod
from ..core import render_metrics, response
from ..server.job_queue import job_queue
from ..server.models import JobResult, JobType
from . import status as statusmod
from .autocorrect_worker import AutoCorrectResult, AutoCorrectWorker
from .fresh_render_worker import FreshRenderWorker
from .log_bridge import QtLogHandler
from .neutral_preview_worker import NeutralPreviewWorker
from .job_worker import JobWorker

_AXIS_LABELS = {"expo": "Exposure", "wb": "WB", "hsl": "HSL", "calib": "Calibration"}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ABELr — auto correction")
        self.resize(760, 600)

        self._worker: JobWorker | None = None
        self._check_worker: JobWorker | None = None
        self._render_worker: FreshRenderWorker | None = None
        self._auto_worker: AutoCorrectWorker | None = None
        self._neutral_worker: NeutralPreviewWorker | None = None
        self._apply_worker: JobWorker | None = None
        self._seed_worker: JobWorker | None = None

        # U2 status panel: state that already exists in the DB/response cache
        # but was never surfaced anywhere. Updated as it becomes known, never
        # reset by an operation that didn't touch it.
        self._catalog_path: str | None = None
        self._last_camera: str | None = None
        self._last_profile: str | None = None

        # Minimal state machine: the current op, the selection and the fresh
        # render are kept between hops (selection → render → measurement).
        self._op: str | None = None                 # "ref"|"preview"|"apply"|"seed_remove"|"neutral"
        self._photos: list = []                     # PhotoResult of the current selection
        self._thumb_paths: dict[str, str] = {}       # uuid → fresh rendered JPEG (plugin)
        # Preview plan reusable by Apply (same selection).
        self._pending_adjustments: list | None = None
        self._pending_ids: frozenset[str] = frozenset()

        self.bridge_label = QLabel()
        self.status_label = QLabel("Ready. Select photos in Lightroom.")
        self.plan_summary_label = QLabel("")
        self.plan_summary_label.setStyleSheet("font-weight: bold;")

        # Progress bar for image analysis / measurement operations. Hidden at
        # rest; determinate when a worker provides (done, total), busy (animated)
        # otherwise.
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)

        # Diagnostics — a status-bar item, not a workflow step (U1).
        self.test_btn = QPushButton("Test bridge")

        # 1. Catalog.
        self.analyze_catalog_btn = QPushButton("Analyze Catalog")

        # 2. References (seeds).
        self.mark_refs_btn = QPushButton("Mark + analyze references")
        self.unmark_refs_btn = QPushButton("Remove from references")

        # 3. Correction — embedded-mode-only prep step, relabeled to say what
        # it does (U1); enabled only when cb_embedded is checked.
        self.calibrate_neutral_btn = QPushButton("Prepare neutral anchors")
        self.calibrate_neutral_btn.setToolTip(
            "Embedded mode only: pre-renders the neutral anchor (WB As Shot, "
            "Exposure 0, HSL 0) for the current selection so the next Preview/"
            "Apply doesn't pay for it inline."
        )

        # Axes + reference.
        self.cb_expo = QCheckBox("Exposure")
        self.cb_wb = QCheckBox("WB")
        self.cb_hsl = QCheckBox("HSL")
        self.cb_calib = QCheckBox("Calibration")
        for cb in (self.cb_expo, self.cb_wb, self.cb_hsl, self.cb_calib):
            cb.setChecked(True)
        self.cb_embedded = QCheckBox("Ref = embedded JPEG")
        self.cb_embedded.setToolTip(
            "Unchecked: target = k-NN over the seeds whose RAW analysis (sharp\n"
            "area) is closest (uses their already-edited preview as the style\n"
            "reference).\n"
            "Checked: target = camera JPEG, anchored on the neutral render (WB As\n"
            "Shot, Exposure 0, HSL 0) — corrects only the PER-PHOTO deviation after\n"
            "subtracting the profile bias; absolute values, idempotent.\n"
            "The 1st Preview after a style change recomputes the anchors in\n"
            "Lightroom (render_probe, ~4-7 s/photo (median ~5.7), then served\n"
            "from cache)."
        )

        # Correction.
        self.preview_btn = QPushButton("Preview")
        self.preview_btn.setToolTip("Measures + plans, shows the deltas WITHOUT applying.")
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setToolTip(
            "Applies the Preview plan if one exists, otherwise measures + plans + applies."
        )

        # U2: status panel (read-only grid, refreshed on the bridge timer and
        # after every operation) + Plan/Log tabs — split out of the single
        # `photo_list` dumping ground the plan lines, diagnostics/warnings and
        # the L2 log stream used to share.
        self.status_panel = QListWidget()
        self.status_panel.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.status_panel.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # read-only display
        self.status_panel.setMaximumHeight(140)

        self.photo_list = QListWidget()
        self.log_list = QListWidget()
        self.tabs = QTabWidget()
        self.tabs.addTab(self.photo_list, "Plan")
        self.tabs.addTab(self.log_list, "Log")

        self._log_handler = QtLogHandler(level=logging.WARNING)
        self._log_handler.record.connect(self.log_list.addItem)
        logging.getLogger("abelr").addHandler(self._log_handler)

        self.test_btn.clicked.connect(self._on_check)
        self.analyze_catalog_btn.clicked.connect(self._on_analyze_catalog)
        self.mark_refs_btn.clicked.connect(lambda: self._begin("ref"))
        self.unmark_refs_btn.clicked.connect(lambda: self._begin("seed_remove"))
        self.calibrate_neutral_btn.clicked.connect(lambda: self._begin("neutral"))
        self.preview_btn.clicked.connect(lambda: self._begin("preview"))
        self.apply_btn.clicked.connect(self._on_apply_click)
        self.cb_embedded.toggled.connect(self._refresh_neutral_btn_enabled)

        layout = QVBoxLayout()
        layout.addWidget(self.bridge_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)

        # U1: numbered groups reflecting the real usage order (previously the
        # order lived only in the module docstring, invisible to the user).
        group1 = QGroupBox("1. Catalog")
        g1_row = QHBoxLayout(group1)
        g1_row.addWidget(self.analyze_catalog_btn)
        g1_row.addStretch()
        layout.addWidget(group1)

        group2 = QGroupBox("2. References")
        g2_row = QHBoxLayout(group2)
        g2_row.addWidget(self.mark_refs_btn)
        g2_row.addWidget(self.unmark_refs_btn)
        g2_row.addStretch()
        layout.addWidget(group2)

        group3 = QGroupBox("3. Correction")
        g3 = QVBoxLayout(group3)
        axes_row = QHBoxLayout()
        axes_row.addWidget(QLabel("Axes:"))
        axes_row.addWidget(self.cb_expo)
        axes_row.addWidget(self.cb_wb)
        axes_row.addWidget(self.cb_hsl)
        axes_row.addWidget(self.cb_calib)
        axes_row.addSpacing(16)
        axes_row.addWidget(self.cb_embedded)
        axes_row.addStretch()
        g3.addLayout(axes_row)
        actions_row = QHBoxLayout()
        actions_row.addWidget(self.calibrate_neutral_btn)
        actions_row.addSpacing(16)
        actions_row.addWidget(self.preview_btn)
        actions_row.addWidget(self.apply_btn)
        actions_row.addStretch()
        g3.addLayout(actions_row)
        layout.addWidget(group3)

        layout.addWidget(QLabel("Status:"))
        layout.addWidget(self.status_panel)
        layout.addWidget(self.plan_summary_label)
        layout.addWidget(self.tabs)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # "Test bridge" is a diagnostic, not a workflow step (U1) — status bar.
        self.statusBar().addPermanentWidget(self.test_btn)

        self._refresh_neutral_btn_enabled()

        self._bridge_timer = QTimer(self)
        self._bridge_timer.timeout.connect(self._refresh_bridge)
        self._bridge_timer.timeout.connect(self._refresh_status)
        self._bridge_timer.start(1000)
        self._refresh_bridge()
        self._refresh_status()

    # ------------------------------------------------------------------ #
    def _refresh_bridge(self) -> None:
        if job_queue.bridge_connected():
            since = job_queue.seconds_since_poll() or 0.0
            self.bridge_label.setText(
                f"Plugin bridge: ● active (last poll {since:.1f}s ago)"
            )
        else:
            self.bridge_label.setText(
                "Plugin bridge: ○ inactive — in Lightroom: "
                "Library > Plug-in Extras > Start / connect the application"
            )

    def _refresh_status(self) -> None:
        """U2: rebuild the read-only status grid. Cheap (local DB reads only,
        no Lr job) — safe to call on the 1s timer and after every operation."""
        conn = None
        if self._catalog_path:
            try:
                conn = cachemod.open_cache(self._catalog_path)
            except Exception:
                conn = None
        model = (
            response.load(self._last_camera, self._last_profile)
            if self._last_camera or self._last_profile else None
        )
        try:
            snap = statusmod.build_status(
                conn, self._photos, model, bridge_connected=job_queue.bridge_connected(),
            )
        finally:
            if conn is not None:
                conn.close()
        self.status_panel.clear()
        for line in statusmod.format_status_lines(snap):
            self.status_panel.addItem(line)

    def _set_actions_enabled(self, enabled: bool) -> None:
        for btn in (self.mark_refs_btn, self.unmark_refs_btn, self.preview_btn, self.apply_btn):
            btn.setEnabled(enabled)
        # calibrate_neutral_btn has its own extra gate (embedded mode only) —
        # re-derive rather than blindly setEnabled(enabled).
        self._refresh_neutral_btn_enabled(busy=not enabled)

    def _refresh_neutral_btn_enabled(self, *_args, busy: bool = False) -> None:
        """U1: "Prepare neutral anchors" only makes sense in embedded mode.
        `*_args` absorbs the bool QCheckBox.toggled emits when used as a slot."""
        self.calibrate_neutral_btn.setEnabled(self.cb_embedded.isChecked() and not busy)

    # ------------------------------------------------------------------ #
    # Progress bar (image analysis / measurement)
    # ------------------------------------------------------------------ #
    def _progress_busy(self) -> None:
        """Shows the bar in indeterminate mode (animated) — step with no counter."""
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)

    def _on_progress_count(self, done: int, total: int) -> None:
        """Switches the bar to determinate `done/total` mode (emitted by GPU workers)."""
        if total <= 0:
            self._progress_busy()
            return
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)
        self.progress_bar.setVisible(True)

    def _progress_done(self) -> None:
        """Hides and resets the bar (operation finished or failed)."""
        self.progress_bar.reset()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setVisible(False)

    def _checked_axes(self) -> frozenset[str]:
        axes = set()
        if self.cb_expo.isChecked():
            axes.add("expo")
        if self.cb_wb.isChecked():
            axes.add("wb")
        if self.cb_hsl.isChecked():
            axes.add("hsl")
        if self.cb_calib.isChecked():
            axes.add("calib")
        return frozenset(axes)

    def _require_bridge(self) -> bool:
        if not job_queue.bridge_connected():
            self.status_label.setText("Bridge inactive — start the application from Lightroom.")
            return False
        return True

    # ------------------------------------------------------------------ #
    # Test bridge
    # ------------------------------------------------------------------ #
    def _on_check(self) -> None:
        if not self._require_bridge():
            return
        self.test_btn.setEnabled(False)
        self.status_label.setText("Checking plugin — waiting for the Lr plugin…")
        self._check_worker = JobWorker(JobType.TEST, timeout=10.0)
        self._check_worker.finished_result.connect(self._on_check_result)
        self._check_worker.failed.connect(self._on_check_failed)
        self._check_worker.start()

    def _on_check_result(self, result: JobResult) -> None:
        self.test_btn.setEnabled(True)
        if result.status == "ok":
            self.status_label.setText("Plugin OK — popup shown in Lightroom.")
        else:
            self.status_label.setText(f"Plugin returned an error: {result.error}")

    def _on_check_failed(self, message: str) -> None:
        self.test_btn.setEnabled(True)
        self.status_label.setText(f"Plugin check failed: {message}")

    # ------------------------------------------------------------------ #
    # Analyze Catalog — metadata index (no pixels)
    # ------------------------------------------------------------------ #
    def _on_analyze_catalog(self) -> None:
        if not self._require_bridge():
            return
        self.analyze_catalog_btn.setEnabled(False)
        self._progress_busy()
        self.photo_list.clear()
        self.status_label.setText("Fetching the catalog (all photos)…")
        self._worker = JobWorker(JobType.GET_CATALOG_PHOTOS, timeout=120.0)
        self._worker.finished_result.connect(self._on_catalog_result)
        self._worker.failed.connect(self._on_catalog_failed)
        self._worker.start()

    def _on_catalog_result(self, result: JobResult) -> None:
        self.analyze_catalog_btn.setEnabled(True)
        self._progress_done()
        photos = result.photos
        if not photos:
            self.status_label.setText("Empty catalog or no photo returned.")
            return
        catalog_path = next((p.catalog_path for p in photos if p.catalog_path), None)
        if catalog_path:
            self._catalog_path = catalog_path
        n_seeds = 0
        if catalog_path:
            try:
                conn = cachemod.open_cache(catalog_path)
                n_seeds = len(cachemod.list_seed_uuids(conn))
                conn.close()
            except Exception:
                pass
        cameras: dict[str, int] = {}
        for p in photos:
            cam = p.exif.camera or "?"
            cameras[cam] = cameras.get(cam, 0) + 1
        self.photo_list.clear()
        self.photo_list.addItem(
            f"Catalog: {len(photos)} photo(s) — {n_seeds} reference(s) marked."
        )
        for cam, n in sorted(cameras.items(), key=lambda kv: -kv[1]):
            self.photo_list.addItem(f"  {cam}: {n} photo(s)")
        self.status_label.setText(f"Catalog indexed — {len(photos)} photo(s).")
        self._refresh_status()

    def _on_catalog_failed(self, message: str) -> None:
        self.analyze_catalog_btn.setEnabled(True)
        self._progress_done()
        self.status_label.setText(f"Analyze Catalog failed: {message}")

    # ------------------------------------------------------------------ #
    # Step 1: fetch the Lr selection (common to all operations)
    # ------------------------------------------------------------------ #
    def _begin(self, op: str) -> None:
        if not self._require_bridge():
            return
        self._op = op
        # Any new operation invalidates a previous Preview plan.
        self._pending_adjustments = None
        self._pending_ids = frozenset()
        self._set_actions_enabled(False)
        self._progress_busy()
        self.plan_summary_label.setText("")
        self.status_label.setText("Fetching the selection…")
        self._worker = JobWorker(JobType.GET_SELECTED_PHOTOS)
        self._worker.finished_result.connect(self._on_selection)
        self._worker.failed.connect(self._on_auto_failed)
        self._worker.start()

    def _on_selection(self, result: JobResult) -> None:
        if not result.photos:
            self._set_actions_enabled(True)
            self._progress_done()
            self.status_label.setText("No photo selected in Lightroom.")
            return
        self._photos = result.photos
        catalog_path = next((p.catalog_path for p in result.photos if p.catalog_path), None)
        if catalog_path:
            self._catalog_path = catalog_path
        op = self._op

        if op == "seed_remove":
            self._apply_seed_flag(result.photos, False)
            self._set_actions_enabled(True)
            self._progress_done()  # DB flag only, no image measurement
            self._refresh_status()
            return

        if op == "neutral":
            n = len(result.photos)
            self.status_label.setText(
                f"{n} photo(s) — neutral calibration render (write + Lr render, heavy)…"
            )
            self._neutral_worker = NeutralPreviewWorker(result.photos)
            self._neutral_worker.progress.connect(self.status_label.setText)
            self._neutral_worker.progress_count.connect(self._on_progress_count)
            self._neutral_worker.finished_result.connect(self._on_neutral_done)
            self._neutral_worker.failed.connect(self._on_auto_failed)
            self._neutral_worker.start()
            return

        # ref | preview | apply: mark first (ref), then fresh render + measurement.
        if op == "ref":
            self._apply_seed_flag(result.photos, True)

        if op in ("preview", "apply") and not self._checked_axes():
            self._set_actions_enabled(True)
            self.status_label.setText("Check at least one axis (Exposure, WB, HSL or Calibration).")
            return

        # Embedded mode (neutral anchor): no measurement of the current render →
        # no fresh render to request from the plugin (the anchor comes from the
        # cache/render_probe).
        if op in ("preview", "apply") and self.cb_embedded.isChecked():
            self._thumb_paths = {}
            self._launch_measure(result.photos)
            return

        self._fetch_fresh_render(result.photos)

    # ------------------------------------------------------------------ #
    # Step 2: fresh render via the plugin (get_thumbnails) — reliable current state
    # ------------------------------------------------------------------ #
    def _fetch_fresh_render(self, photos: list) -> None:
        n = len(photos)
        self.status_label.setText(f"Fresh render of {n} photo(s) via Lightroom…")
        # Submitted in small get_thumbnails jobs rather than one job carrying every
        # photo_id (FreshRenderWorker/fetch_thumbnails_chunked): bounds each job's
        # blocking window inside the plugin's pollOnce dispatch and a timed-out
        # chunk only drops that chunk to the passive-preview fallback, not the
        # whole selection.
        self._render_worker = FreshRenderWorker(
            photos, render_metrics.MEASURE_LONG_EDGE, render_metrics.MEASURE_LONG_EDGE,
        )
        self._render_worker.progress.connect(self.status_label.setText)
        self._render_worker.progress_count.connect(self._on_progress_count)
        self._render_worker.finished_result.connect(self._on_render_ready)
        self._render_worker.failed.connect(self._on_render_failed)
        self._render_worker.start()

    def _on_render_failed(self, message: str) -> None:
        # The fresh render failed: continue anyway (passive Previews.lrdata
        # fallback in the worker), flagging the degradation.
        self.status_label.setText(f"Fresh render unavailable ({message}) — falling back to passive preview.")
        self._thumb_paths = {}
        self._launch_measure(self._photos)

    def _on_render_ready(self, thumbnails: dict) -> None:
        self._thumb_paths = {
            photo_id: t.thumbnail_path
            for photo_id, t in thumbnails.items()
            if t.thumbnail_path
        }
        got = len(self._thumb_paths)
        if got == 0:
            self.status_label.setText(
                "No fresh render provided by the plugin — falling back to passive "
                "preview (Previews.lrdata). Generate previews if the measurement fails."
            )
        self._launch_measure(self._photos)

    def _launch_measure(self, photos: list) -> None:
        op = self._op
        if op == "ref":
            self.status_label.setText(
                f"{len(photos)} reference(s) — RAW analysis + camera JPEG + render…"
            )
            self._auto_worker = AutoCorrectWorker(
                photos, analyze_only=True, thumbnail_paths=self._thumb_paths,
            )
            self._auto_worker.progress.connect(self.status_label.setText)
            self._auto_worker.progress_count.connect(self._on_progress_count)
            self._auto_worker.finished_result.connect(self._on_analyze_done)
            self._auto_worker.failed.connect(self._on_auto_failed)
            self._auto_worker.start()
            return

        # preview | apply
        axes = self._checked_axes()
        self.status_label.setText(
            f"{len(photos)} photo(s) — measuring {'/'.join(_AXIS_LABELS[a] for a in sorted(axes))}…"
        )
        self._auto_worker = AutoCorrectWorker(
            photos,
            axes=axes,
            forced_embedded=self.cb_embedded.isChecked(),
            thumbnail_paths=self._thumb_paths,
            # Apply (seeds mode): measurement always re-decoded so as not to
            # recompute a delta on a stale render. Preview: the PreviewJPEG
            # cache is enough. Embedded mode: no effect (no measurement of the
            # current render).
            force_fresh_preview=(op == "apply"),
        )
        self._auto_worker.progress.connect(self.status_label.setText)
        self._auto_worker.progress_count.connect(self._on_progress_count)
        self._auto_worker.finished_result.connect(self._on_plan_ready)
        self._auto_worker.failed.connect(self._on_auto_failed)
        self._auto_worker.start()

    # ------------------------------------------------------------------ #
    # Seed flagging in DB (common to ref / removal)
    # ------------------------------------------------------------------ #
    def _apply_seed_flag(self, photos: list, value: bool) -> None:
        catalog_path = next((p.catalog_path for p in photos if p.catalog_path), None)
        if not catalog_path:
            self.status_label.setText("No catalog_path received — cannot locate the cache.")
            return
        try:
            conn = cachemod.open_cache(catalog_path)
            # SINGLE transaction (Fable 5 review DB-03): 2 commits/photo on the
            # Qt thread froze the GUI for several seconds at 300+ photos.
            for p in photos:
                cachemod.put_picture(
                    conn, p.photo_id, path=p.path, catalog_path=p.catalog_path,
                    exif=(p.exif.model_dump() if p.exif else None),
                    current_develop=p.current_develop or {},
                    commit=False,
                )
                cachemod.set_seed(conn, p.photo_id, value, commit=False)
            conn.commit()
            conn.close()
        except Exception as exc:
            self.status_label.setText(f"Reference flagging failed: {exc}")
            return
        if not value:
            self.status_label.setText(f"{len(photos)} photo(s) removed from references.")

    # ------------------------------------------------------------------ #
    # Results
    # ------------------------------------------------------------------ #
    def _on_analyze_done(self, res: AutoCorrectResult) -> None:
        self._set_actions_enabled(True)
        self._progress_done()
        self.photo_list.clear()
        for note in res.notes:
            self.photo_list.addItem(note)
        self.status_label.setText(
            f"References marked + analyzed — {res.n_measured} measured, "
            f"{res.n_skipped} without render · usable pool: {res.seeds_usable}/{res.seeds_marked}."
        )
        self._refresh_status()

    def _on_neutral_done(self, message: str) -> None:
        self._set_actions_enabled(True)
        self._progress_done()
        self.status_label.setText(message)
        self._refresh_status()

    def _format_adjustment(self, adj) -> str:
        """Preview line "key: current → target (delta)" — embedded values are
        absolute, so the gap vs the current setting is the useful info."""
        current = next(
            (p.current_develop or {} for p in self._photos if p.photo_id == adj.photo_id), {}
        )
        parts = []
        for k, v in adj.develop.items():
            cur = current.get(k)
            if isinstance(v, (int, float)) and isinstance(cur, (int, float)):
                parts.append(f"{k}: {cur:g} → {v:g} (Δ{v - cur:+g})")
            elif isinstance(v, (int, float)):
                parts.append(f"{k}: ? → {v:g}")
            else:
                parts.append(f"{k}: {cur} → {v}")
        return f"  {adj.photo_id[:8]} → " + ", ".join(parts)

    def _on_plan_ready(self, res: AutoCorrectResult) -> None:
        if res.camera or res.profile:
            self._last_camera, self._last_profile = res.camera, res.profile
        self._refresh_status()
        diag = res.diagnostics
        self.photo_list.clear()
        if diag is not None:
            mode_label = "embedded (neutral anchor)" if diag.mode == "embedded" else diag.mode
            summary = (
                f"Mode {mode_label} — {diag.n_seeds} seed(s), {diag.n_targets} target(s), "
                f"{res.n_measured} measured, {res.n_skipped} not measurable."
            )
            if diag.n_low_confidence:
                summary += f"  ⚠ {diag.n_low_confidence} low-confidence match(es)"
            self.plan_summary_label.setText(summary)
            for note in res.notes:
                self.photo_list.addItem(f"  • {note}")
            for note in diag.notes:
                self.photo_list.addItem(f"  • {note}")

        # Silent embedded fallback: seeds are marked but unusable (missing RAW
        # analysis) → the user thinks they're correcting "to seed style".
        if (
            diag is not None and diag.mode == "embedded"
            and not self.cb_embedded.isChecked()
            and res.seeds_marked > res.seeds_usable
        ):
            self.photo_list.insertItem(
                0,
                f"⚠ {res.seeds_marked - res.seeds_usable} reference(s) marked "
                f"but not analyzed → falling back to camera JPEG. Run "
                f"\"Mark + analyze references\" on your reference shots first.",
            )

        for adj in res.adjustments[:10]:
            self.photo_list.addItem(self._format_adjustment(adj))
        if len(res.adjustments) > 10:
            self.photo_list.addItem(f"  … +{len(res.adjustments) - 10} photo(s)")

        if not res.adjustments:
            self._set_actions_enabled(True)
            self._progress_done()
            # PLAN.md U4a: n_targets==0 (every selected photo is a reference)
            # is a distinct, actionable case — surface it instead of the
            # generic message, which contradicts the n_targets shown above it.
            if diag is not None and diag.n_targets == 0 and diag.notes:
                self.status_label.setText(diag.notes[0])
            else:
                self.status_label.setText(
                    "No correction needed — photos already match the profile "
                    "(or no usable target, see notes)."
                )
            return

        if self._op == "preview":
            self._pending_adjustments = res.adjustments
            self._pending_ids = frozenset(p.photo_id for p in self._photos)
            self._set_actions_enabled(True)
            self._progress_done()
            self.status_label.setText(
                f"Preview ready — {len(res.adjustments)} correction(s). "
                f"Click \"Apply\" to confirm."
            )
            return

        # op == "apply": apply directly the plan just computed (the bar stays
        # shown until the plugin finishes applying it).
        self._submit_apply(res.adjustments)

    # ------------------------------------------------------------------ #
    # Apply — reuses the Preview plan, otherwise measure+plan+apply
    # ------------------------------------------------------------------ #
    def _on_apply_click(self) -> None:
        if not self._require_bridge():
            return
        if self._pending_adjustments is not None:
            # Fable 5 review B-01: do not replay a Preview plan if the Lr
            # selection changed in the meantime (partial/inconsistent apply
            # otherwise). Re-fetch the selection and compare against
            # `_pending_ids` before submitting.
            self._set_actions_enabled(False)
            self._progress_busy()
            self.status_label.setText("Checking the selection before applying…")
            self._worker = JobWorker(JobType.GET_SELECTED_PHOTOS)
            self._worker.finished_result.connect(self._on_apply_selection_check)
            self._worker.failed.connect(self._on_auto_failed)
            self._worker.start()
        else:
            self._begin("apply")

    def _on_apply_selection_check(self, result: JobResult) -> None:
        adjustments = self._pending_adjustments
        pending_ids = self._pending_ids
        self._pending_adjustments = None
        self._pending_ids = frozenset()
        if adjustments is None:  # defensive: plan consumed in the meantime
            self._set_actions_enabled(True)
            self._progress_done()
            self.status_label.setText("Preview plan not found — rerun Preview.")
            return
        current_ids = frozenset(p.photo_id for p in result.photos)
        if current_ids != pending_ids:
            self.status_label.setText(
                "Selection changed since the Preview — new measurement + planning…"
            )
            self._begin("apply")
            return
        self._submit_apply(adjustments)

    def _submit_apply(self, adjustments: list) -> None:
        if not adjustments:
            self._set_actions_enabled(True)
            self._progress_done()
            self.status_label.setText("Nothing to apply.")
            return
        self._progress_busy()
        self.status_label.setText(
            f"Applying — {len(adjustments)} photo(s) in Lightroom…"
        )
        payload = {"adjustments": [a.model_dump() for a in adjustments]}
        # Timeout proportional to n (Fable 5 review B-05): the plugin applies in
        # batches of 50 with a heartbeat, but a large selection easily exceeds
        # 180s total.
        timeout = max(180.0, len(adjustments) * 2.0)
        self._apply_worker = JobWorker(JobType.APPLY_ADJUSTMENTS, payload, timeout=timeout)
        self._apply_worker.finished_result.connect(self._on_apply_done)
        self._apply_worker.failed.connect(self._on_auto_failed)
        self._apply_worker.start()

    def _on_apply_done(self, result: JobResult) -> None:
        self._set_actions_enabled(True)
        self._progress_done()
        applied = result.applied if result.applied is not None else "?"
        total = result.total if result.total is not None else "?"
        if result.status == "ok":
            msg = f"Applied: {applied}/{total} photo(s) — check the result in Lightroom."
            if result.errors_summary:
                # Partial apply (Fable 5 review L-04): failure causes shown.
                msg += f" Failures: {result.errors_summary}"
                self.photo_list.insertItem(0, f"⚠ Partial apply — {result.errors_summary}")
            self.status_label.setText(msg)
        else:
            self.status_label.setText(f"Apply: {applied}/{total} — {result.error}")
        self._refresh_status()

    def _on_auto_failed(self, message: str) -> None:
        self._set_actions_enabled(True)
        self._progress_done()
        self.status_label.setText(f"Error: {message}")

    def closeEvent(self, event) -> None:
        """Stops every tracked QThread worker before the window closes.

        Without this, a worker still running when the user closes the window
        is destroyed mid-run (Qt logs "QThread: Destroyed while thread is
        still running" and the app can crash on exit) — cf. PLAN.md COV5.
        """
        for worker in (
            self._worker, self._check_worker, self._render_worker,
            self._auto_worker, self._neutral_worker, self._apply_worker,
            self._seed_worker,
        ):
            if worker is not None and worker.isRunning():
                worker.quit()
                worker.wait(5000)
        # Detach the Log tab's handler — otherwise a closed-but-not-destroyed
        # window (or a second MainWindow) keeps receiving abelr.* WARNING+
        # records into a QListWidget nobody is looking at.
        logging.getLogger("abelr").removeHandler(self._log_handler)
        super().closeEvent(event)
