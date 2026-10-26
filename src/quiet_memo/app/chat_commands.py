from __future__ import annotations

from functools import partial

from textual.widgets import Input

from ..infra.tokens import format_tokens
from . import actions
from .chat_widgets import PickScreen, SLASH_COMMANDS, SessionPickerScreen, TextInputScreen
from .session_ui import visible_sessions


class ChatCommandsMixin:
    """斜杠命令、待确认、主题、会话与设置选择器；依赖 ChatApp 的界面方法。"""

    # ---------- 斜杠命令 ----------

    def _command(self, line: str) -> None:
        command, _, argument = line.partition(" ")
        command = command.lower()
        argument = argument.strip()

        if command in ("/exit", "/quit"):
            self.exit()
        elif command == "/help":
            self.add_card("\n".join(f"{usage:<24}{description}" for _, usage, description in SLASH_COMMANDS), "Commands")
        elif command == "/model":
            self._open_models(argument.lower() in ("refresh", "--refresh", "-r"))
        elif command == "/think":
            self._pick_reasoning()
        elif command == "/context":
            self._pick_context()
        elif command == "/setup":
            self.add_card("Preset providers can be configured in /model; full setup (custom endpoints, vault) requires qm setup in a terminal.")
        elif command == "/new":
            self._new_session()
        elif command in ("/sessions", "/resume"):
            if argument and command == "/resume":
                self._resume_by_query(argument)
            else:
                self.open_session_picker()
        elif command == "/compact":
            self._run_compact()
        elif command == "/summary":
            if not self.runtime.has_session:
                self.add_card("This new session has no summary yet.")
            else:
                summary = self.runtime.summary_text()
                self.add_markdown_card(summary, "Session summary") if summary else self.add_card("This session has no summary yet.")
        elif command == "/status":
            self._show_status()
        elif command == "/rename":
            if not argument:
                self.add_card("Usage: /rename <title>")
            elif not self.runtime.has_session:
                self.add_card("The session is not created yet; send a message first, then rename.")
            else:
                self.runtime.rename(argument)
                self.refresh_sessions()
                self.add_card(f"Renamed to: {argument}")
        elif command == "/recall":
            if not argument:
                self.add_card("Usage: /recall <query>")
            else:
                hits = self.service.recall(argument, 10)
                self.add_card(
                    "\n".join(f"[{hit.topic}] {hit.score:.2f} · {hit.text}" for hit in hits)
                    if hits
                    else "No matching memories."
                )
        elif command == "/remember":
            if not argument:
                self.add_card("Usage: /remember <text>")
            else:
                self._run_remember(argument)
        elif command == "/pending":
            self._open_pending()
        elif command == "/topics":
            self._show_topics(argument.strip() or None)
        else:
            self.add_card(f"Unknown command: {command}. Type /help for help.")

    # ---------- 待确认交互处理（issue #1） ----------

    @staticmethod
    def _truncate(text: str, width: int) -> str:
        return text if len(text) <= width else text[:width] + "…"

    def _open_pending(self) -> None:
        items = self.service.pending()
        if not items:
            self.add_card("No pending items.")
            return
        options = [
            (f"{item.id}  [{item.topic}] {self._truncate(item.text, 36)}", item.id)
            for item in items
        ]
        self.push_screen(PickScreen(f"Pending items: {len(items)}", options, self._pending_picked))

    def _pending_picked(self, item_id) -> None:
        if item_id is None:
            self.query_one("#input", Input).focus()
            return
        item = next((i for i in self.service.pending() if i.id == item_id), None)
        if item is None:
            self.notify("That pending item no longer exists.")
            return
        options = [
            ("Approve", ("approve", item_id, None)),
            ("Edit and approve", ("edit", item_id, None)),
            ("Reject", ("reject", item_id, None)),
        ]
        self.push_screen(PickScreen(f"[{item.topic}] {self._truncate(item.text, 44)}", options, self._pending_action))

    def _pending_action(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        action, item_id, _ = value
        if action == "edit":
            item = next((i for i in self.service.pending() if i.id == item_id), None)
            default = item.text if item else ""
            self.push_screen(
                TextInputScreen("Full revised text", default=default, on_result=lambda v: self._pending_edited(item_id, v))
            )
            return
        self._pending_apply(action, item_id, None)

    def _pending_edited(self, item_id, text) -> None:
        if not text:
            self.query_one("#input", Input).focus()
            return
        self._pending_apply("approve", item_id, text.strip())

    def _pending_apply(self, action: str, item_id: str, override: str | None) -> None:
        try:
            if action == "approve":
                entry = self.service.approve(item_id)
                if override and override != entry.text:
                    entry = self.service.correct(entry.id, override, reason="人工改写后写入")
                message = f"Saved [{entry.topic}] {entry.text}"
            else:
                item = self.service.reject(item_id)
                message = f"Rejected {item.id} {self._truncate(item.text, 40)}"
        except (KeyError, ValueError) as exc:
            self.add_card(str(exc))
            return
        self.add_card(message)
        self.refresh_pending()
        self.update_status()
        if self.service.pending():
            # 队列里还有：自动进入下一条，ESC 随时退出循环。
            self.call_after_refresh(self._open_pending)
        else:
            self.add_card("Pending queue is empty.")
            self.query_one("#input", Input).focus()

    def _show_topics(self, name: str | None = None) -> None:
        topics = self.service.topics()
        if not topics:
            self.add_card("The memory vault is empty.")
            return
        if name:
            needle = name.lower()
            matched = next(
                (t for t in topics if needle in t["topic"].lower() or needle in t["title"].lower()),
                None,
            )
            if matched is None:
                self.add_card(f"No matching topic: {name}")
                return
            entries = self.service.store.entries(topic=matched["topic"])
            lines = [f"{matched['title']} ({matched['topic']}) · {len(entries)} entries", ""]
            for entry in entries:
                tag = "" if entry.status == "active" else f" ({entry.status})"
                lines.append(f"{entry.id[:26]}  {self._truncate(entry.text, 56)}{tag}")
            self.add_card("\n".join(lines), "Topic entries")
            return
        self.add_card(
            "\n".join(f"{t['topic']:<24} {t['title']} · {t['entries']} entries · updated {t['updated']}" for t in topics),
            "Memory topics",
        )

    def open_session_picker(self) -> None:
        rows = visible_sessions(self.sessions.list(100, with_tokens=False))
        if not rows:
            self.add_card("No sessions to choose from.")
            return
        current_id = self.runtime.current.id if self.runtime.has_session else None
        self.push_screen(
            SessionPickerScreen(rows, current_id, self._picker_selected)
        )

    def _picker_selected(self, session_id) -> None:
        if session_id is None:
            self.query_one("#input", Input).focus()
            return
        if self.runtime.has_session and session_id == self.runtime.current.id:
            self.notify("Already the current session.")
            self.query_one("#input", Input).focus()
            return
        if self.busy:
            self._pending_switch = session_id
            return
        self._switch_session(session_id)

    def _resume_by_query(self, query: str) -> None:
        if self.busy:
            return
        rows = visible_sessions(self.sessions.list(100))
        needle = query.lower()
        matched = next(
            (row for row in rows if row["id"] == query or needle in row["title"].lower()),
            None,
        )
        if matched is None:
            self.notify("No matching session.")
            return
        if self.runtime.has_session and matched["id"] == self.runtime.current.id:
            self.notify("Already the current session.")
            return
        self._switch_session(matched["id"])

    def _show_status(self) -> None:
        if not self.runtime.has_session:
            self.add_card("The session is not created yet; it appears after your first message.")
            return
        info = self.runtime.status()
        percent = info["estimated_tokens"] / max(info["context_window"], 1)
        self.add_card(
            f"Session: {info['session_id']} · {info['title']}\n"
            f"Model: {info['model']}\n"
            f"Retrieval: {info['retrieval']}\n"
            f"Context: {format_tokens(info['estimated_tokens'])}/{format_tokens(info['context_window'])} ({percent:.0%})\n"
            f"Summary: {info['summaries']} segments / {info['summary_chars']} chars · covering {info['covered_records']} records\n"
            f"Pending: {info['pending']}",
            "Status",
        )

    def _run_compact(self) -> None:
        self._set_busy(True)
        self.run_worker(
            self._compact_work, group="ask", thread=True, exclusive=True, exit_on_error=False
        )

    def _compact_work(self) -> None:
        try:
            events = self.runtime.force_compact()
        except Exception as exc:
            self.call_from_thread(self._finish_error, f"Compaction failed: {exc}")
            return
        self.call_from_thread(self._after_compact, events)

    def _after_compact(self, events: list[str]) -> None:
        for event in events:
            self.add_event(event)
        self.update_status()
        self._set_busy(False)
        self._drain_pending_intent()

    def _run_remember(self, text: str) -> None:
        self._set_busy(True)
        self.run_worker(
            partial(self._remember_work, text),
            group="ask",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _remember_work(self, text: str) -> None:
        source = f"session:{self.runtime.current.id}:manual" if self.runtime.has_session else "manual"
        try:
            result = self.service.memorize(text, source=source)
            message = (
                f"Wrote {len(result.admitted)}, pending {len(result.queued)}, "
                f"duplicates {len(result.duplicates)}, blocked {len(result.blocked)}"
            )
        except Exception as exc:
            message = f"Write failed: {exc}"
        self.call_from_thread(self._after_remember, message)

    def _after_remember(self, message: str) -> None:
        self.add_card(message)
        self.refresh_pending()
        self.update_status()
        self._set_busy(False)
        self._drain_pending_intent()

    # ---------- 设置类选择器（逻辑在 actions.py） ----------

    def _open_models(self, refresh: bool) -> None:
        self._set_busy(True)
        self.run_worker(
            partial(self._models_work, refresh),
            group="ask",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _models_work(self, refresh: bool) -> None:
        try:
            options = actions.model_options(self.service, refresh=refresh)
        except Exception as exc:
            self.call_from_thread(self._finish_error, f"Failed to fetch models: {exc}")
            return
        entries = [(option.label, (option.provider, option.model), option.current) for option in options]
        entries.append(("↻ Refresh models", ("__refresh__", ""), False))
        entries.append(("Custom model…", ("__custom__", ""), False))
        entries.append(("Configure provider…", ("__provider__", ""), False))
        self.call_from_thread(self._show_model_picker, entries)

    def _show_model_picker(self, entries: list[tuple]) -> None:
        self._set_busy(False)
        self._drain_pending_intent()
        self.push_screen(
            PickScreen(f"Current model: {self.runtime.model_label}", entries, self._model_picked)
        )

    def _model_picked(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        provider, model = value
        if provider == "__refresh__":
            self._open_models(True)
            return
        if provider == "__custom__":
            current = self.service.config.llm
            self.push_screen(
                TextInputScreen("Model name", default=current.model, on_result=self._model_custom)
            )
            return
        if provider == "__provider__":
            self.push_screen(
                PickScreen(
                    "Configure provider",
                    actions.provider_options(self.service),
                    self._provider_picked,
                )
            )
            return
        message = actions.apply_model(self.service, self.runtime, provider, model)
        self.add_card(message)
        self.update_status()
        self.query_one("#input", Input).focus()

    def _provider_picked(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        provider_key, label = value
        self.push_screen(
            TextInputScreen(
                f"{label} API key",
                password=True,
                on_result=partial(self._provider_key_entered, provider_key),
            )
        )

    def _provider_key_entered(self, provider_key: str, api_key) -> None:
        if not api_key:
            self.query_one("#input", Input).focus()
            return
        message = actions.configure_provider(self.service, self.runtime, provider_key, api_key)
        self.add_card(message)
        self.update_status()
        self.query_one("#input", Input).focus()

    def _model_custom(self, name) -> None:
        if not name:
            self.query_one("#input", Input).focus()
            return
        current = self.service.config.llm
        message = actions.apply_model(self.service, self.runtime, current.provider, name)
        self.add_card(message)
        self.update_status()
        self.query_one("#input", Input).focus()

    def _pick_reasoning(self) -> None:
        options = actions.reasoning_options(self.service)
        if not options:
            self.add_card(f"{self.runtime.model_label} does not support reasoning effort; use /model to switch to a reasoning-capable model.")
            return
        current = self.service.config.llm.reasoning_effort or ""
        entries = [(label, value, value == current) for value, label in options]
        self.push_screen(PickScreen(f"Reasoning effort: {current or 'default'}", entries, self._reasoning_picked))

    def _reasoning_picked(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        self.add_card(actions.apply_reasoning(self.service, value))
        self.query_one("#input", Input).focus()

    def _pick_context(self) -> None:
        current = self.service.config.chat.context_window
        entries = [(format_tokens(size), size, size == current) for size in actions.context_options(self.service)]
        entries.append(("Custom…", 0, False))
        self.push_screen(PickScreen(f"Context window: {format_tokens(current)}", entries, self._context_picked))

    def _context_picked(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        if value == 0:
            current = self.service.config.chat.context_window
            self.push_screen(
                TextInputScreen("Context window (tokens)", default=str(current), on_result=self._context_custom)
            )
            return
        self.add_card(actions.apply_context(self.service, value))
        self.update_status()
        self.query_one("#input", Input).focus()

    def _context_custom(self, value) -> None:
        try:
            # isdigit 会放行 "①" 这类 Unicode 数字而 int() 抛错，改用 int 校验。
            size = int(value) if value else 0
        except ValueError:
            size = 0
        if size <= 0:
            self.add_card("Context window must be a positive integer (tokens).")
            self.query_one("#input", Input).focus()
            return
        self.add_card(actions.apply_context(self.service, size))
        self.update_status()
        self.query_one("#input", Input).focus()
