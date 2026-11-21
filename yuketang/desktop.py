"""Native Qt Widgets UI. No web server, web view, or browser dependency."""
from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QObject, Signal, QTimer, QSize
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QStackedWidget, QTabBar,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QProgressBar,
    QFrame, QDialog, QDialogButtonBox, QTabWidget, QFormLayout,
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox, QScrollArea,
    QListView, QStyledItemDelegate, QFileDialog,
)

from .config import Config
from .course import Course, Leaf, LEAF_TYPE_NAMES
from .desktop_backend import DesktopService, save_settings
from .login import LoginError
from .desktop_widgets import TaskSwitch, RunPanel
from .desktop_state import TaskStateStore, display_time, progress_caption

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SCHOOL_ID_HELP = ("学校 ID 获取方式\n在浏览器登录雨课堂，按 F12 → Application（应用程序）→ Cookies，"
                  "选择当前雨课堂站点，找到 university_id，复制它的值并填入上方。")


STYLE = """
QWidget {font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 14px; color: #203e38;}
QMainWindow, QDialog {background: #f7f9f6;}
QFrame#sidebar {background: #edf3ee; border-right: 1px solid #dfe8df;}
QLabel#brand {font-size: 23px; font-weight: 700;}
QLabel#eyebrow {font-size: 12px; color: #55796d; letter-spacing: 2px;}
QLabel#title {font-size: 28px; font-weight: 700;}
QLabel#muted {color: #728278; font-size: 12px;}
QLabel#badge {background: #e3efe8; color: #316b58; padding: 6px 12px; border-radius: 12px;}
QLabel#notice {background: #fff0d2; color: #805d27; padding: 9px 14px; border-radius: 10px;}
QPushButton {background: #fff; border: 1px solid #dce5dc; padding: 10px 17px; border-radius: 12px;}
QPushButton:hover {background: #e7efe8; border-color: #b6ccc0;}
QPushButton:pressed {background: #dce9df;}
QPushButton#primary {background: #26745c; color: white; border: none; font-weight: 600;}
QPushButton#primary:hover {background: #1c604b;}
QPushButton#stop {color: #9b5146; background: #fff6f1; border-color: #ecd9cf;}
QPushButton:disabled {color: #99a79e; background: #edf1ec; border-color: #e5eae3;}
QPushButton#primary:disabled {color: #93a897; background: #dde8dd;}
QPushButton#stop:disabled {color: #b1ada1; background: #f1f2eb; border-color: #e5eae3;}
QPushButton#iconButton {padding: 0; background: transparent; border: 1px solid #d9e3d7; border-radius: 10px;}
QPushButton#iconButton:hover {background: #e3ede3;}
QListWidget {background: transparent; border: none; outline: none;}
QListWidget::item {padding: 16px 12px; margin: 3px 0; border-radius: 12px;}
QListWidget::item:selected {background: #dbe9df; color: #205c48;}
QListWidget::item:hover {background: #e4ede5;}
QTabBar {background: transparent;}
QTabBar::tab {background: #edf2ec; padding: 10px 18px; margin-right: 8px; border-radius: 11px;}
QTabBar::tab:selected {background: #d8e8dd; color: #226449; font-weight: 600;}
QTreeWidget {background: white; border: 1px solid #e1e8df; border-radius: 14px; outline: none;}
QTreeWidget::item {padding: 11px 5px; border-bottom: 1px solid #f0f3ed;}
QTreeWidget::item:selected {background: #eaf3ec; color: #203e38;}
QHeaderView::section {background: #f1f5ef; border: none; padding: 12px 8px; color: #687c6d; font-size: 12px;}
QTextBrowser {background: #f0f5ef; border: 1px solid #e1e8df; border-radius: 14px; padding: 14px;}
QProgressBar {background: #eaf0e8; border: none; border-radius: 5px; text-align: center; min-height: 10px; max-height: 10px;}
QProgressBar::chunk {background: #61a786; border-radius: 5px;}
QFrame#footer {background: #eef4ed; border: 1px solid #dfe9dc; border-radius: 16px;}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {background: white; border: 1px solid #d9e3d7; border-radius: 10px; padding: 0 12px; min-height: 42px;}
QSpinBox, QDoubleSpinBox, QComboBox {padding: 0 34px 0 12px;}
QAbstractSpinBox > QLineEdit {border: none; border-radius: 0; background: transparent; padding: 0; min-height: 0;}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {border-color: #68a087;}
QComboBox::drop-down {subcontrol-origin: border; subcontrol-position: top right; width: 34px; border: none; background: transparent;}
QComboBox::down-arrow {image: url("__DOWN_ARROW__"); width: 12px; height: 12px; top: 3px;}
QComboBox::drop-down:hover {background: #edf4ed; border-top-right-radius: 10px; border-bottom-right-radius: 10px; margin: 1px;}
QComboBox QAbstractItemView {background: white; color: #203e38; border: 1px solid #d9e3d7; border-radius: 10px; padding: 5px; outline: none; selection-background-color: #e1eee4; selection-color: #246247;}
QComboBox QAbstractItemView::item {padding: 10px 12px; min-height: 22px; border-radius: 7px;}
QSpinBox::up-button, QDoubleSpinBox::up-button {subcontrol-origin: border; subcontrol-position: top right; width: 30px; border: none; margin-top: 1px; margin-right: 1px; border-top-right-radius: 9px; background: transparent;}
QSpinBox::down-button, QDoubleSpinBox::down-button {subcontrol-origin: border; subcontrol-position: bottom right; width: 30px; border: none; margin-bottom: 1px; margin-right: 1px; border-bottom-right-radius: 9px; background: transparent;}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {background: #e5efe5;}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {image: url("__UP_ARROW__"); width: 12px; height: 12px; top: -3px;}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {image: url("__DOWN_ARROW__"); width: 12px; height: 12px; top: 3px;}
QCheckBox {spacing: 10px; padding: 5px 0;}
QCheckBox::indicator {width: 18px; height: 18px; border-radius: 5px; border: 1px solid #bbcdbd; background: white;}
QCheckBox::indicator:checked {background: #398469; border-color: #398469; image: none;}
QTabWidget::pane {border: none; background: #f7f9f6; padding-top: 12px;}
QScrollArea {border: none; background: transparent;}
QWidget#settingsPage, QWidget#settingsAiPage, QWidget#settingsViewport {background: #f7f9f6; border: none;}
QLabel#help {color: #5c806c; border: 1px solid #bfd1c2; border-radius: 9px; font-size: 12px; background: #eef5ec; padding: 0;}
QToolTip {background: #f7fbf5; color: #365c46; border: 1px solid #cbdccb; padding: 10px; font-size: 12px;}
QScrollBar:vertical {width: 8px; background: transparent; margin: 4px;}
QScrollBar::handle:vertical {background: #c6d6c9; min-height: 30px; border-radius: 3px;}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {height: 0;}
QSplitter::handle {background: transparent; width: 14px;}
QWidget#runContainer, QScrollArea#runScroll {background: #f7f9f6; border: none;}
QFrame#taskCard {background: white; border: 1px solid #dde7d9; border-radius: 14px;}
QFrame#problemCard {background: #f2f7ef; border: none; border-radius: 10px;}
QLabel#taskState {color: #47745e; font-size: 12px;}
QLabel#taskTitle {font-size: 16px; font-weight: 600;}
QLabel#runStats {color: #47745e; background: #edf4e8; padding: 12px 16px; border-radius: 12px;}
"""
STYLE = STYLE.replace("__UP_ARROW__", (ASSETS / "up_arrow.svg").as_posix()).replace(
    "__DOWN_ARROW__", (ASSETS / "down_arrow.svg").as_posix())


