from __future__ import annotations

from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.keys import Keys

CANCEL_MESSAGE = "已取消。"


def pointer_control(question):
    """返回 questionary select 内部的 InquirerControl，用于读取当前高亮项。"""
    return next(
        control
        for control in question.application.layout.find_all_controls()
        if hasattr(control, "get_pointed_at")
    )


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
    return question.ask(kbi_msg=CANCEL_MESSAGE)
