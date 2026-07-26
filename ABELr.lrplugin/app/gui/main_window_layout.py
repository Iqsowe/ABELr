"""Widget construction + layout for `MainWindow` (PLAN.md U5).

Split out of `main_window.py` purely to bring that file under the target
line count — `_build_ui` is a straight relocation of `MainWindow.__init__`'s
widget/layout code, no behavior change. Mixed into `MainWindow` (not a
standalone function taking `win`) so every widget stays a plain `self.foo`
attribute, unchanged from before the split.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .log_bridge import QtLogHandler
from .op import Op


class MainWindowLayoutMixin:
    """Provides `_build_ui`, called once from `MainWindow.__init__` after the
    plain instance-state attributes are set."""

    def _build_ui(self) -> None:
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
        # U3: cooperative cancel for the probe-heavy neutral-anchor run —
        # enabled only while that specific worker is active.
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setToolTip("Stops after the chunk currently in flight finishes.")

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
        self.mark_refs_btn.clicked.connect(lambda: self._begin(Op.REF))
        self.unmark_refs_btn.clicked.connect(lambda: self._begin(Op.SEED_REMOVE))
        self.calibrate_neutral_btn.clicked.connect(lambda: self._begin(Op.NEUTRAL))
        self.cancel_btn.clicked.connect(self._on_cancel_click)
        self.preview_btn.clicked.connect(lambda: self._begin(Op.PREVIEW))
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
        actions_row.addWidget(self.cancel_btn)
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