def label(text, name=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.PlainText)
    if name:
        widget.setObjectName(name)
    return widget


def button(text, callback, name=None):
    widget = QPushButton(text)
    widget.setCursor(Qt.PointingHandCursor)
    if name:
        widget.setObjectName(name)
    widget.clicked.connect(callback)
    return widget


def icon_button(filename, tooltip, callback):
    widget = button("", callback, "iconButton")
    widget.setIcon(QIcon(str(ASSETS / filename)))
    widget.setIconSize(QSize(22, 22))
    widget.setFixedSize(44, 44)
    widget.setToolTip(tooltip)
    widget.setAccessibleName(tooltip)
    return widget


def app_icon():
    pixmap = QPixmap(128, 128)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor("#26745c"))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(0, 0, 128, 128, 30, 30)
    painter.setPen(QColor("#f3f8ec"))
    painter.setFont(QFont("Microsoft YaHei UI", 54, QFont.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "雨")
    painter.end()
    return QIcon(pixmap)


def configure_app(app):
    # Windows offscreen/render environments may not enumerate installed fonts.
    # Read the installed font in that case; no system fonts are bundled/distributed.
    if sys.platform == "win32" and not QFontDatabase.families():
        from pathlib import Path
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ("msyh.ttc", "msyhbd.ttc"):
            QFontDatabase.addApplicationFont(str(fonts / filename))
    app.setApplicationName("雨课堂")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)


