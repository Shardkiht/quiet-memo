from __future__ import annotations

from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout, VSplit, Window

from .layout import widths

CANCEL_MESSAGE = "已取消。"


def pointer_control(question):
    """返回 questionary select 内部的 InquirerControl，用于读取当前高亮项。"""
    return next(
        control
        for control in question.application.layout.find_all_controls()
        if hasattr(control, "get_pointed_at")
    )


def _center_layout(question) -> None:
    """把 questionary 的布局套进左右留白，与对话区保持同一列。"""
    application = question.application
    if getattr(application, "_qm_centered", False):
        return
    cols = application.output.get_size().columns
    left, _, right = widths(cols)
    application.layout = Layout(
        VSplit(
            [
                Window(width=left),
                application.layout.container,
                Window(width=right),
            ]
        )
    )
    application._qm_centered = True


def ask(question, extra=None):
    """questionary 的 ask 包装：ESC 与 Ctrl+C 一样取消当前界面。"""
    bindings = KeyBindings()

    @bindings.add(Keys.Escape, eager=True)
    def _cancel(event):
        event.app.exit(exception=KeyboardInterrupt)

    layers = [bindings]
    if extra is not None:
        layers.append(extra)
    existing = question.application.key_bindings
    if existing is not None:
        layers.append(existing)
    question.application.key_bindings = merge_key_bindings(layers)
    _center_layout(question)
    return question.ask(kbi_msg=CANCEL_MESSAGE)
