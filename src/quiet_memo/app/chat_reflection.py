from __future__ import annotations

import threading
import time
from functools import partial

from rich.markup import escape


class ChatReflectionMixin:
    """回顾 worker 不占聊天 busy；取消令牌在主线程创建，覆盖尚未启动的任务。"""

    def _start_auto_reflection(self) -> None:
        self._reflection_last_activity = time.monotonic()
        self._reflection_next = 0.0
        self._reflection_remaining = self.service.config.chat.reflect_request_budget
        self._reflection_mode = "dream"
        self._reflection_paused = False
        self.set_interval(5, self._auto_reflection_tick)

    def _reflection_activity(self) -> None:
        self._reflection_last_activity = time.monotonic()

    def _auto_reflection_tick(self) -> None:
        config = self.service.config.chat
        now = time.monotonic()
        if (not config.auto_reflect or self._reflection_paused or not getattr(self, "_reflection_ready", False)
            or self.busy or getattr(self, "_reflection_pending", False) or self._reflection_remaining <= 0
            or now < self._reflection_next or now - self._reflection_last_activity < config.reflect_idle_seconds):
            return
        from textual.widgets import Input
        if self.query_one("#input", Input).value.strip() or len(self.screen_stack) > 1:
            return
        self._reflection_next = now + config.reflect_interval_seconds
        self._reflection_pending = True
        self._reflection_cancel = threading.Event()
        mode = self._reflection_mode
        self._reflection_mode = "profile" if mode == "dream" else "dream"
        # 每轮只发一次；缺少新材料时引擎不调用模型，也不消耗预算。
        self.run_worker(partial(self._auto_reflection_work, mode, self._reflection_cancel),
                        thread=True, group="reflection", exit_on_error=False)

    def _auto_reflection_work(self, mode: str, token: threading.Event) -> None:
        try:
            result = self.service.reflection.run(mode, 1, cancel_event=token)
            self.call_from_thread(self._after_auto_reflection, result, None)
        except Exception:
            self.call_from_thread(self._after_auto_reflection, None, "自动回顾失败，已保留进度。")

    def _after_auto_reflection(self, result, error: str | None) -> None:
        self._reflection_pending = False
        self._reflection_next = time.monotonic() + self.service.config.chat.reflect_interval_seconds
        if result is not None:
            self._reflection_remaining -= result.requests
            error = result.error
            if result.proposed:
                self.notify(f"后台回顾新增 {result.proposed} 条推断，可用 /inferences 审查。")
        else:
            self._reflection_remaining -= 1
        if error:
            self.notify(error)

    def _reflection_command(self, mode: str, argument: str) -> None:
        if argument == "resume":
            self._reflection_paused = False
            self.add_card("自动回顾已恢复，空闲后继续。")
            return
        if argument == "cancel":
            self._reflection_paused = True
            token = getattr(self, "_reflection_cancel", None)
            if token is not None:
                token.set()
            self.service.reflection.cancel()
            self.add_card("已请求取消；正在发送的模型请求返回后停止，不再提交该轮候选。")
            return
        if argument == "status":
            running = "运行中" if getattr(self, "_reflection_pending", False) else "空闲"
            enabled = self.service.config.chat.auto_reflect and not getattr(self, "_reflection_paused", False)
            self.add_card(f"回顾{running}；自动回顾{'启用' if enabled else '暂停/关闭'}；本次启动剩余请求预算 {getattr(self, '_reflection_remaining', 0)}。")
            return
        if mode == "profile" and not argument:
            self.run_worker(self._profile_work, thread=True, group="profile-view", exit_on_error=False)
            return
        if mode == "profile" and argument.split()[0] != "refresh":
            self.add_card("查看画像用 /profile；调用模型提炼请用 /profile refresh [0–20]。")
            return
        value = argument.removeprefix("refresh").strip() if mode == "profile" else argument
        try:
            budget = int(value) if value else 1
            if not 0 <= budget <= 20:
                raise ValueError
        except ValueError:
            self.add_card("用法：/dream [0–20|cancel|status]，/profile [refresh [0–20]|cancel|status]")
            return
        if getattr(self, "_reflection_pending", False):
            self.add_card("已有回顾任务运行中，可输入 /dream cancel 取消。")
            return
        self._reflection_cancel = threading.Event()
        self._reflection_pending = True
        self.add_card(f"已启动后台回顾，本次最多 {budget} 次模型请求；可以继续聊天或切换会话。")
        self.run_worker(partial(self._reflection_work, mode, budget, self._reflection_cancel),
                        thread=True, group="reflection", exit_on_error=False)

    def _profile_work(self) -> None:
        try:
            items = self.service.reflection.profile()
            labels = {"active": "事实", "hypothesis": "待确认推断", "confirmed": "已确认推断"}
            text = "\n".join(f"[{labels.get(item.status, item.status)}] {item.text}\n来源：{', '.join(item.source_ids)}" for item in items)
            text = text or "暂无画像。保存偏好、目标或习惯后可查看；/profile refresh 可生成待审查推断。"
            text += "\n事实可通过现有更正/忘记操作调整；推断可在 /inferences 审查。"
        except Exception:
            text = "画像读取失败，请检查本地记忆与推断日志。"
        self.call_from_thread(self.add_card, escape(text))

    def _reflection_work(self, mode: str, budget: int, token: threading.Event) -> None:
        try:
            result = self.service.reflection.run(mode, budget, cancel_event=token)
            message = (f"回顾{'已取消' if result.cancelled else '结束'}：请求 {result.requests} 次，"
                       f"新增推断 {result.proposed} 条，剩余 {result.remaining} 批。")
            if result.error:
                message += f"\n{result.error}"
            if result.proposed:
                message += "\n可在 /inferences 查看与审查。"
        except Exception:
            message = "回顾失败，已保留本地进度；请检查任务日志后重试。"
        self.call_from_thread(self._after_reflection, message)

    def _after_reflection(self, message: str) -> None:
        self._reflection_pending = False
        self.add_card(escape(message))

    def on_unmount(self) -> None:
        token = getattr(self, "_reflection_cancel", None)
        if token is not None:
            token.set()
        self.service.reflection.cancel()