class SettingsDialog(QDialog):
    def __init__(self, config, parent=None, demo=False, task_only=False):
        super().__init__(parent)
        self.config = config
        self.result_config = None
        self.demo = demo
        self.check_job = None
        self.check_cancel = threading.Event()
        self.check_closing = False
        self.check_bridge = Bridge(self)
        self.check_bridge.event.connect(self._on_check_event)
        self.setWindowTitle("设置")
        self.resize(620, 800)
        self.setMinimumSize(560, 650)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 22)
        layout.setSpacing(16)
        layout.addWidget(label("设置", "title"))
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.fields = {}
        ai, form = self._page("AI 助手")
        mode = QComboBox()
        mode.addItem("DeepSeek API", "deepseek")
        mode.addItem("Codex CLI", "codex")
        mode.setCurrentIndex(max(0, mode.findData(config.ai_mode)))
        mode.setFixedHeight(44)
        view = QListView(mode)
        view.setItemDelegate(QStyledItemDelegate(view))
        view.setFrameShape(QFrame.NoFrame)
        mode.setView(view)
        self.fields["ai_mode"] = mode
        self._row(form, "使用模式", mode)
        self.ai_pages = QStackedWidget()
        form.addRow(self.ai_pages)
        for kind in ("deepseek", "codex"):
            widget = QWidget()
            widget.setObjectName("settingsAiPage")
            sub = self._form(widget, (0, 8, 0, 0))
            self.ai_pages.addWidget(widget)
            if kind == "deepseek":
                self._edit(sub, "deepseek_base_url", "API 地址")
                self._edit(sub, "deepseek_model", "模型")
                self.key = QLineEdit()
                self.key.setFixedHeight(44)
                self.key.setEchoMode(QLineEdit.Password)
                self.key.setPlaceholderText("已保存 · 留空保留" if config.has_deepseek else "输入 API 密钥")
                self._row(sub, "API 密钥", self.key)
                self.clear_key = QCheckBox("清除已存密钥")
                self._row(sub, "", self.clear_key)
                self._number(sub, "deepseek_temperature", "温度", 0, 2, True)
                self._number(sub, "deepseek_timeout", "超时（秒）", 1, 3600)
            else:
                self._edit(sub, "codex_cli", "程序路径", browse=True)
                self._edit(sub, "codex_model", "模型", "留空使用 CLI 默认模型")
                self._number(sub, "codex_timeout", "超时（秒）", 1, 3600)
                sub.addRow(label("使用本机 Codex 登录状态", "muted"))
        mode.currentIndexChanged.connect(self.ai_pages.setCurrentIndex)
        self.ai_pages.setCurrentIndex(mode.currentIndex())
        check = QWidget()
        check_box = QHBoxLayout(check)
        check_box.setContentsMargins(0, 0, 0, 0)
        self.check_btn = button("检查连接", self.check_connection)
        self.check_status = label("", "muted")
        self.check_status.setWordWrap(True)
        check_box.addWidget(self.check_btn)
        check_box.addWidget(self.check_status, 1)
        form.addRow(check)
        _, form = self._page("学习任务")
        form.addRow(label("处理任务", "muted"))
        choices = QWidget()
        choices_box = QHBoxLayout(choices)
        choices_box.setContentsMargins(0, 0, 0, 0)
        choices_box.setSpacing(12)
        for key, name in (("auto_video", "视频"), ("auto_richtext", "图文"), ("auto_discussion", "讨论"),
                          ("auto_homework", "作业"), ("auto_exam", "考试")):
            choice = TaskSwitch(name)
            choice.setChecked(getattr(config, key))
            self.fields[key] = choice
            choices_box.addWidget(choice)
        choices_box.addStretch()
        form.addRow(choices)
        self._number(form, "video_speed", "视频速度", 0.25, 16, True)
        self._number(form, "max_concurrent_videos", "视频并发", 1, 8)
        self._number(form, "heartbeat_interval", "心跳间隔（秒）", 5, 300)
        self._check(form, "skip_completed", "跳过服务端已完成的视频")
        self._number(form, "richtext_stay_seconds", "阅读停留（秒）", 0, 600)
        _, form = self._page("连接")
        self._edit(form, "domain", "雨课堂站点")
        self._edit(form, "university_id", "学校 ID", "手动填写 university_id")
        self._number(form, "max_retries", "请求重试次数", 1, 10)
        self.school_id_help = label(SCHOOL_ID_HELP, "muted")
        self.school_id_help.setWordWrap(True)
        form.addRow(self.school_id_help)
        self.error = label("演示模式：设置仅在本次预览中生效" if demo else "", "muted")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        controls = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons = controls
        controls.button(QDialogButtonBox.Save).setText("保存")
        controls.button(QDialogButtonBox.Save).setObjectName("primary")
        controls.button(QDialogButtonBox.Cancel).setText("取消")
        controls.accepted.connect(self.save)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)
        if task_only:
            self.tabs.setTabEnabled(0, False)
            self.tabs.setTabEnabled(2, False)
            self.tabs.setCurrentIndex(1)
            self.error.setText("修改将在下次开始时生效")

    def _page(self, title):
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.viewport().setObjectName("settingsViewport")
        scroll.setWidgetResizable(True)
        widget = QWidget()
        widget.setObjectName("settingsPage")
        form = self._form(widget, (0, 18, 0, 18))
        scroll.setWidget(widget)
        self.tabs.addTab(scroll, title)
        return widget, form

    @staticmethod
    def _form(widget, margins):
        form = QFormLayout(widget)
        form.setContentsMargins(*margins)
        form.setVerticalSpacing(14)
        form.setHorizontalSpacing(16)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignTop)
        form.setRowWrapPolicy(QFormLayout.DontWrapRows)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        return form

    def _row(self, form, title, widget):
        caption = label(title)
        caption.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        caption.setFixedWidth(122)
        caption.setFixedHeight(44)
        form.addRow(caption, widget)

    def _edit(self, form, key, title, placeholder="", browse=False):
        widget = QLineEdit(str(getattr(self.config, key)))
        widget.setFixedHeight(44)
        widget.setPlaceholderText(placeholder)
        self.fields[key] = widget
        if browse:
            group = QWidget()
            box = QHBoxLayout(group)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(8)
            box.addWidget(widget, 1)
            self.browse_btn = icon_button("folder.svg", "选择 Codex 程序", self.browse_codex)
            box.addWidget(self.browse_btn)
            self._row(form, title, group)
        else:
            self._row(form, title, widget)

    def _number(self, form, key, title, low, high, decimal=False):
        widget = QDoubleSpinBox() if decimal else QSpinBox()
        widget.setFixedHeight(44)
        widget.lineEdit().setTextMargins(12, 0, 0, 0)
        widget.lineEdit().setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        widget.setRange(low, high)
        if decimal:
            widget.setSingleStep(0.25 if key == "video_speed" else 0.1)
        widget.setValue(getattr(self.config, key))
        self.fields[key] = widget
        self._row(form, title, widget)

    def _check(self, form, key, title):
        widget = QCheckBox(title)
        widget.setChecked(getattr(self.config, key))
        self.fields[key] = widget
        self._row(form, "", widget)

    def values(self):
        values = {}
        for key, widget in self.fields.items():
            if isinstance(widget, QComboBox):
                value = widget.currentData()
            elif isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                value = widget.value()
            else:
                value = widget.text()
            values[key] = value
        return values

    def browse_codex(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 Codex 程序", "", "程序 (*.exe *.cmd *.bat *.ps1);;所有文件 (*)")
        if path:
            self.fields["codex_cli"].setText(path)

    def check_connection(self):
        if self.demo:
            self.check_status.setText("演示模式 · 未连接 AI")
            return
        if self.check_job and self.check_job.is_alive():
            self.check_cancel.set()
            self.check_btn.setEnabled(False)
            self.check_status.setText("正在取消…")
            return
        from dataclasses import replace
        candidate = replace(self.config, **self.values())
        candidate.deepseek_api_key = ("" if self.clear_key.isChecked() else self.key.text().strip() or self.config.deepseek_api_key)
        if candidate.ai_problems():
            self.check_status.setText("请补全所选模式的连接配置")
            return
        self.check_cancel.clear()
        self.check_btn.setText("取消检查")
        self.check_status.setText("检查中…")
        self.buttons.button(QDialogButtonBox.Save).setEnabled(False)
        def work():
            try:
                from .desktop_runtime import run_isolated
                run_isolated("ai_check", candidate, self.check_cancel, self.check_bridge.event.emit)
            except Exception:
                self.check_bridge.event.emit("error", "连接检查失败，请检查配置")
            finally:
                self.check_bridge.event.emit("check_idle", None)
        self.check_job = threading.Thread(target=work, name="yuketang-ai-check", daemon=False)
        self.check_job.start()

    def _on_check_event(self, kind, value):
        if kind == "ai_check":
            self.check_status.setText(value[1])
        elif kind == "error":
            self.check_status.setText(value)
        elif kind == "cancelled":
            self.check_status.setText("已取消")
        elif kind == "log_error":
            self.check_status.setText("AI 回复日志写入失败，请检查 data/logs 目录是否可写")
        elif kind == "check_idle":
            if self.check_job and self.check_job.is_alive():
                QTimer.singleShot(20, lambda: self._on_check_event("check_idle", None))
                return
            self.check_btn.setText("检查连接")
            self.check_btn.setEnabled(True)
            self.buttons.button(QDialogButtonBox.Save).setEnabled(True)
            if self.check_closing:
                super().reject()

    def reject(self):
        if self.check_job and self.check_job.is_alive():
            self.check_closing = True
            self.check_cancel.set()
            self.check_status.setText("正在取消…")
            return
        super().reject()

    def closeEvent(self, event):
        if self.check_job and self.check_job.is_alive():
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)

    def save(self):
        values = self.values()
        try:
            if self.demo:
                from dataclasses import replace
                self.result_config = replace(self.config, **values)
            else:
                self.result_config = save_settings(self.config, values, self.config.project_root / ".env",
                                                   new_key=self.key.text(), clear_key=self.clear_key.isChecked())
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        except OSError:
            self.error.setText("保存失败，请检查软件数据目录是否可写")
            return
        self.key.clear()
        self.accept()


