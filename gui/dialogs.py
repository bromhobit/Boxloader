"""Modal dialogs for Apple ID login (+ dynamic 2FA) and USB profile renewal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from .workers import AppleLoginWorker


class AppleLoginDialog(QDialog):
    """Modal dialog handling Apple ID credentials (stdin-only) and live 2FA prompt."""

    log_line = Signal(str)

    def __init__(
        self,
        core_bin: Path,
        session_dir: Path,
        *,
        has_existing_session: bool = False,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._core_bin = Path(core_bin)
        self._session_dir = Path(session_dir)
        self._worker: AppleLoginWorker | None = None
        self.result_ok: bool = False
        self.result_message: str = ""
        self.result_email_masked: str = ""

        self.setWindowTitle("Вход в Apple ID")
        self.setModal(True)
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        title = QLabel("Авторизация в Apple ID")
        title.setObjectName("BrandTitle")
        root.addWidget(title)

        note = QLabel(
            "Пароль передаётся напрямую через stdin в локальный процесс, "
            "не логируется и не сохраняется на диск. В каталоге сессии сохраняется "
            "только файл сессии Apple с правами 0600."
        )
        note.setObjectName("MutedText")
        note.setWordWrap(True)
        root.addWidget(note)

        form_card = QFrame()
        form_card.setObjectName("Card")
        form_layout = QFormLayout(form_card)
        form_layout.setContentsMargins(16, 16, 16, 16)
        form_layout.setSpacing(10)

        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("name@icloud.com")
        form_layout.addRow("Apple ID (Email):", self.email_edit)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("Пароль Apple ID")
        form_layout.addRow("Пароль:", self.password_edit)

        self.overwrite_check = QCheckBox("Перезаписать существующую сессию")
        self.overwrite_check.setChecked(has_existing_session)
        form_layout.addRow("", self.overwrite_check)

        root.addWidget(form_card)

        # Dynamic 2FA card (revealed when `need_2fa` arrives from dopamine-loader-core)
        self.tfa_card = QFrame()
        self.tfa_card.setObjectName("Card")
        self.tfa_card.setVisible(False)
        tfa_layout = QVBoxLayout(self.tfa_card)
        tfa_layout.setContentsMargins(16, 14, 16, 14)
        tfa_layout.setSpacing(10)

        self.tfa_title = QLabel("Двухфакторная аутентификация (2FA)")
        self.tfa_title.setStyleSheet("font-weight: 700; color: #a78bfa; font-size: 13px;")
        tfa_layout.addWidget(self.tfa_title)

        self.tfa_prompt_label = QLabel("Введите 6-значный код подтверждения Apple ID:")
        self.tfa_prompt_label.setWordWrap(True)
        tfa_layout.addWidget(self.tfa_prompt_label)

        tfa_row = QHBoxLayout()
        tfa_row.setSpacing(8)
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("123456")
        self.code_edit.setMaxLength(12)
        self.code_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code_edit.setStyleSheet("font-size: 18px; font-weight: 700; letter-spacing: 4px;")
        tfa_row.addWidget(self.code_edit, 1)

        self.submit_code_btn = QPushButton("Подтвердить")
        self.submit_code_btn.setObjectName("PrimaryButton")
        self.submit_code_btn.clicked.connect(self._on_submit_code)
        tfa_row.addWidget(self.submit_code_btn)

        self.resend_btn = QPushButton("Отправить снова")
        self.resend_btn.clicked.connect(self._on_resend_code)
        tfa_row.addWidget(self.resend_btn)
        tfa_layout.addLayout(tfa_row)

        root.addWidget(self.tfa_card)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        root.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        self.login_btn = QPushButton("Войти в Apple ID")
        self.login_btn.setObjectName("PrimaryButton")
        self.login_btn.clicked.connect(self._on_start_login)
        btn_row.addWidget(self.login_btn)
        root.addLayout(btn_row)

        self.password_edit.returnPressed.connect(self._on_start_login)
        self.code_edit.returnPressed.connect(self._on_submit_code)

    def _set_status(self, text: str, *, is_error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))
        color = "#f87171" if is_error else "#9ba4c2"
        self.status_label.setStyleSheet(f"color: {color}; font-size: 12px;")

    def _set_busy(self, busy: bool) -> None:
        self.email_edit.setEnabled(not busy)
        self.password_edit.setEnabled(not busy)
        self.overwrite_check.setEnabled(not busy)
        self.login_btn.setEnabled(not busy)
        self.progress_bar.setVisible(busy)

    def _on_start_login(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        email = self.email_edit.text().strip()
        password = self.password_edit.text()
        # Clear password widget immediately on submit regardless of validation outcome.
        self.password_edit.clear()

        if not email or "@" not in email:
            self._set_status("Укажите корректный email Apple ID.", is_error=True)
            return
        if not password:
            self._set_status("Введите пароль Apple ID.", is_error=True)
            return

        self.tfa_card.setVisible(False)
        self._set_busy(True)
        self._set_status("Подключение к серверам Apple…")

        worker = AppleLoginWorker(
            self._core_bin,
            self._session_dir,
            email,
            password,
            overwrite=self.overwrite_check.isChecked(),
            parent=self,
        )
        password = ""
        worker.log_line.connect(self.log_line)
        worker.progress.connect(self._on_progress)
        worker.need_2fa.connect(self.show_2fa_prompt)
        worker.completed.connect(self._on_completed)
        self._worker = worker
        worker.start()

    def _on_progress(self, _stage: str, pct: int, message: str) -> None:
        if pct >= 0:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(pct)
        else:
            self.progress_bar.setRange(0, 0)
        if message:
            self._set_status(message)

    def show_2fa_prompt(self, prompt: str, retry: bool, sms: bool, last_error: str) -> None:
        """Reveal and focus the inline 2FA prompt when `need_2fa` arrives."""
        self.tfa_card.setVisible(True)
        channel = "SMS" if sms else "доверенное устройство Apple"
        details = prompt or f"Введите 6-значный код подтверждения ({channel}):"
        if retry and last_error:
            details = f"Неверный код ({last_error}). Попробуйте ещё раз:"
        elif retry:
            details = "Неверный код подтверждения. Попробуйте ещё раз:"
        self.tfa_prompt_label.setText(details)
        self.code_edit.clear()
        self.code_edit.setEnabled(True)
        self.submit_code_btn.setEnabled(True)
        self.resend_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self._set_status(f"Ожидание кода подтверждения ({channel})…", is_error=retry)
        self.code_edit.setFocus()

    def _on_submit_code(self) -> None:
        code = self.code_edit.text().strip()
        self.code_edit.clear()
        if not code:
            self._set_status("Введите 6-значный код подтверждения.", is_error=True)
            return
        self.code_edit.setEnabled(False)
        self.submit_code_btn.setEnabled(False)
        self.resend_btn.setEnabled(False)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)
        self._set_status("Проверка кода 2FA…")
        if self._worker is not None:
            self._worker.submit_2fa_code(code)
        code = ""

    def _on_resend_code(self) -> None:
        self.code_edit.clear()
        self.code_edit.setEnabled(False)
        self.submit_code_btn.setEnabled(False)
        self.resend_btn.setEnabled(False)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)
        self._set_status("Запрашиваем новый код 2FA…")
        if self._worker is not None:
            self._worker.submit_2fa_code("resend")

    def _on_completed(self, ok: bool, message: str, email_masked: str) -> None:
        self._set_busy(False)
        self.tfa_card.setVisible(False)
        self.result_ok = ok
        self.result_message = message
        self.result_email_masked = email_masked
        if ok:
            self.accept()
        else:
            self._set_status(message or "Не удалось выполнить вход в Apple ID.", is_error=True)

    def reject(self) -> None:
        self.password_edit.clear()
        self.code_edit.clear()
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        super().reject()


class RenewUsbDialog(QDialog):
    """Compact confirmation dialog for `renew-usb` with auto-detected defaults."""

    def __init__(
        self,
        *,
        default_bundle: str = "",
        default_team: str = "",
        source_note: str = "",
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Продлить подпись по USB")
        self.setModal(True)
        self.setMinimumWidth(440)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        title = QLabel("Продление профиля подписи по USB")
        title.setObjectName("BrandTitle")
        root.addWidget(title)

        desc = QLabel(
            "Запросит свежий профиль у Apple, проверит совместимость с текущим "
            "профилем на iPhone и установит его через USB без переустановки приложения."
        )
        desc.setObjectName("MutedText")
        desc.setWordWrap(True)
        root.addWidget(desc)

        if source_note:
            hint = QLabel(source_note)
            hint.setObjectName("InfoBanner")
            hint.setWordWrap(True)
            root.addWidget(hint)

        card = QFrame()
        card.setObjectName("Card")
        form = QFormLayout(card)
        form.setContentsMargins(16, 14, 16, 14)
        form.setSpacing(10)

        self.bundle_edit = QLineEdit(default_bundle)
        self.bundle_edit.setPlaceholderText("com.SideStore.SideStore.XXXXXXXXXX")
        form.addRow("Bundle ID:", self.bundle_edit)

        self.team_edit = QLineEdit(default_team)
        self.team_edit.setPlaceholderText("Опционально (автоопределение с iPhone)")
        form.addRow("Team ID:", self.team_edit)

        root.addWidget(card)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #f87171; font-size: 12px;")
        self.error_label.setVisible(False)
        root.addWidget(self.error_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Отмена")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        ok_btn = QPushButton("Продлить подпись")
        ok_btn.setObjectName("PrimaryButton")
        ok_btn.clicked.connect(self._on_accept)
        btn_row.addWidget(ok_btn)
        root.addLayout(btn_row)

    def _on_accept(self) -> None:
        if not self.bundle_edit.text().strip():
            self.error_label.setText("Укажите Bundle ID приложения для продления.")
            self.error_label.setVisible(True)
            return
        self.accept()

    def bundle_id(self) -> str:
        return self.bundle_edit.text().strip()

    def team_id(self) -> str:
        return self.team_edit.text().strip()
