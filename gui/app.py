#!/usr/bin/env python3
"""Boxloader — Linux Desktop GUI alternative to iLoader (PySide6)."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Support both `python3 -m gui.app` and direct `python3 gui/app.py` invocation.
if __package__ in (None, ""):
    _pkg_parent = str(Path(__file__).resolve().parent.parent)
    if _pkg_parent not in sys.path:
        sys.path.insert(0, _pkg_parent)
    from gui.config import (  # type: ignore[import-not-found]
        AppPaths,
        IpaCandidate,
        discover_ipa_files,
    )
    from gui.dialogs import AppleLoginDialog  # type: ignore[import-not-found]
    from gui.protocol import (  # type: ignore[import-not-found]
        INSTALL_STAGES,
        DeviceInfo,
        SecretSanitizer,
        SessionStatus,
        parse_devices_output,
        parse_session_status_output,
        stage_label,
        stage_step_index,
    )
    from gui.theme import APP_QSS  # type: ignore[import-not-found]
    from gui.workers import CoreCommandWorker  # type: ignore[import-not-found]
else:
    from .config import (
        AppPaths,
        IpaCandidate,
        discover_ipa_files,
    )
    from .dialogs import AppleLoginDialog
    from .protocol import (
        INSTALL_STAGES,
        DeviceInfo,
        SecretSanitizer,
        SessionStatus,
        parse_devices_output,
        parse_session_status_output,
        stage_label,
        stage_step_index,
    )
    from .theme import APP_QSS
    from .workers import CoreCommandWorker

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


def _apply_pill_style(label: QLabel, style_name: str, text: str) -> None:
    label.setObjectName(style_name)
    label.setText(text)
    style = label.style()
    if style is not None:
        style.unpolish(label)
        style.polish(label)


class BoxloaderWindow(QMainWindow):
    """Native-launched GUI for IPA sideloading and connection diagnostics."""

    def __init__(
        self,
        paths: AppPaths | None = None,
        *,
        auto_refresh: bool = True,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.paths = paths or AppPaths.discover()
        self._sanitizer = SecretSanitizer()
        self._devices: list[DeviceInfo] = []
        self._session_status: SessionStatus | None = None
        self._ipa_candidates: list[IpaCandidate] = []
        self._ipa_chip_buttons: list[tuple[IpaCandidate, QPushButton]] = []

        self._devices_worker: CoreCommandWorker | None = None
        self._session_worker: CoreCommandWorker | None = None
        self._active_op_worker: CoreCommandWorker | None = None
        self._active_op_kind: str = ""  # "ipa" | "pairing" | "sidebox"

        self.setWindowTitle("Boxloader — установка IPA на iPhone")
        self.resize(960, 740)
        self.setMinimumSize(820, 620)
        if self.paths.icon_path.is_file():
            self.setWindowIcon(QIcon(str(self.paths.icon_path)))

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        main_layout.addWidget(self._build_top_bar())

        body_wrap = QWidget()
        body_layout = QVBoxLayout(body_wrap)
        body_layout.setContentsMargins(16, 10, 16, 6)
        body_layout.setSpacing(8)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_ipa_tab(), "Установка IPA (iLoader)")
        self.tabs.addTab(self._build_settings_log_tab(), "Журнал и настройки")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        body_layout.addWidget(self.tabs, 1)

        main_layout.addWidget(body_wrap, 1)
        self.bottom_drawer = self._build_bottom_log_drawer()
        main_layout.addWidget(self.bottom_drawer)

        self._populate_ipa_chips()
        self._reset_install_stages()

        if auto_refresh:
            QTimer.singleShot(50, self._initial_refresh)

    # ------------------------------------------------------------------
    # Top Device & Apple ID Bar
    # ------------------------------------------------------------------

    def _build_top_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("TopBar")
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(8)

        # Row 1: Brand + Apple ID pill & login
        row1 = QHBoxLayout()
        row1.setSpacing(10)

        icon_lbl = QLabel()
        icon_lbl.setFixedSize(34, 34)
        if self.paths.icon_path.is_file():
            pm = QPixmap(str(self.paths.icon_path)).scaled(
                34,
                34,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            icon_lbl.setPixmap(pm)
        row1.addWidget(icon_lbl)

        brand_col = QVBoxLayout()
        brand_col.setSpacing(0)
        eyebrow = QLabel("BOXLOADER")
        eyebrow.setObjectName("BrandEyebrow")
        title = QLabel("Менеджер установки для iPhone")
        title.setObjectName("BrandTitle")
        brand_col.addWidget(eyebrow)
        brand_col.addWidget(title)
        row1.addLayout(brand_col)

        row1.addStretch(1)

        self.apple_pill = QLabel("Apple ID: проверка…")
        self.apple_pill.setObjectName("PillNeutral")
        row1.addWidget(self.apple_pill)

        self.apple_login_btn = QPushButton("Войти в Apple ID")
        self.apple_login_btn.clicked.connect(self._open_apple_login)
        row1.addWidget(self.apple_login_btn)

        layout.addLayout(row1)

        # Row 2: USB device selector + paired badge + refresh
        row2 = QHBoxLayout()
        row2.setSpacing(10)

        dev_lbl = QLabel("iPhone по USB:")
        dev_lbl.setStyleSheet("font-weight: 600; color: #b8c1de;")
        row2.addWidget(dev_lbl)

        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(340)
        self.device_combo.addItem("Поиск подключённых устройств…", None)
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        row2.addWidget(self.device_combo, 1)

        self.paired_pill = QLabel("Не подключён")
        self.paired_pill.setObjectName("PillNeutral")
        row2.addWidget(self.paired_pill)

        self.refresh_devices_btn = QPushButton("Обновить")
        self.refresh_devices_btn.clicked.connect(self.refresh_all_status)
        row2.addWidget(self.refresh_devices_btn)

        layout.addLayout(row2)
        return bar

    # ------------------------------------------------------------------
    # Tab 1: Установка IPA (iLoader)
    # ------------------------------------------------------------------

    def _build_ipa_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(2, 4, 2, 4)
        layout.setSpacing(10)

        # Card 1: IPA selection + quick-select chips
        ipa_card = QFrame()
        ipa_card.setObjectName("Card")
        ipa_lay = QVBoxLayout(ipa_card)
        ipa_lay.setContentsMargins(14, 11, 14, 11)
        ipa_lay.setSpacing(8)

        h1 = QLabel("ВЫБОР ФАЙЛА .IPA")
        h1.setObjectName("SectionHeading")
        ipa_lay.addWidget(h1)

        picker_row = QHBoxLayout()
        picker_row.setSpacing(8)
        self.ipa_path_edit = QLineEdit()
        self.ipa_path_edit.setPlaceholderText("Выберите .ipa-файл для установки на iPhone…")
        self.ipa_path_edit.textChanged.connect(self._on_ipa_path_changed)
        picker_row.addWidget(self.ipa_path_edit, 1)

        self.ipa_browse_btn = QPushButton("Обзор…")
        self.ipa_browse_btn.clicked.connect(self._browse_ipa)
        picker_row.addWidget(self.ipa_browse_btn)
        ipa_lay.addLayout(picker_row)

        chips_hdr = QLabel("Найденные .ipa в папке outputs:")
        chips_hdr.setObjectName("MutedText")
        ipa_lay.addWidget(chips_hdr)

        self.chips_container = QWidget()
        self.chips_layout = QGridLayout(self.chips_container)
        self.chips_layout.setContentsMargins(0, 0, 0, 0)
        self.chips_layout.setHorizontalSpacing(8)
        self.chips_layout.setVerticalSpacing(6)
        ipa_lay.addWidget(self.chips_container)

        self.trollstore_notice = QLabel(
            "Этот пакет предназначен для TrollStore и не поддерживает обычную USB-подпись. "
            "Boxloader устанавливает стандартные .ipa через Apple ID."
        )
        self.trollstore_notice.setObjectName("TrollStoreNotice")
        self.trollstore_notice.setWordWrap(True)
        self.trollstore_notice.setVisible(False)
        ipa_lay.addWidget(self.trollstore_notice)

        layout.addWidget(ipa_card)

        # Card 2: Primary Install action + live progress stages
        action_card = QFrame()
        action_card.setObjectName("Card")
        act_lay = QVBoxLayout(action_card)
        act_lay.setContentsMargins(14, 11, 14, 11)
        act_lay.setSpacing(9)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.install_ipa_btn = QPushButton("Установить .ipa на iPhone")
        self.install_ipa_btn.setObjectName("PrimaryButton")
        self.install_ipa_btn.setMinimumHeight(40)
        self.install_ipa_btn.clicked.connect(self._start_install_ipa)
        btn_row.addWidget(self.install_ipa_btn, 1)

        self.cancel_ipa_btn = QPushButton("Отмена")
        self.cancel_ipa_btn.setObjectName("DangerButton")
        self.cancel_ipa_btn.setMinimumHeight(40)
        self.cancel_ipa_btn.setVisible(False)
        self.cancel_ipa_btn.clicked.connect(self._cancel_active_operation)
        btn_row.addWidget(self.cancel_ipa_btn)
        act_lay.addLayout(btn_row)

        stages_row = QHBoxLayout()
        stages_row.setSpacing(6)
        self._stage_labels: dict[str, QLabel] = {}
        for idx, (code, caption) in enumerate(INSTALL_STAGES):
            lbl = QLabel(caption)
            lbl.setObjectName("StageIdle")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            stages_row.addWidget(lbl, 1)
            self._stage_labels[code] = lbl
            if idx + 1 < len(INSTALL_STAGES):
                arrow = QLabel("→")
                arrow.setStyleSheet("color: #4d5678; font-weight: 700;")
                stages_row.addWidget(arrow)
        act_lay.addLayout(stages_row)

        self.ipa_progress_bar = QProgressBar()
        self.ipa_progress_bar.setRange(0, 100)
        self.ipa_progress_bar.setValue(0)
        act_lay.addWidget(self.ipa_progress_bar)

        self.ipa_status_label = QLabel("Выберите .ipa и нажмите «Установить .ipa на iPhone».")
        self.ipa_status_label.setObjectName("MutedText")
        self.ipa_status_label.setWordWrap(True)
        act_lay.addWidget(self.ipa_status_label)

        layout.addWidget(action_card)

        # Card 3: Quick USB Tools
        tools_card = QFrame()
        tools_card.setObjectName("Card")
        tools_lay = QVBoxLayout(tools_card)
        tools_lay.setContentsMargins(14, 11, 14, 11)
        tools_lay.setSpacing(8)

        h_tools = QLabel("БЫСТРЫЕ ИНСТРУМЕНТЫ USB")
        h_tools.setObjectName("SectionHeading")
        tools_lay.addWidget(h_tools)

        self.setup_sidebox_btn = QPushButton("Настроить Sidebox (pairing + Apple ID)")
        self.setup_sidebox_btn.setObjectName("SideBoxSetupButton")
        self.setup_sidebox_btn.setMinimumHeight(38)
        self.setup_sidebox_btn.clicked.connect(self._start_setup_sidebox)
        tools_lay.addWidget(self.setup_sidebox_btn)

        tools_hint = QLabel(
            "Настройка Sidebox переносит pairing-файл и сохранённую сессию Apple ID на iPhone. "
            "Продление подписей в этой версии Boxloader отключено."
        )
        tools_hint.setObjectName("MutedText")
        tools_hint.setWordWrap(True)
        tools_lay.addWidget(tools_hint)

        layout.addWidget(tools_card)
        layout.addStretch(1)

        scroll.setWidget(inner)
        return scroll

    # ------------------------------------------------------------------
    # Tab 2: Журнал и настройки
    # ------------------------------------------------------------------

    def _build_settings_log_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 6, 4, 6)
        layout.setSpacing(10)

        cfg_card = QFrame()
        cfg_card.setObjectName("Card")
        cfg_outer = QVBoxLayout(cfg_card)
        cfg_outer.setContentsMargins(16, 12, 16, 12)
        cfg_outer.setSpacing(8)

        h_set = QLabel("ПУТИ И НАСТРОЙКИ")
        h_set.setObjectName("SectionHeading")
        cfg_outer.addWidget(h_set)

        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Apple session directory
        s_row = QHBoxLayout()
        s_row.setSpacing(8)
        self.settings_session_edit = QLineEdit(str(self.paths.session_dir))
        s_row.addWidget(self.settings_session_edit, 1)
        s_browse = QPushButton("Обзор…")
        s_browse.clicked.connect(self._browse_settings_session)
        s_row.addWidget(s_browse)
        s_check = QPushButton("Проверить")
        s_check.clicked.connect(self._apply_settings_and_refresh)
        s_row.addWidget(s_check)
        form.addRow("Каталог сессии Apple:", s_row)

        # Core binary path
        c_row = QHBoxLayout()
        c_row.setSpacing(8)
        self.settings_core_edit = QLineEdit(str(self.paths.core_bin))
        c_row.addWidget(self.settings_core_edit, 1)
        c_browse = QPushButton("Обзор…")
        c_browse.clicked.connect(self._browse_core_bin)
        c_row.addWidget(c_browse)
        form.addRow("dopamine-loader-core:", c_row)

        # Outputs dir
        o_row = QHBoxLayout()
        o_row.setSpacing(8)
        self.settings_outputs_edit = QLineEdit(str(self.paths.outputs_dir))
        o_row.addWidget(self.settings_outputs_edit, 1)
        o_browse = QPushButton("Обзор…")
        o_browse.clicked.connect(self._browse_outputs_dir)
        o_row.addWidget(o_browse)
        form.addRow("Папка с .ipa / .deb:", o_row)

        cfg_outer.addLayout(form)
        layout.addWidget(cfg_card)

        # Full log card
        log_card = QFrame()
        log_card.setObjectName("Card")
        log_lay = QVBoxLayout(log_card)
        log_lay.setContentsMargins(14, 10, 14, 12)
        log_lay.setSpacing(8)

        log_hdr = QHBoxLayout()
        hl = QLabel("ПОЛНЫЙ ЖУРНАЛ ОПЕРАЦИЙ")
        hl.setObjectName("SectionHeading")
        log_hdr.addWidget(hl)
        log_hdr.addStretch(1)

        copy_btn = QPushButton("Копировать")
        copy_btn.clicked.connect(self._copy_logs)
        log_hdr.addWidget(copy_btn)

        clear_btn = QPushButton("Очистить")
        clear_btn.clicked.connect(self._clear_logs)
        log_hdr.addWidget(clear_btn)
        log_lay.addLayout(log_hdr)

        self.full_log_edit = QPlainTextEdit()
        self.full_log_edit.setReadOnly(True)
        self.full_log_edit.setMaximumBlockCount(2000)
        log_lay.addWidget(self.full_log_edit, 1)

        layout.addWidget(log_card, 1)
        return tab

    # ------------------------------------------------------------------
    # Collapsible Bottom Live Log Drawer
    # ------------------------------------------------------------------

    def _build_bottom_log_drawer(self) -> QFrame:
        drawer = QFrame()
        drawer.setObjectName("LogDrawer")
        lay = QVBoxLayout(drawer)
        lay.setContentsMargins(14, 6, 14, 8)
        lay.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        self.drawer_toggle_btn = QPushButton("▾ Журнал операций")
        self.drawer_toggle_btn.setObjectName("DrawerToggle")
        self.drawer_toggle_btn.clicked.connect(self._toggle_drawer)
        hdr.addWidget(self.drawer_toggle_btn)

        hdr.addStretch(1)

        self.drawer_copy_btn = QPushButton("Копировать")
        self.drawer_copy_btn.clicked.connect(self._copy_logs)
        hdr.addWidget(self.drawer_copy_btn)

        self.drawer_clear_btn = QPushButton("Очистить")
        self.drawer_clear_btn.clicked.connect(self._clear_logs)
        hdr.addWidget(self.drawer_clear_btn)
        lay.addLayout(hdr)

        self.drawer_log_edit = QPlainTextEdit()
        self.drawer_log_edit.setReadOnly(True)
        self.drawer_log_edit.setMaximumBlockCount(1000)
        self.drawer_log_edit.setFixedHeight(84)
        self.drawer_log_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        lay.addWidget(self.drawer_log_edit)
        return drawer

    def _on_tab_changed(self, index: int) -> None:
        # The second tab already contains the full operation log.
        self.bottom_drawer.setVisible(index != 1)

    def _toggle_drawer(self) -> None:
        visible = not self.drawer_log_edit.isVisible()
        self.drawer_log_edit.setVisible(visible)
        self.drawer_copy_btn.setVisible(visible)
        self.drawer_clear_btn.setVisible(visible)
        self.drawer_toggle_btn.setText(
            "▾ Журнал операций" if visible else "▸ Журнал операций"
        )

    # ------------------------------------------------------------------
    # Logging (strictly sanitized — never logs passwords/tokens)
    # ------------------------------------------------------------------

    def append_log(self, raw_text: str) -> None:
        safe = self._sanitizer.sanitize(raw_text).strip()
        if not safe:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{stamp}] {safe}"
        for edit in (self.drawer_log_edit, self.full_log_edit):
            edit.appendPlainText(formatted)
            sb = edit.verticalScrollBar()
            if sb is not None:
                sb.setValue(sb.maximum())

    def _copy_logs(self) -> None:
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(self.full_log_edit.toPlainText())
            self.append_log("Журнал скопирован в буфер обмена.")

    def _clear_logs(self) -> None:
        self.drawer_log_edit.clear()
        self.full_log_edit.clear()

    # ------------------------------------------------------------------
    # Paths / IPA Chips / Profile Preview
    # ------------------------------------------------------------------

    def _current_core_bin(self) -> Path:
        txt = self.settings_core_edit.text().strip()
        return Path(txt) if txt else self.paths.core_bin

    def _current_session_dir(self) -> Path:
        txt = self.settings_session_edit.text().strip()
        return Path(txt) if txt else self.paths.session_dir

    def _current_outputs_dir(self) -> Path:
        txt = self.settings_outputs_edit.text().strip()
        return Path(txt) if txt else self.paths.outputs_dir

    def selected_udid(self) -> str | None:
        data = self.device_combo.currentData()
        return str(data) if isinstance(data, str) and data else None

    def _populate_ipa_chips(self) -> None:
        while self.chips_layout.count():
            item = self.chips_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._ipa_chip_buttons.clear()

        candidates = discover_ipa_files(self._current_outputs_dir())
        self._ipa_candidates = candidates

        if not candidates:
            empty = QLabel("Файлы .ipa в папке outputs не найдены — выберите файл кнопкой «Обзор…».")
            empty.setObjectName("MutedText")
            self.chips_layout.addWidget(empty, 0, 0)
            return

        current_text = self.ipa_path_edit.text().strip()
        default_pick: IpaCandidate | None = None
        cols = 3
        for idx, cand in enumerate(candidates[:9]):
            if default_pick is None and not cand.is_trollstore_only:
                default_pick = cand

            if cand.is_featured:
                label = f"★ {cand.name} ({cand.size_label})"
            elif cand.is_trollstore_only:
                label = f"{cand.name} · TrollStore"
            else:
                label = f"{cand.name} ({cand.size_label})"

            btn = QPushButton(label)
            btn.setObjectName(
                "IpaChipTrollStore"
                if cand.is_trollstore_only
                else ("IpaChipFeatured" if cand.is_featured else "IpaChip")
            )
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, p=cand.path: self._select_ipa_path(p))
            self.chips_layout.addWidget(btn, idx // cols, idx % cols)
            self._ipa_chip_buttons.append((cand, btn))

        if not current_text and default_pick is not None:
            self._select_ipa_path(default_pick.path)
        else:
            self._update_chip_selection_styles()

    def _select_ipa_path(self, path: Path) -> None:
        self.ipa_path_edit.setText(str(path))
        self._update_chip_selection_styles()

    def _on_ipa_path_changed(self, text: str) -> None:
        self._update_chip_selection_styles()
        name = Path(text.strip()).name.lower() if text.strip() else ""
        self.trollstore_notice.setVisible(name.startswith("dopaminerenew"))

    def _update_chip_selection_styles(self) -> None:
        current = self.ipa_path_edit.text().strip()
        for cand, btn in self._ipa_chip_buttons:
            if str(cand.path) == current:
                obj = "IpaChipTrollStore" if cand.is_trollstore_only else "IpaChipSelected"
            elif cand.is_trollstore_only:
                obj = "IpaChipTrollStore"
            elif cand.is_featured:
                obj = "IpaChipFeatured"
            else:
                obj = "IpaChip"
            btn.setObjectName(obj)
            style = btn.style()
            if style is not None:
                style.unpolish(btn)
                style.polish(btn)

    # ------------------------------------------------------------------
    # File / Directory Browsers
    # ------------------------------------------------------------------

    def _browse_ipa(self) -> None:
        start_dir = str(self._current_outputs_dir())
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите файл .ipa", start_dir, "iOS приложения (*.ipa);;Все файлы (*)"
        )
        if path:
            self._select_ipa_path(Path(path))

    def _browse_settings_session(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Выберите каталог сессии Apple ID", self.settings_session_edit.text()
        )
        if path:
            self.settings_session_edit.setText(path)
            self.refresh_session_status()

    def _browse_core_bin(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите dopamine-loader-core", str(self._current_core_bin().parent)
        )
        if path:
            self.settings_core_edit.setText(path)

    def _browse_outputs_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Выберите папку с .ipa / .deb", self.settings_outputs_edit.text()
        )
        if path:
            self.settings_outputs_edit.setText(path)
            self._populate_ipa_chips()

    def _apply_settings_and_refresh(self) -> None:
        self._populate_ipa_chips()
        self.refresh_all_status()

    # ------------------------------------------------------------------
    # Device & Apple ID Status Refresh (non-blocking background workers)
    # ------------------------------------------------------------------

    def _initial_refresh(self) -> None:
        self.append_log("Boxloader запущен. Проверяем USB-устройства и сессию Apple ID…")
        self.refresh_all_status()

    def refresh_all_status(self) -> None:
        self.refresh_devices()
        self.refresh_session_status()

    def refresh_devices(self) -> None:
        if self._devices_worker is not None and self._devices_worker.isRunning():
            return
        core = self._current_core_bin()
        if not core.is_file():
            self.apply_devices([], f"Ядро не найдено: {core}")
            return
        self.refresh_devices_btn.setEnabled(False)
        worker = CoreCommandWorker(core, ["devices", "--json"], stream_logs=False, parent=self)
        worker.completed.connect(self._on_devices_loaded)
        self._devices_worker = worker
        worker.start()

    def _on_devices_loaded(self, _ok: bool, message: str, raw_stdout: str, _payload: object) -> None:
        self.refresh_devices_btn.setEnabled(True)
        devices, err = parse_devices_output(raw_stdout)
        if not devices and err is None and not _ok:
            err = message
        self.apply_devices(devices, err)

    def apply_devices(self, devices: list[DeviceInfo], error_msg: str | None = None) -> None:
        prev_udid = self.selected_udid()
        self._devices = list(devices)

        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        if not devices:
            placeholder = (
                f"Ошибка USB: {error_msg}" if error_msg else "iPhone не подключён по USB"
            )
            self.device_combo.addItem(placeholder, None)
            self.device_combo.setToolTip(error_msg or "Подключите iPhone кабелем USB и разблокируйте экран.")
        else:
            restore_idx = 0
            for idx, dev in enumerate(devices):
                self.device_combo.addItem(dev.display_label(), dev.udid)
                if prev_udid and dev.udid == prev_udid:
                    restore_idx = idx
            self.device_combo.setCurrentIndex(restore_idx)
        self.device_combo.blockSignals(False)
        self._on_device_changed()

        if error_msg:
            self.append_log(f"[USB] {error_msg}")
        elif devices:
            self.append_log(f"[USB] Найдено устройств: {len(devices)} ({devices[0].display_label()})")

    def _on_device_changed(self) -> None:
        udid = self.selected_udid()
        selected = next((d for d in self._devices if d.udid == udid), None)
        if selected is None:
            _apply_pill_style(self.paired_pill, "PillNeutral", "Не подключён")
            self.device_combo.setToolTip("")
            return
        self.device_combo.setToolTip(
            f"{selected.name or 'iPhone'}\nМодель: {selected.product_type or '—'}\n"
            f"iOS: {selected.product_version or '—'}\nUDID: {selected.udid}"
        )
        if selected.paired:
            _apply_pill_style(self.paired_pill, "PillGreen", "Пара создана")
        else:
            _apply_pill_style(self.paired_pill, "PillAmber", "Доверьтесь этому ПК")

    def refresh_session_status(self) -> None:
        if self._session_worker is not None and self._session_worker.isRunning():
            return
        core = self._current_core_bin()
        sess_dir = self._current_session_dir()
        if not core.is_file():
            self.apply_session_status(
                SessionStatus(exists=False, valid=False, message="Ядро не найдено")
            )
            return
        worker = CoreCommandWorker(
            core,
            ["session-status", "--session-dir", str(sess_dir), "--json"],
            stream_logs=False,
            parent=self,
        )
        worker.completed.connect(self._on_session_loaded)
        self._session_worker = worker
        worker.start()

    def _on_session_loaded(self, _ok: bool, message: str, raw_stdout: str, _payload: object) -> None:
        status = parse_session_status_output(raw_stdout)
        if status is None:
            status = SessionStatus(exists=False, valid=False, message=message)
        self.apply_session_status(status)

    def apply_session_status(self, status: SessionStatus) -> None:
        self._session_status = status
        if status.valid:
            suffix = f" ({status.email_masked})" if status.email_masked else ""
            _apply_pill_style(self.apple_pill, "PillGreen", f"Apple ID: активна{suffix}")
            self.apple_pill.setToolTip(status.message or "Сессия Apple ID действительна.")
        else:
            _apply_pill_style(self.apple_pill, "PillAmber", "Apple ID: требуется вход")
            self.apple_pill.setToolTip(status.message or "Нажмите «Войти в Apple ID» для авторизации.")

    # ------------------------------------------------------------------
    # Apple ID Login Modal
    # ------------------------------------------------------------------

    def _open_apple_login(self) -> None:
        core = self._current_core_bin()
        if not core.is_file():
            QMessageBox.warning(
                self,
                "Ядро не найдено",
                f"Не найден файл dopamine-loader-core:\n{core}\n\nПроверьте путь на вкладке «Журнал и настройки».",
            )
            return
        has_existing = bool(self._session_status and self._session_status.exists)
        dlg = AppleLoginDialog(
            core,
            self._current_session_dir(),
            has_existing_session=has_existing,
            parent=self,
        )
        dlg.log_line.connect(self.append_log)
        if dlg.exec() == AppleLoginDialog.DialogCode.Accepted and dlg.result_ok:
            self.append_log(f"[Apple ID] {dlg.result_message or 'Сессия сохранена.'}")
            self.refresh_session_status()

    # ------------------------------------------------------------------
    # Busy State Management & Stage Progress UI
    # ------------------------------------------------------------------

    def _set_operation_busy(self, busy: bool, kind: str = "") -> None:
        self._active_op_kind = kind if busy else ""
        self.install_ipa_btn.setEnabled(not busy)
        self.setup_sidebox_btn.setEnabled(not busy)
        self.apple_login_btn.setEnabled(not busy)
        self.ipa_browse_btn.setEnabled(not busy)
        self.cancel_ipa_btn.setVisible(busy and kind in ("ipa", "sidebox"))

    def _reset_install_stages(self) -> None:
        for _code, lbl in self._stage_labels.items():
            lbl.setObjectName("StageIdle")
            style = lbl.style()
            if style is not None:
                style.unpolish(lbl)
                style.polish(lbl)

    def update_install_stage(self, stage: str, percent: int, message: str) -> None:
        idx = stage_step_index(stage)
        for pos, (code, _caption) in enumerate(INSTALL_STAGES):
            lbl = self._stage_labels[code]
            if idx < 0:
                obj = "StageIdle"
            elif code == "done" and idx == len(INSTALL_STAGES) - 1:
                obj = "StageDone"
            elif pos < idx:
                obj = "StageDone"
            elif pos == idx:
                obj = "StageActive"
            else:
                obj = "StageIdle"
            lbl.setObjectName(obj)
            style = lbl.style()
            if style is not None:
                style.unpolish(lbl)
                style.polish(lbl)

        if percent < 0:
            self.ipa_progress_bar.setRange(0, 0)
        else:
            self.ipa_progress_bar.setRange(0, 100)
            self.ipa_progress_bar.setValue(max(0, min(100, percent)))

        nice_stage = stage_label(stage)
        if nice_stage and message:
            self.ipa_status_label.setText(f"{nice_stage}: {message}")
        elif message:
            self.ipa_status_label.setText(message)
        elif nice_stage:
            self.ipa_status_label.setText(nice_stage)

    def _cancel_active_operation(self) -> None:
        if self._active_op_worker is not None and self._active_op_worker.isRunning():
            self.append_log("Отменяем текущую операцию…")
            self._active_op_worker.cancel()

    # ------------------------------------------------------------------
    # Tab 1 Operations: install-ipa, Sidebox pairing setup
    # ------------------------------------------------------------------

    def _start_install_ipa(self) -> None:
        if self._active_op_worker is not None and self._active_op_worker.isRunning():
            return
        ipa_raw = self.ipa_path_edit.text().strip()
        if not ipa_raw:
            QMessageBox.warning(self, "Выберите файл", "Сначала выберите файл .ipa для установки.")
            return
        ipa_path = Path(ipa_raw)
        if not ipa_path.is_file():
            QMessageBox.warning(self, "Файл не найден", f"Файл .ipa не существует:\n{ipa_path}")
            return

        if ipa_path.name.lower().startswith("dopaminerenew"):
            ans = QMessageBox.question(
                self,
                "Пакет для TrollStore",
                "DopamineRenew-*.ipa предназначен для TrollStore (с системными правами). "
                "При установке обычным бесплатным Apple ID системные права не сработают.\n\n"
                "Этот пакет рекомендуется устанавливать через TrollStore.\n"
                "Всё равно продолжить подпись через Apple ID?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        if self._session_status is not None and not self._session_status.valid:
            QMessageBox.warning(
                self,
                "Требуется вход в Apple ID",
                "Сессия Apple ID отсутствует или недействительна. Нажмите «Войти в Apple ID» в верхней панели.",
            )
            return

        core = self._current_core_bin()
        args = [
            "install-ipa",
            "--ipa",
            str(ipa_path),
            "--session-dir",
            str(self._current_session_dir()),
            "--json",
        ]
        udid = self.selected_udid()
        if udid:
            args.extend(["--udid", udid])

        self._reset_install_stages()
        self.update_install_stage("connecting", 5, f"Подготовка установки {ipa_path.name}…")
        self._set_operation_busy(True, "ipa")
        self.append_log(f"Запуск установки .ipa: {ipa_path.name}")

        worker = CoreCommandWorker(core, args, stream_logs=True, parent=self)
        worker.log_line.connect(self.append_log)
        worker.progress.connect(self.update_install_stage)
        worker.completed.connect(self._on_ipa_op_finished)
        self._active_op_worker = worker
        worker.start()

    def build_setup_sidebox_args(self) -> list[str]:
        args = ["install-pairing", "--target", "sidebox", "--json"]
        sess_dir = self._current_session_dir()
        if (sess_dir / "apple-session.plist").is_file():
            args.extend(["--sync-session-dir", str(sess_dir)])
        udid = self.selected_udid()
        if udid:
            args.extend(["--udid", udid])
        return args

    def _start_setup_sidebox(self) -> None:
        if self._active_op_worker is not None and self._active_op_worker.isRunning():
            return
        core = self._current_core_bin()
        args = self.build_setup_sidebox_args()
        syncing_session = "--sync-session-dir" in args

        self._reset_install_stages()
        self.ipa_progress_bar.setRange(0, 0)
        self.ipa_status_label.setText(
            "Перенос pairing-файла и сессии Apple ID в Sidebox…"
            if syncing_session
            else "Перенос pairing-файла в Sidebox (сессия Apple ID не найдена)…"
        )
        self._set_operation_busy(True, "sidebox")
        self.append_log(
            "Запуск настройки Sidebox (pairing + Apple ID)…"
            if syncing_session
            else "Запуск настройки Sidebox (только pairing-файл; для переноса Apple ID войдите сначала)…"
        )

        worker = CoreCommandWorker(core, args, stream_logs=True, parent=self)
        worker.log_line.connect(self.append_log)
        worker.progress.connect(self.update_install_stage)
        worker.completed.connect(self._on_ipa_op_finished)
        self._active_op_worker = worker
        worker.start()

    def _on_ipa_op_finished(
        self, ok: bool, message: str, _raw_stdout: str, payload: object
    ) -> None:
        kind = self._active_op_kind
        self._set_operation_busy(False)
        self._active_op_worker = None
        self.ipa_progress_bar.setRange(0, 100)

        if ok:
            self.ipa_progress_bar.setValue(100)
            if kind == "ipa":
                self.update_install_stage("done", 100, message or "Приложение установлено!")
                if isinstance(payload, dict):
                    special = payload.get("special_app")
                    bid = str(payload.get("bundle_id") or "")
                    if special in ("Sidebox", "SideBox") or bid.startswith(
                        ("com.bromhobit.Sidebox", "org.codex.SideBox")
                    ):
                        self.ipa_status_label.setText(
                            f"Готово: {message} Теперь нажмите «Настроить Sidebox (pairing + Apple ID)»."
                        )
            else:
                self.ipa_status_label.setText(f"Готово: {message}")
            self.append_log(f"Успешно: {message}")
        else:
            self.ipa_status_label.setText(f"Ошибка: {message}")
            self.append_log(f"Ошибка: {message}")

    # ------------------------------------------------------------------
    # Clean Shutdown (guarantees no orphan subprocess / iproxy)
    # ------------------------------------------------------------------

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        for worker in (self._active_op_worker, self._devices_worker, self._session_worker):
            if worker is not None and worker.isRunning():
                worker.cancel()
                worker.wait(3000)
        super().closeEvent(event)


DopamineLoaderWindow = BoxloaderWindow  # legacy API alias


def parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Boxloader Linux Desktop GUI")
    parser.add_argument("--core", type=Path, default=None, help="Path to dopamine-loader-core")
    parser.add_argument("--setup-script", type=Path, default=None, help="Path to setup-device.sh")
    parser.add_argument("--outputs", type=Path, default=None, help="Outputs directory with .ipa/.deb")
    parser.add_argument("--work", type=Path, default=None, help="Work directory")
    parser.add_argument("--session-dir", type=Path, default=None, help="Apple ID session directory")
    parser.add_argument("--icon", type=Path, default=None, help="Application PNG icon path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_cli_args(argv)
    paths = AppPaths.discover(
        outputs_override=args.outputs,
        work_override=args.work,
        core_override=args.core,
        setup_script_override=args.setup_script,
        session_override=args.session_dir,
        icon_override=args.icon,
    )
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    app.setApplicationName("Boxloader")
    app.setOrganizationName("Codex")
    app.setStyleSheet(APP_QSS)
    if paths.icon_path.is_file():
        app.setWindowIcon(QIcon(str(paths.icon_path)))

    window = BoxloaderWindow(paths=paths, auto_refresh=True)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