class Bridge(QObject):
    event = Signal(str, object)


class LogoutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("退出登录")
        self.setMinimumWidth(360)
        box = QVBoxLayout(self)
        box.setContentsMargins(26, 24, 26, 24)
        box.setSpacing(20)
        box.addWidget(label("退出登录", "taskTitle"))
        box.addWidget(label("退出后需要重新扫码登录。"))
        controls = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        controls.button(QDialogButtonBox.Ok).setText("退出登录")
        controls.button(QDialogButtonBox.Ok).setObjectName("primary")
        controls.button(QDialogButtonBox.Cancel).setText("取消")
        controls.accepted.connect(self.accept)
        controls.rejected.connect(self.reject)
        box.addWidget(controls)


class MainWindow(QMainWindow):
    def __init__(self, demo=False, service=None):
        super().__init__()
        self.demo = demo
        self.bridge = Bridge(self)
        self.bridge.event.connect(self.on_event)
        self.service = service or DesktopService(Config() if demo else None)
        self.service.emit = self.bridge.event.emit
        self.job = None
        self.closing = False
        self.states = {}
        self.task_state_store = TaskStateStore()
        self.state_save_timer = QTimer(self)
        self.state_save_timer.setSingleShot(True)
        self.state_save_timer.setInterval(300)
        self.state_save_timer.timeout.connect(self.flush_task_states)
        self.items = {}
        self.progress_widgets = {}
        self.tab_types = []
        self.active_ids = set()
        self.active_types = set()
        self.setWindowTitle("雨课堂" + ("  [演示数据]" if demo else ""))
        self.setWindowIcon(app_icon())
        self.resize(1250, 820)
        self.setMinimumSize(1060, 700)
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(250)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(22, 30, 22, 24)
        side.setSpacing(20)
        logo = QHBoxLayout()
        mark = QLabel()
        mark.setPixmap(app_icon().pixmap(42, 42))
        logo.addWidget(mark)
        logo.addWidget(label("雨课堂", "brand"))
        logo.addStretch()
        side.addLayout(logo)
        side.addWidget(label("学习空间", "eyebrow"))
        self.course_list = QListWidget()
        self.course_list.setWordWrap(False)
        self.course_list.setUniformItemSizes(True)
        self.course_list.setTextElideMode(Qt.ElideRight)
        self.course_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.course_list.itemClicked.connect(self.choose_course)
        side.addWidget(self.course_list, 1)
        self.refresh_btn = button("刷新课程", lambda: self.launch("加载课程", self.service.login))
        side.addWidget(self.refresh_btn)
        self.settings_btn = button("设置", self.open_settings)
        side.addWidget(self.settings_btn)
        self.account_btn = button("退出登录", self.logout)
        side.addWidget(self.account_btn)
        outer.addWidget(sidebar)
        main = QVBoxLayout()
        main.setContentsMargins(34, 28, 34, 28)
        main.setSpacing(18)
        outer.addLayout(main, 1)
        top = QHBoxLayout()
        top.addWidget(label("YUKETANG  /  学习", "eyebrow"))
        top.addStretch()
        main.addLayout(top)
        if demo:
            main.addWidget(label("演示数据 · 所有操作均为本地模拟", "notice"))
        self.pages = QStackedWidget()
        main.addWidget(self.pages, 1)
        self._login_page()
        self._course_page()
        self.footer = QFrame()
        self.footer.setObjectName("footer")
        bar = QHBoxLayout(self.footer)
        bar.setContentsMargins(20, 16, 16, 16)
        status = QVBoxLayout()
        self.status = label("准备就绪")
        self.status.setWordWrap(True)
        self.enabled = label("", "muted")
        status.addWidget(self.status)
        status.addWidget(self.enabled)
        bar.addLayout(status, 1)
        self.stop_btn = button("停止", self.stop, "stop")
        self.stop_btn.setEnabled(False)
        self.start_btn = button("开始本课程", self.start, "primary")
        self.start_btn.setEnabled(False)
        self.task_settings_btn = icon_button("settings.svg", "选择处理任务", lambda: self.open_settings(1))
        bar.addWidget(self.task_settings_btn)
        bar.addWidget(self.stop_btn)
        bar.addWidget(self.start_btn)
        main.addWidget(self.footer)
        self.update_enabled()
        self.refresh_btn.setEnabled(False)
        self.account_btn.setEnabled(False)
        self.demo_timer = QTimer(self)
        self.demo_timer.setInterval(350)
        self.demo_timer.timeout.connect(self.demo_tick)
        self.demo_step = 0
        if demo:
            self.load_demo()
        elif self.service.config.has_credentials and self.service.config.effective_university_id:
            QTimer.singleShot(0, self.restore_login)

    def restore_login(self):
        if (not self.demo and not self.closing and self.service.session is None
                and self.service.config.has_credentials and self.service.config.effective_university_id):
            self.launch("自动登录", self.service.login)

    def update_login_action(self):
        self.login_btn.setText("微信扫码登录" if self.service.config.effective_university_id else "配置学校ID")

    def login_action(self):
        if not self.service.config.effective_university_id:
            self.open_settings(2)
        else:
            self.launch("扫码登录", lambda: self.service.login(True))

    def _login_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(20)
        self.login_hint = label("", "muted")
        self.login_hint.hide()
        self.qr = label("雨")
        self.qr.setAlignment(Qt.AlignCenter)
        self.qr.setFixedSize(238, 238)
        self.qr.setStyleSheet("background: white; border: 1px solid #dfe8da; border-radius: 22px; font-size: 70px; color: #b1cabb;")
        layout.addWidget(self.qr, alignment=Qt.AlignCenter)
        layout.addWidget(self.login_hint, alignment=Qt.AlignCenter)
        self.login_btn = button("", self.login_action, "primary")
        self.update_login_action()
        layout.addWidget(self.login_btn, alignment=Qt.AlignCenter)
        self.pages.addWidget(page)

    def _course_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(18)
        self.course_title = label("我的课程", "title")
        self.course_title.setWordWrap(True)
        heading = QHBoxLayout()
        heading.addWidget(self.course_title, 1)
        self.view_btn = button("课程结构", self.toggle_run_view)
        self.view_btn.hide()
        heading.addWidget(self.view_btn)
        layout.addLayout(heading)
        self.summary = label("选择左侧课程，查看学习任务", "muted")
        layout.addWidget(self.summary)
        self.task_views = QStackedWidget()
        structure = QWidget()
        structure_box = QVBoxLayout(structure)
        structure_box.setContentsMargins(0, 0, 0, 0)
        structure_box.setSpacing(18)
        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.currentChanged.connect(self.render_tasks)
        structure_box.addWidget(self.tabs)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["学习任务", "状态", "播放位置"])
        self.tree.setColumnCount(3)
        self.tree.setIndentation(16)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.Fixed)
        self.tree.header().setSectionResizeMode(2, QHeaderView.Fixed)
        self.tree.setColumnWidth(1, 120)
        self.tree.setColumnWidth(2, 220)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        structure_box.addWidget(self.tree, 1)
        self.task_views.addWidget(structure)
        self.run_panel = RunPanel()
        self.task_views.addWidget(self.run_panel)
        layout.addWidget(self.task_views, 1)
        self.pages.addWidget(page)

    def update_enabled(self):
        cfg = self.service.config
        names = []
        for key, title, kind in (("auto_video", "视频", 0), ("auto_richtext", "图文", 3),
                                 ("auto_discussion", "讨论", 4), ("auto_homework", "作业", 6), ("auto_exam", "考试", 5)):
            enabled = kind in self.active_types if self.service.busy and self.active_ids else getattr(cfg, key)
            if enabled:
                names.append(title)
        self.enabled.setText(" · ".join(names))

    def open_settings(self, tab=0):
        dialog = SettingsDialog(self.service.config, self, self.demo, task_only=self.service.busy)
        dialog.tabs.setCurrentIndex(tab)
        if tab == 2:
            dialog.fields["university_id"].setFocus()
        if dialog.exec() == QDialog.Accepted:
            old = self.service.config
            self.service.config = dialog.result_config
            if self.service.session:
                self.service.session.max_retries = dialog.result_config.max_retries
            if (old.domain, old.effective_university_id) != (dialog.result_config.domain, dialog.result_config.effective_university_id):
                self.close_task_states()
                if self.service.session:
                    self.service.session.session.close()
                self.service.session = None
                self.service.course, self.service.leaves, self.service.courses = None, [], []
                self.course_list.clear()
                self.states.clear()
                self.active_ids.clear()
                self.active_types.clear()
                self.run_panel.reset()
                self.view_btn.hide()
                self.items, self.progress_widgets = {}, {}
                self.tree.clear()
                self.pages.setCurrentIndex(0)
                self.start_btn.setEnabled(False)
                self.refresh_btn.setEnabled(False)
                self.account_btn.setEnabled(False)
            self.update_login_action()
            self.update_enabled()
            self.start_btn.setEnabled(not self.service.busy and bool(self.service.selected_leaves()))
            if not self.service.busy:
                self.status.setText("设置已保存" if not self.demo else "演示设置已更新")

    def logout(self):
        if self.service.busy:
            return
        if LogoutDialog(self).exec() != QDialog.Accepted:
            return
        self.close_task_states()
        try:
            self.service.logout(persist=not self.demo)
        except OSError:
            self.status.setText("退出失败，请检查配置文件是否可写")
            return
        self.course_list.clear()
        self.states.clear()
        self.active_ids.clear()
        self.active_types.clear()
        self.run_panel.reset()
        self.tree.clear()
        self.items, self.progress_widgets = {}, {}
        self.tab_types = []
        self.pages.setCurrentIndex(0)
        self.view_btn.hide()
        self.start_btn.setEnabled(False)
        self.account_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.update_login_action()
        self.qr.clear()
        self.qr.setText("雨")
        self.login_hint.clear()
        self.login_hint.hide()
        self.status.setText("已退出登录")

    def toggle_run_view(self):
        showing_run = self.task_views.currentIndex() == 1
        self.task_views.setCurrentIndex(0 if showing_run else 1)
        self.view_btn.setText("运行记录" if showing_run else "课程结构")

    def launch(self, title, operation):
        if self.demo or not self.service.acquire():
            return
        self.set_busy(True)
        self.status.setText(title + "中…")
        def work():
            try:
                operation()
            except (ValueError, LoginError) as exc:
                self.bridge.event.emit("error", str(exc))
            except Exception:
                # Never surface server responses, cookies, keys or Codex stderr.
                self.bridge.event.emit("error", title + "失败，请检查连接与设置后重试")
            finally:
                self.bridge.event.emit("idle", None)
        self.job = threading.Thread(target=work, name="yuketang-desktop", daemon=False)
        self.job.start()

    def set_busy(self, busy):
        for widget in (self.settings_btn, self.course_list, self.login_btn):
            widget.setEnabled(not busy)
        logged_in = self.service.session is not None or (self.demo and bool(self.service.courses))
        self.refresh_btn.setEnabled(not busy and logged_in)
        self.account_btn.setEnabled(not busy and logged_in)
        self.start_btn.setEnabled(not busy and bool(self.service.selected_leaves()))
        self.stop_btn.setEnabled(busy)
        self.update_enabled()

    def choose_course(self, item):
        course = item.data(Qt.UserRole)
        if self.demo:
            self.on_event("leaves", (course, self.demo_leaves[course.classroom_id]))
        else:
            self.launch("加载章节", lambda: self.service.open_course(course))

    def flush_task_states(self):
        self.state_save_timer.stop()
        if not self.task_state_store.flush():
            self.status.setText("本地状态记录保存失败，请检查 data/task-state 目录是否可写")

    def remember_task_state(self, leaf_id, values):
        if not self.demo and self.task_state_store.remember(leaf_id, values) and not self.state_save_timer.isActive():
            self.state_save_timer.start()

    def close_task_states(self):
        self.flush_task_states()
        self.task_state_store.close()

    def on_event(self, kind, value):
        if kind == "qr":
            import qrcode
            buffer = io.BytesIO()
            qrcode.make(value).save(buffer, format="PNG")
            pixmap = QPixmap()
            pixmap.loadFromData(buffer.getvalue())
            self.qr.setPixmap(pixmap.scaled(230, 230, Qt.KeepAspectRatio, Qt.FastTransformation))
            self.pages.setCurrentIndex(0)
            self.login_hint.setText("请使用微信扫码")
            self.login_hint.show()
        elif kind == "courses":
            self.close_task_states()
            self.course_list.clear()
            self.states.clear()
            self.active_ids.clear()
            self.active_types.clear()
            self.items, self.progress_widgets = {}, {}
            self.tab_types = []
            self.service.courses = value
            self.service.course, self.service.leaves = None, []
            for course in value:
                item = QListWidgetItem(course.course_name)
                item.setData(Qt.UserRole, course)
                item.setToolTip(course.course_name)
                item.setSizeHint(QSize(0, 62))
                self.course_list.addItem(item)
            self.pages.setCurrentIndex(1)
            self.course_title.setText("我的课程")
            self.summary.setText(f"{len(value)} 门课程 · 选择课程查看章节" if value else "暂无课程，可刷新重试")
            self.tabs.blockSignals(True)
            while self.tabs.count():
                self.tabs.removeTab(0)
            self.tabs.blockSignals(False)
            self.tree.clear()
            self.run_panel.reset()
            self.task_views.setCurrentIndex(0)
            self.view_btn.hide()
            self.status.setText("课程已更新")
        elif kind == "leaves":
            course, leaves = value
            self.service.course, self.service.leaves = course, leaves
            self.state_save_timer.stop()
            self.states = ({} if self.demo else self.task_state_store.open_course(
                self.service.config, self.service.session, course, leaves))
            self.active_ids.clear()
            self.active_types.clear()
            self.run_panel.reset()
            self.task_views.setCurrentIndex(0)
            self.view_btn.hide()
            self.pages.setCurrentIndex(1)
            self.course_title.setText(course.course_name)
            self.summary.setText(f"{len(leaves)} 项任务" if leaves else "暂无学习任务")
            self.tabs.blockSignals(True)
            while self.tabs.count():
                self.tabs.removeTab(0)
            self.tab_types = sorted({leaf.leaf_type for leaf in leaves})
            for leaf_type in self.tab_types:
                count = sum(leaf.leaf_type == leaf_type for leaf in leaves)
                self.tabs.addTab(f"{LEAF_TYPE_NAMES.get(leaf_type, '其他')}  {count}")
            self.tabs.blockSignals(False)
            self.render_tasks()
            self.start_btn.setEnabled(bool(self.service.selected_leaves()) and not self.service.busy)
            self.status.setText("准备就绪")
        elif kind == "task":
            leaf_id, values = value
            state = self.states.setdefault(leaf_id, {})
            values = dict(values)
            problem_ids = ()
            if "problem" in values:
                problem = values.pop("problem")
                state.setdefault("problems", {}).setdefault(problem["id"], {}).update(problem)
                problem_ids = (problem["id"],)
            stats_changed = any(key in values and values[key] != state.get(key) for key in ("state", "outcome"))
            state.update(values)
            self.remember_task_state(leaf_id, values)
            state["updated"] = datetime.now().strftime("%H:%M:%S")
            self.refresh_task(leaf_id)
            self.run_panel.update_task(leaf_id, state, problem_ids=problem_ids)
            if stats_changed:
                self.run_panel.update_stats(self.states)
        elif kind == "run_done":
            self.status.setText(f"本轮已结束 · 处理 {value} 项任务，请查看条目状态")
            self.flush_task_states()
        elif kind == "cancelled":
            self.mark_cancelled()
            self.flush_task_states()
        elif kind == "log_error":
            self.status.setText("AI 回复日志写入失败，请检查 data/logs 目录是否可写")
        elif kind == "error":
            self.status.setText(value)
            for leaf_id in self.active_ids:
                state = self.states.get(leaf_id, {})
                if state.get("state") in {"等待中", "处理中"}:
                    attempted = state.get("state") == "处理中"
                    state.update(state="未完成", stage="", outcome="unprocessed")
                    if attempted:
                        self.remember_task_state(leaf_id, {"state": "未完成", "outcome": "unprocessed"})
                    self.refresh_task(leaf_id)
                    if leaf_id in self.run_panel.cards:
                        self.run_panel.update_task(leaf_id, state)
            self.run_panel.update_stats(self.states)
        elif kind == "idle":
            # Joining happens after the worker returns; keep cancellation pending until then.
            if self.job and self.job.is_alive():
                QTimer.singleShot(20, lambda: self.on_event("idle", None))
                return
            cancelled = self.service.cancel.is_set()
            self.service.release()
            self.flush_task_states()
            self.set_busy(False)
            self.qr.clear()
            self.qr.setText("雨")
            self.login_hint.clear()
            self.login_hint.hide()
            if cancelled:
                self.mark_cancelled()
            if self.closing:
                self.close()

    def render_tasks(self, *_):
        self.tree.clear()
        self.items, self.progress_widgets = {}, {}
        index = self.tabs.currentIndex()
        if index < 0 or index >= len(self.tab_types):
            return
        kind = self.tab_types[index]
        self.tree.setColumnHidden(2, kind != 0)
        chapters = {}
        for leaf in self.service.leaves:
            if leaf.leaf_type != kind:
                continue
            chapter = leaf.chapter_name or "未命名章节"
            chapter_key = leaf.chapter_id or chapter
            if chapter_key not in chapters:
                parent = QTreeWidgetItem([chapter])
                font = parent.font(0)
                font.setBold(True)
                parent.setFont(0, font)
                parent.setFlags(Qt.ItemIsEnabled)
                self.tree.addTopLevelItem(parent)
                parent.setExpanded(True)
                chapters[chapter_key] = parent
            item = QTreeWidgetItem(chapters[chapter_key], [leaf.name, "未知", "—"])
            item.setData(0, Qt.UserRole, leaf.leaf_id)
            item.setToolTip(0, leaf.name)
            self.items[leaf.leaf_id] = item
            self.refresh_task(leaf.leaf_id)

    def refresh_task(self, leaf_id):
        item = self.items.get(leaf_id)
        if item is None:
            return
        state = self.states.get(leaf_id, {})
        item.setText(1, state.get("state", "未知"))
        history = (f"本地记录：{state.get('cached_state', state.get('state', ''))} · "
                   f"{display_time(state['cache_checked_at'])}；本次尚未核实") if state.get("cache_checked_at") else ""
        item.setToolTip(1, "\n".join(filter(None, (state.get("state", "未知"), state.get("reason"), history))))
        item.setToolTip(2, "本地播放位置；" + progress_caption(state))
        if "position" not in state and state.get("cached_server_rate") is not None:
            item.setText(2, f"上次完成率 {state['cached_server_rate']:.1%}")
        if "position" in state and state.get("duration", 0) > 0:
            item.setText(2, "")
            if leaf_id not in self.progress_widgets:
                widget = QWidget()
                widget.setMinimumHeight(48)
                box = QVBoxLayout(widget)
                box.setContentsMargins(8, 5, 8, 5)
                box.setSpacing(4)
                text = label("", "muted")
                text.setMinimumHeight(20)
                progress = QProgressBar()
                progress.setRange(0, 1000)
                progress.setTextVisible(False)
                box.addWidget(text)
                box.addWidget(progress)
                self.progress_widgets[leaf_id] = (text, progress)
                for column in range(3):
                    item.setSizeHint(column, QSize(0, 64))
                self.tree.setItemWidget(item, 2, widget)
                self.tree.doItemsLayout()
            text, progress = self.progress_widgets[leaf_id]
            position, duration = state["position"], state["duration"]
            text.setText(f"{_clock(position)} / {_clock(duration)}")
            progress.setValue(min(1000, int(position / duration * 1000)))
            item.setSizeHint(2, QSize(220, 64))

    def start(self):
        if self.service.busy:
            return
        selected = self.service.selected_leaves()
        if not selected:
            self.status.setText("请先选择需要处理的任务类型")
            return
        self.active_ids = {leaf.leaf_id for leaf in selected}
        self.active_types = {leaf.leaf_type for leaf in selected}
        self.run_panel.reset(selected)
        for leaf_id in self.active_ids:
            # Keep historical measurements visibly dated, outside this run's counters.
            cached = {key: value for key, value in self.states.get(leaf_id, {}).items()
                      if key.startswith(("cache_", "cached_"))}
            self.states[leaf_id] = dict(cached, state="等待中")
        self.render_tasks()
        self.run_panel.update_stats(self.states)
        self.task_views.setCurrentIndex(1)
        self.view_btn.setText("课程结构")
        self.view_btn.show()
        if self.demo:
            self.service.acquire()
            self.demo_step = 0
            self.set_busy(True)
            self.status.setText("演示运行中…")
            self.demo_timer.start()
        else:
            self.launch("处理任务", self.service.run_course_isolated)

    def stop(self):
        self.service.stop()
        self.stop_btn.setEnabled(False)
        self.status.setText("正在停止任务…")
        if self.demo:
            self.demo_timer.stop()
            self.service.release()
            self.mark_cancelled()
            self.set_busy(False)

    def mark_cancelled(self):
        for leaf_id in self.active_ids:
            state = self.states[leaf_id]
            if state.get("state") in {"等待中", "处理中", "演示处理中", "已停止"}:
                attempted = state.get("state") != "等待中"
                state["state"] = "已停止"
                state["stage"] = ""
                state["outcome"] = "stopped"
                if attempted:
                    self.remember_task_state(leaf_id, {"state": "已停止", "outcome": "stopped"})
                for problem in state.get("problems", {}).values():
                    if problem["state"] in {"生成答案中", "提交中"}:
                        problem["state"] = "已停止 · 提交结果待核实" if problem["state"] == "提交中" else "已停止"
                self.refresh_task(leaf_id)
                # Keep untouched queued tasks out of the processing history.
                if leaf_id in self.run_panel.cards:
                    self.run_panel.update_task(leaf_id, state)
        self.run_panel.update_stats(self.states)
        self.status.setText("已停止")

    def closeEvent(self, event):
        self.closing = True
        if self.service.busy:
            self.stop()
            event.ignore()
            if self.demo:
                QTimer.singleShot(0, self.close)
            return
        if self.service.session:
            self.service.session.session.close()
        self.close_task_states()
        event.accept()

    def load_demo(self):
        courses = [Course("设计思维与创新", 101, "", 1, 1), Course("大学英语 · 综合教程", 102, "", 2, 2),
                   Course("数据科学导论", 103, "", 3, 3)]
        self.service.courses = courses
        self.demo_leaves = {}
        for course in courses:
            self.demo_leaves[course.classroom_id] = [
                Leaf(1, "从观察到洞察", 0, "第一章  发现问题"),
                Leaf(2, "理解用户的真实需求", 0, "第一章  发现问题"),
                Leaf(3, "让想法成为可能", 0, "第二章  探索与实践"),
                Leaf(4, "课前阅读：观察的力量", 3, "第一章  发现问题"),
                Leaf(5, "分享你身边的一次设计发现", 4, "第一章  发现问题"),
                Leaf(6, "第一章 · 课后练习", 6, "第一章  发现问题"),
                Leaf(7, "阶段测验", 5, "第二章  探索与实践", raw={"is_locked": True}),
            ]
        self.on_event("courses", courses)
        self.course_list.setCurrentRow(0)
        self.choose_course(self.course_list.item(0))
        self.refresh_btn.hide()
        self.account_btn.show()
        self.account_btn.setEnabled(True)
        self.start_btn.setEnabled(True)

    def demo_tick(self):
        self.demo_step += 1
        step = self.demo_step
        for leaf in self.service.leaves:
            if leaf.leaf_id not in self.active_ids:
                continue
            values = {"state": "演示处理中"}
            if leaf.leaf_type == 0:
                values.update(position=min(step * 24, 300), duration=300, server_rate=0.32, server_completed=False)
            elif leaf.leaf_type == 3:
                values.update(content="观察日常生活中的真实行为，从中发现值得解决的问题。", stage="阅读中")
            elif leaf.leaf_type == 4:
                values.update(content="分享你身边的一次设计发现", answer="通过观察，发现用户行为与原先预期的差异。", answer_state="演示已生成")
            else:
                values["problem"] = {"id": min(step, 12), "type": "SingleChoice",
                    "body": "设计观察首先关注什么？", "options": ["A. 用户的真实行为", "B. 预设的结论"],
                    "state": "演示已作答", "answer": "A"}
            self.on_event("task", (leaf.leaf_id, values))
        if step >= 13:
            self.demo_timer.stop()
            self.service.release()
            for leaf_id in self.active_ids:
                self.on_event("task", (leaf_id, {"state": "演示已结束", "stage": "", "outcome": "unprocessed"}))
            self.status.setText("演示已结束 · 未连接雨课堂")
            self.set_busy(False)


