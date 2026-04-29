from __future__ import annotations

import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QPainter
from PyQt6.QtWidgets import QLabel, QSizePolicy
from qfluentwidgets import isDarkTheme
from qfluentwidgets.common.style_sheet import themeColor


class WaveformWidget(QLabel):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.font = QFont("HarmonyOS Sans SC", 8)
        self.point_size = max(1, self.font.pointSize())
        self.history: list[int] = [0]
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.set_matrix_size(50, 20)
        self.refresh_rate = 2
        self.last_add_value_time = 0.0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(int(1000 / self.refresh_rate))

    def tick(self) -> None:
        if time.time() - self.last_add_value_time >= (1 / self.refresh_rate):
            self.repeat()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setFont(self.font)
        pen_color = themeColor()
        if not pen_color.isValid():
            pen_color = Qt.GlobalColor.white if isDarkTheme() else Qt.GlobalColor.black
        painter.setPen(pen_color)

        columns = max(1, int(self.width() / self.point_size))
        history = self.history[-columns:]
        if len(history) < columns:
            history = [0 for _ in range(columns - len(history))] + history

        min_val = min(history)
        max_val = max(history)
        if max_val - min_val == 0 and history[0] == 0:
            values = [0 for _ in history]
        elif max_val - min_val == 0 and history[0] != 0:
            values = [1 for _ in history]
        else:
            values = [(v - min_val) / (max_val - min_val) for v in history]

        lines = []
        for value in reversed(values):
            lines.append("." * int(value * (self.matrix_height - 1) + 1))

        x = self.width() - self.point_size
        for line in lines:
            y = self.height()
            for point in line:
                painter.drawText(x, y, point)
                y -= self.point_size
            x -= self.point_size

    def repeat(self) -> None:
        self.add_value(self.history[-1] if self.history else 0)

    def add_value(self, value: int) -> None:
        if len(self.history) >= self.matrix_width:
            self.history.pop(0)
        self.history.append(value)
        self.last_add_value_time = time.time()

    def set_matrix_size(self, width: int, height: int) -> None:
        self.matrix_width = max(1, width)
        self.matrix_height = max(1, height)
        self.max_width = self.matrix_width * self.point_size
        self.max_height = self.matrix_height * self.point_size
        self.setFixedHeight(self.max_height)
        self.setMinimumWidth(self.point_size * 10)
        self.history = [0 for _ in range(self.matrix_width)]
