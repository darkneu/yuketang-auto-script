"""Small native widgets for task selection and the live run history."""
from PySide6.QtCore import Qt, QSize, QTimer, QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QCheckBox, QVBoxLayout, QHBoxLayout,
    QScrollArea, QProgressBar, QSizePolicy,
)
from .desktop_state import progress_caption


def text_label(text="", name=None):
    label = QLabel(text)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    if name:
        label.setObjectName(name)
    return label


class TaskSwitch(QCheckBox):
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + 48, 40)

    def hitButton(self, position):
        return self.rect().contains(position)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        y = (self.height() - 20) / 2
        track = QRectF(0, y, 36, 20)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#398469" if self.isChecked() else "#c8d5ca"))
        painter.drawRoundedRect(track, 10, 10)
        painter.setBrush(QColor("white"))
        painter.drawEllipse(QRectF(18 if self.isChecked() else 2, y + 2, 16, 16))
        painter.setPen(QColor("#203e38"))
        painter.drawText(self.rect().adjusted(44, 0, 0, 0), Qt.AlignLeft | Qt.AlignVCenter, self.text())
        if self.hasFocus():
            painter.setPen(QPen(QColor("#6a9b7e"), 1, Qt.DotLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(self.rect().adjusted(1, 2, -1, -2), 6, 6)


def answer_text(answer):
    if isinstance(answer, dict):
        return "\n".join(f"第 {key} 空：{value}" for key, value in answer.items())
    if isinstance(answer, (tuple, list)):
        return "、".join(str(value) for value in answer)
    return str(answer)


def set_text(label, value):
    if label.text() != value:
        label.setText(value)


class TaskCard(QFrame):
    def __init__(self, leaf, parent=None):
        super().__init__(parent)
        self.leaf = leaf
        self.setObjectName("taskCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(18, 16, 18, 16)
        self.box.setSpacing(10)
        header = QHBoxLayout()
        header.addWidget(text_label((leaf.chapter_name or "未命名章节") + " · " + leaf.type_name, "muted"), 1)
        self.state = text_label("处理中", "taskState")
        header.addWidget(self.state)
        self.box.addLayout(header)
        self.box.addWidget(text_label(leaf.name, "taskTitle"))
        self.reason = text_label("", "muted")
        self.reason.hide()
        self.box.addWidget(self.reason)
        self.video_row = QWidget()
        video = QVBoxLayout(self.video_row)
        video.setContentsMargins(0, 0, 0, 0)
        video.setSpacing(6)
        self.position = text_label("", "muted")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.server = text_label("服务端完成率：未知", "muted")
        video.addWidget(self.position)
        video.addWidget(self.progress)
        video.addWidget(self.server)
        self.video_row.hide()
        self.box.addWidget(self.video_row)
        self.content = text_label()
        self.content.hide()
        self.box.addWidget(self.content)
        self.discussion_answer = text_label()
        self.discussion_answer.hide()
        self.box.addWidget(self.discussion_answer)
        self.problems = {}
        self.problem_numbers = {}

    def update_state(self, state, *, problem_ids=None):
        set_text(self.state, state.get("stage") or state.get("state", "处理中"))
        set_text(self.reason, state.get("reason", ""))
        self.reason.setVisible(bool(state.get("reason")))
        if "position" in state and state.get("duration", 0) > 0:
            self.video_row.show()
            position, duration = state["position"], state["duration"]
            clock = lambda value: f"{int(value) // 60:02d}:{int(value) % 60:02d}"
            set_text(self.position, f"{clock(position)} / {clock(duration)} · 本地播放位置")
            self.progress.setValue(min(1000, int(position / duration * 1000)))
        set_text(self.server, progress_caption(state))
        if state.get("content"):
            set_text(self.content, state["content"])
            self.content.show()
        if "answer" in state:
            set_text(self.discussion_answer, "答案 · " + state.get("answer_state", "已生成") + "\n" + answer_text(state["answer"]))
            self.discussion_answer.show()
        problems = state.get("problems", {})
        for problem_id in (problems if problem_ids is None else problem_ids):
            problem = problems[problem_id]
            if problem_id not in self.problems:
                frame = QFrame()
                frame.setObjectName("problemCard")
                layout = QVBoxLayout(frame)
                layout.setContentsMargins(14, 12, 14, 12)
                layout.setSpacing(8)
                title, body, answer = text_label(name="taskState"), text_label(), text_label()
                layout.addWidget(title)
                layout.addWidget(body)
                layout.addWidget(answer)
                self.box.addWidget(frame)
                self.problems[problem_id] = (title, body, answer)
                self.problem_numbers[problem_id] = len(self.problems)
            index = self.problem_numbers[problem_id]
            title, body, answer = self.problems[problem_id]
            kind = {"SingleChoice": "单选题", "MultipleChoice": "多选题", "Choice": "选择题",
                    "Judgement": "判断题", "FillBlank": "填空题", "ShortAnswer": "简答题"}.get(problem.get("type"), "题目")
            set_text(title, f"第 {index} 题 · {kind} · {problem['state']}")
            parts = [problem.get("body", "")] + problem.get("options", [])
            if problem.get("type") == "FillBlank":
                parts.append(f"{problem.get('blank_count', 1)} 个空")
            set_text(body, "\n".join(parts))
            if "answer" in problem:
                set_text(answer, "答案：" + answer_text(problem["answer"]))
                answer.show()
            else:
                answer.hide()
        self.updateGeometry()


class RunPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        self.stats = text_label("", "runStats")
        layout.addWidget(self.stats)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("runScroll")
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.container = QWidget()
        self.container.setObjectName("runContainer")
        self.cards_box = QVBoxLayout(self.container)
        self.cards_box.setContentsMargins(0, 0, 6, 0)
        self.cards_box.setSpacing(14)
        self.cards_box.addStretch()
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)
        self.cards = {}
        self.leaves = {}
        self.follow_tail = True
        self._auto_scroll = False
        bar = self.scroll.verticalScrollBar()
        bar.valueChanged.connect(self._scrolled)
        bar.rangeChanged.connect(self._range_changed)

    def reset(self, leaves=()):
        for card in self.cards.values():
            self.cards_box.removeWidget(card)
            card.deleteLater()
        self.cards = {}
        self.leaves = {leaf.leaf_id: leaf for leaf in leaves}
        self.follow_tail = True
        self.update_stats({})

    def _scrolled(self, value):
        if not self._auto_scroll:
            self.follow_tail = value >= self.scroll.verticalScrollBar().maximum() - 4

    def _range_changed(self, *_):
        if self.follow_tail:
            QTimer.singleShot(0, self._tail)

    def _tail(self):
        if self.follow_tail:
            self._auto_scroll = True
            self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum())
            self._auto_scroll = False

    def update_task(self, leaf_id, state, *, problem_ids=None):
        leaf = self.leaves.get(leaf_id)
        if leaf is None or state.get("state") == "等待中":
            return
        if leaf_id not in self.cards:
            card = TaskCard(leaf)
            self.cards[leaf_id] = card
            self.cards_box.insertWidget(self.cards_box.count() - 1, card)
        self.cards[leaf_id].update_state(state, problem_ids=problem_ids)
        if self.follow_tail:
            QTimer.singleShot(0, self._tail)

    def update_stats(self, states):
        values = [states.get(leaf_id, {}) for leaf_id in self.leaves]
        done = sum(value.get("outcome") == "success" for value in values)
        skipped = sum(value.get("outcome") == "skipped" for value in values)
        failed = sum(value.get("outcome") in {"failed", "unprocessed"} for value in values)
        stopped = sum(value.get("state") == "已停止" for value in values)
        active = sum(value.get("state") in {"处理中", "演示处理中"} and not value.get("outcome") for value in values)
        waiting = max(0, len(values) - done - skipped - failed - stopped - active)
        self.stats.setText(f"本轮 · 处理中 {active}   ·   已完成 {done}   ·   已跳过 {skipped}   ·   未完成 {failed}   ·   待处理 {waiting}" +
                           (f"   ·   已停止 {stopped}" if stopped else ""))