def _clock(seconds):
    value = max(0, int(seconds))
    return f"{value // 60:02d}:{value % 60:02d}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="雨课堂")
    parser.add_argument("--demo", action="store_true", help="本地演示，不读取凭证，不连接平台或 AI")
    parser.add_argument("--smoke-test", action="store_true", help="离线检查打包后的窗口并退出")
    parser.add_argument("--preview-dir", help="离线检查的预览输出目录")
    args = parser.parse_args(argv)
    # Worker libraries can emit raw API diagnostics; desktop displays its own safe states.
    logger = logging.getLogger("yuketang")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    app = QApplication(sys.argv[:1])
    configure_app(app)
    window = MainWindow(demo=args.demo or args.smoke_test)
    window.show()
    if args.smoke_test:
        def smoke():
            from pathlib import Path
            import json
            output = Path(args.preview_dir) if args.preview_dir else Path(sys.executable).parent / "preview"
            try:
                output.mkdir(parents=True, exist_ok=True)
                from .desktop_runtime import run_isolated
                probe_stop = threading.Event()
                probe_events = []
                def probe_emit(kind, value):
                    probe_events.append(kind)
                    if kind == "probe_ready":
                        probe_stop.set()
                run_isolated("offline_probe", Config(), probe_stop, probe_emit, grace=0.2)
                assert "probe_ready" in probe_events and "cancelled" in probe_events
                assert len(window.tab_types) == 5
                assert window.windowTitle() == "雨课堂  [演示数据]"
                assert app.applicationName() == "雨课堂"
                # Exercise the frozen cache in an isolated temporary folder with a synthetic account.
                from tempfile import TemporaryDirectory
                from types import SimpleNamespace
                with TemporaryDirectory(prefix="cache-smoke-", dir=output) as temporary:
                    cache_config = SimpleNamespace(project_root=Path(temporary))
                    cache_session = SimpleNamespace(domain="example.invalid", university_id="offline-school", user_id="offline-user")
                    sample_leaf = window.service.leaves[0]
                    cache = TaskStateStore()
                    cache.open_course(cache_config, cache_session, window.service.course, [sample_leaf])
                    cache.remember(sample_leaf.leaf_id, {"server_rate": 1.0, "server_completed": True})
                    cache.remember(sample_leaf.leaf_id, {"state": "服务端已完成", "outcome": "success"})
                    assert cache.flush()
                    restored = TaskStateStore().open_course(cache_config, cache_session, window.service.course, [sample_leaf])
                    assert restored[sample_leaf.leaf_id]["state"] == "上次已完成"
                    assert "outcome" not in restored[sample_leaf.leaf_id]
                    scoped_config = save_settings(Config(university_id="offline-school", session_id="offline-cookie"),
                                                  {"university_id": ""}, Path(temporary) / ".env")
                    assert scoped_config.university_id == "" and scoped_config.effective_university_id == "offline-school"
                    assert scoped_config.session_id == "offline-cookie"
                    switched_config = save_settings(scoped_config, {"university_id": "other-school"}, Path(temporary) / ".env")
                    assert not switched_config.has_credentials and not switched_config.session_university_id
                assert window.summary.text() == "7 项任务"
                assert not hasattr(window, "reuse_btn")
                window.tree.setCurrentItem(window.items[1])
                assert "服务端完成率：未知" in window.items[1].toolTip(2)
                assert window.login_hint.isHidden()
                assert all(widget.text() != "从这里，开启学习" for widget in window.findChildren(QLabel))
                window.start()
                for _ in range(5):
                    window.demo_tick()
                    app.processEvents()
                assert window.service.busy and not window.start_btn.isEnabled()
                app.processEvents()
                window.grab().save(str(output / "course.png"))
                window.stop()
                assert not window.service.busy and not window.demo_timer.isActive()
                assert window.states[1]["state"] == "已停止"
                dialog = SettingsDialog(Config(), window, demo=True)
                dialog.show()
                app.processEvents()
                dialog.grab().save(str(output / "settings.png"))
                assert dialog.key.text() == ""
                for filename in ("up_arrow.svg", "down_arrow.svg"):
                    assert not QPixmap(str(ASSETS / filename)).isNull()
                dialog.tabs.setCurrentIndex(2)
                app.processEvents()
                assert "university_id" in dialog.school_id_help.text()
                dialog.grab().save(str(output / "settings-connection.png"))
                dialog.tabs.setCurrentIndex(0)
                dialog.fields["ai_mode"].showPopup()
                app.processEvents()
                dialog.fields["ai_mode"].view().window().grab().save(str(output / "settings-dropdown.png"))
                dialog.fields["ai_mode"].hidePopup()
                dialog.fields["ai_mode"].setCurrentIndex(1)
                assert dialog.ai_pages.currentIndex() == 1
                dialog.close()
                login_window = MainWindow(service=DesktopService(Config()))
                assert login_window.login_btn.text() == "配置学校ID"
                login_window.show()
                app.processEvents()
                login_window.grab().save(str(output / "login.png"))
                login_window.close()
                (output / "result.json").write_text(json.dumps({"passed": True, "demo": True,
                    "checks": ["native-window", "window-title", "state-cache", "school-input-keeps-login", "task-tabs", "unknown-progress", "start-stop", "masked-key", "ai-mode", "svg-arrows", "manual-school-id", "school-setup-button", "task-count-caption", "clean-home", "dropdown"]}), encoding="utf-8")
                window.close()
                app.exit(0)
            except Exception as exc:
                (output / "result.json").write_text(json.dumps({"passed": False, "error": type(exc).__name__}), encoding="utf-8")
                app.exit(1)
        QTimer.singleShot(100, smoke)
    return app.exec()
