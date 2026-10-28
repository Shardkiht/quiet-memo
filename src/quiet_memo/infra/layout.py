from __future__ import annotations

import shutil


def terminal_width() -> int:
    return shutil.get_terminal_size((100, 30)).columns


def widths(cols: int | None = None) -> tuple[int, int, int]:
    """返回（左边距, 内容宽度, 右边距）：宽幅左对齐，约占窗口 90%，随窗口伸缩。"""
    cols = cols or terminal_width()
    width = max(40, min(cols - 4, round(cols * 0.9)))
    left = 2
    right = max(0, cols - left - width)
    return left, width, right
