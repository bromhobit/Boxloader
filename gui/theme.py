"""Dopamine-inspired dark theme QSS stylesheet for PySide6."""

APP_QSS = """
QMainWindow, QDialog {
    background-color: #0b0e18;
    color: #f3f5fc;
    font-family: "Inter", "Noto Sans", "DejaVu Sans", sans-serif;
    font-size: 13px;
}

QWidget {
    color: #f3f5fc;
}

/* Top bar */
QFrame#TopBar {
    background-color: #111626;
    border-bottom: 1px solid #222a44;
}

QLabel#BrandEyebrow {
    color: #9b82ff;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
}

QLabel#BrandTitle {
    color: #ffffff;
    font-size: 17px;
    font-weight: 700;
}

QLabel#SectionHeading {
    color: #8892b4;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.8px;
}

QLabel#MutedText {
    color: #9ba4c2;
    font-size: 12px;
}

QLabel#InfoBanner {
    background-color: #141a30;
    border: 1px solid #283257;
    border-radius: 10px;
    padding: 10px 14px;
    color: #d6dcf2;
    font-size: 13px;
}

QLabel#TrollStoreNotice {
    background-color: rgba(245, 158, 11, 0.13);
    border: 1px solid rgba(245, 158, 11, 0.40);
    border-radius: 8px;
    padding: 8px 12px;
    color: #fbbf24;
    font-size: 12px;
}

/* Status pills */
QLabel#PillGreen {
    background-color: rgba(34, 197, 94, 0.16);
    border: 1px solid rgba(34, 197, 94, 0.40);
    border-radius: 11px;
    padding: 4px 10px;
    color: #4ade80;
    font-weight: 600;
    font-size: 12px;
}

QLabel#PillAmber {
    background-color: rgba(245, 158, 11, 0.16);
    border: 1px solid rgba(245, 158, 11, 0.40);
    border-radius: 11px;
    padding: 4px 10px;
    color: #fbbf24;
    font-weight: 600;
    font-size: 12px;
}

QLabel#PillRed {
    background-color: rgba(239, 68, 68, 0.16);
    border: 1px solid rgba(239, 68, 68, 0.42);
    border-radius: 11px;
    padding: 4px 10px;
    color: #f87171;
    font-weight: 600;
    font-size: 12px;
}

QLabel#PillNeutral {
    background-color: rgba(148, 163, 184, 0.14);
    border: 1px solid rgba(148, 163, 184, 0.30);
    border-radius: 11px;
    padding: 4px 10px;
    color: #cbd5e1;
    font-weight: 600;
    font-size: 12px;
}

/* Cards */
QFrame#Card {
    background-color: #131829;
    border: 1px solid #222a44;
    border-radius: 14px;
}

QFrame#LogDrawer {
    background-color: #0e1220;
    border-top: 1px solid #222a44;
}

/* Tabs */
QTabWidget::pane {
    border: none;
    background-color: #0b0e18;
    margin-top: 4px;
}

QTabBar::tab {
    background-color: #131829;
    color: #9aa3c2;
    border: 1px solid #222a44;
    border-radius: 10px;
    padding: 8px 16px;
    margin-right: 6px;
    font-weight: 600;
    font-size: 13px;
}

QTabBar::tab:selected {
    background-color: #6f47f5;
    border-color: #8463ff;
    color: #ffffff;
}

QTabBar::tab:hover:!selected {
    background-color: #1b223a;
    color: #e6ebfa;
}

/* Inputs */
QLineEdit, QComboBox {
    background-color: #0e1322;
    border: 1px solid #2a3354;
    border-radius: 9px;
    padding: 5px 10px;
    color: #f3f5fc;
    selection-background-color: #6f47f5;
    min-height: 22px;
}

QLineEdit:focus, QComboBox:focus {
    border-color: #8463ff;
}

QLineEdit:disabled, QComboBox:disabled {
    background-color: #111524;
    color: #6e7794;
}

QComboBox::drop-down {
    border: none;
    width: 24px;
}

QComboBox QAbstractItemView {
    background-color: #131829;
    border: 1px solid #2e385c;
    selection-background-color: #6f47f5;
    color: #f3f5fc;
    outline: none;
}

/* Buttons */
QPushButton {
    background-color: #1b2138;
    border: 1px solid #2d3759;
    border-radius: 9px;
    padding: 6px 13px;
    color: #ebeffb;
    font-weight: 600;
    min-height: 20px;
}

QPushButton:hover {
    background-color: #232b47;
    border-color: #3d4a76;
}

QPushButton:pressed {
    background-color: #161c30;
}

QPushButton:disabled {
    background-color: #141828;
    border-color: #1f253c;
    color: #5f6885;
}

QPushButton#PrimaryButton {
    background-color: #6f47f5;
    border: 1px solid #8463ff;
    color: #ffffff;
    font-size: 14px;
    font-weight: 700;
    padding: 11px 20px;
    border-radius: 11px;
}

QPushButton#PrimaryButton:hover {
    background-color: #7d58ff;
}

QPushButton#PrimaryButton:pressed {
    background-color: #5b37d6;
}

QPushButton#PrimaryButton:disabled {
    background-color: #3a2d6b;
    border-color: #46387c;
    color: #a9a2c9;
}

QPushButton#DangerButton {
    background-color: rgba(239, 68, 68, 0.16);
    border: 1px solid rgba(239, 68, 68, 0.48);
    color: #fca5a5;
}

QPushButton#DangerButton:hover {
    background-color: rgba(239, 68, 68, 0.28);
}

QPushButton#IpaChip {
    background-color: #161c31;
    border: 1px solid #2b3557;
    border-radius: 14px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
    color: #d8dff6;
}

QPushButton#IpaChip:hover {
    background-color: #202948;
    border-color: #7c5cff;
}

QPushButton#IpaChipSelected {
    background-color: rgba(111, 71, 245, 0.26);
    border: 1px solid #8c6eff;
    border-radius: 14px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 700;
    color: #ffffff;
}

QPushButton#IpaChipFeatured {
    background-color: rgba(45, 212, 191, 0.14);
    border: 1px solid rgba(45, 212, 191, 0.55);
    border-radius: 14px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 700;
    color: #5eead4;
}

QPushButton#IpaChipFeatured:hover {
    background-color: rgba(45, 212, 191, 0.24);
}

QPushButton#SideBoxSetupButton {
    background-color: rgba(45, 212, 191, 0.16);
    border: 1px solid rgba(45, 212, 191, 0.55);
    border-radius: 10px;
    padding: 7px 14px;
    color: #99f6e4;
    font-weight: 700;
}

QPushButton#SideBoxSetupButton:hover {
    background-color: rgba(45, 212, 191, 0.26);
    color: #ffffff;
}

QPushButton#SideBoxSetupButton:disabled {
    background-color: #141828;
    border-color: #1f253c;
    color: #5f6885;
}

QPushButton#IpaChipTrollStore {
    background-color: rgba(245, 158, 11, 0.12);
    border: 1px solid rgba(245, 158, 11, 0.42);
    border-radius: 14px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
    color: #fcd34d;
}

QPushButton#IpaChipTrollStore:hover {
    background-color: rgba(245, 158, 11, 0.22);
}

QPushButton#DrawerToggle {
    background-color: transparent;
    border: none;
    color: #9ba4c2;
    font-weight: 700;
    text-align: left;
    padding: 4px 6px;
}

QPushButton#DrawerToggle:hover {
    color: #ffffff;
}

/* Progress bar */
QProgressBar {
    background-color: #0d1220;
    border: 1px solid #242d4a;
    border-radius: 7px;
    height: 14px;
    text-align: center;
    color: #ffffff;
    font-size: 11px;
    font-weight: 600;
}

QProgressBar::chunk {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 #6f47f5,
        stop:1 #2dd4bf
    );
    border-radius: 6px;
}

/* Stage indicator chips */
QLabel#StageIdle {
    background-color: #111627;
    border: 1px solid #222a44;
    border-radius: 8px;
    padding: 4px 8px;
    color: #7c85a6;
    font-size: 11px;
    font-weight: 600;
}

QLabel#StageActive {
    background-color: rgba(111, 71, 245, 0.25);
    border: 1px solid #8c6eff;
    border-radius: 8px;
    padding: 4px 8px;
    color: #ffffff;
    font-size: 11px;
    font-weight: 700;
}

QLabel#StageDone {
    background-color: rgba(34, 197, 94, 0.16);
    border: 1px solid rgba(34, 197, 94, 0.45);
    border-radius: 8px;
    padding: 4px 8px;
    color: #4ade80;
    font-size: 11px;
    font-weight: 700;
}

/* Log text area */
QPlainTextEdit {
    background-color: #090c14;
    border: 1px solid #1f2740;
    border-radius: 10px;
    padding: 8px;
    color: #cdd5ee;
    font-family: "JetBrains Mono", "Fira Code", "DejaVu Sans Mono", monospace;
    font-size: 12px;
}

QCheckBox {
    spacing: 8px;
    color: #d6dcf2;
}

QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #39446b;
    background-color: #0e1322;
}

QCheckBox::indicator:checked {
    background-color: #6f47f5;
    border-color: #8c6eff;
}

QScrollArea {
    border: none;
    background-color: #0b0e18;
}

QScrollArea > QWidget > QWidget {
    background-color: #0b0e18;
}

QScrollBar:vertical {
    background: #0b0e18;
    width: 8px;
    margin: 2px 0;
}

QScrollBar::handle:vertical {
    background: #283152;
    border-radius: 4px;
    min-height: 24px;
}

QScrollBar::handle:vertical:hover {
    background: #3d4a7a;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

QScrollBar:horizontal {
    background: #0b0e18;
    height: 8px;
}

QScrollBar::handle:horizontal {
    background: #283152;
    border-radius: 4px;
    min-width: 24px;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
}
"""
