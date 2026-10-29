from __future__ import annotations

from functools import partial
from pathlib import Path
from rich.markup import escape

from textual.widgets import Input

from ..infra.tokens import format_tokens
from ..infra.documents import DocumentError, SUPPORTED_SUFFIXES, read_document
from ..memory.extractor import split_chunks
from ..llm import cached_provider_models, fetch_provider_models
from . import actions
from .chat_widgets import ModelPickerScreen, PickScreen, SessionPickerScreen, TextInputScreen
from .command_catalog import SLASH_COMMANDS
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
            self.add_card("在 /model 按 Ctrl+P 管理内置供应商；完整配置（自定义接口、记忆库）请在终端运行 qm setup。")
        elif command == "/new":
            self._new_session()
        elif command in ("/sessions", "/resume"):
            if argument and command == "/resume":
                self._resume_by_query(argument)
            else:
                self.open_session_picker()
        elif command == "/compact":
            self._run_compact()
        elif command == "/undo":
            self._undo_message()
        elif command == "/summary":
            if not self.runtime.has_session:
                self.add_card("This new session has no summary yet.")
            else:
                summary = self.runtime.summary_text()
                self.add_markdown_card(summary, "Session summary") if summary else self.add_card("This session has no summary yet.")
        elif command == "/status":
            self._show_status()
        elif command == "/rename":
            if not self.runtime.has_session:
                self.add_card("The session is not created yet; send a message first, then rename.")
            elif argument:
                self.runtime.rename(argument)
                self.refresh_sessions()
                self.add_card(f"Renamed to: {argument}")
            else:
                self._open_rename_prompt()
        elif command == "/recall":
            if not argument:
                self.add_card("Usage: /recall <query>")
            else:
                self._run_recall(argument)
        elif command == "/remember":
            if not argument:
                self.add_card("Usage: /remember <text>")
            else:
                self._run_remember(argument)
        elif command == "/import":
            if not argument:
                self.push_screen(TextInputScreen("导入文件或目录路径（默认预览；末尾加 --apply 写入）", on_result=lambda value: self._run_import(value) if value else None))
            else:
                self._run_import(argument)
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
                entry = self.service.approve(item_id, text_override=override)
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
            entries = self.service.topic_entries(matched["topic"])
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

    def _open_rename_prompt(self) -> None:
        self.push_screen(
            TextInputScreen(
                "Rename session",
                default=self.runtime.current.title(),
                on_result=self._rename_current_result,
            )
        )

    def _rename_current_result(self, title) -> None:
        if title:
            self.runtime.rename(title)
            self.refresh_sessions()
            self.notify(f"Renamed to: {title}")
        self.query_one("#input", Input).focus()

    def rename_session(self, row: dict) -> None:
        self.push_screen(
            TextInputScreen(
                "Rename session",
                default=row["title"],
                on_result=partial(self._rename_session_result, row),
            )
        )

    def _rename_session_result(self, row: dict, title) -> None:
        if title:
            if not self.sessions.exists(row["id"]):
                self.notify("Session no longer exists.")
            else:
                # 当前会话走 runtime，保证内存中的 Session 也立即拿到新标题。
                if self.runtime.has_session and row["id"] == self.runtime.current.id:
                    self.runtime.rename(title)
                else:
                    self.sessions.rename(self.sessions.load(row["id"]), title)
                self.refresh_sessions()
                for screen in self.screen_stack:
                    if isinstance(screen, SessionPickerScreen):
                        screen.set_rows(visible_sessions(self.sessions.list(100, with_tokens=False)))
                self.notify("Session renamed.")
        # 重命名弹窗关闭后，焦点回到选择器而不是主输入框。
        if isinstance(self.screen, SessionPickerScreen):
            self.screen.query_one("#picker-input", Input).focus()
        else:
            self.query_one("#input", Input).focus()

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

    def _undo_message(self) -> None:
        if not self.runtime.has_session:
            self.add_card("No message to undo.")
            return
        text = self.runtime.undo()
        if text is None:
            self.add_card("No message to undo.")
            return
        self._clear_chat()
        self.build_transcript()
        self.refresh_sessions()
        self.update_status()
        widget = self.query_one("#input", Input)
        widget.value = text
        widget.cursor_position = len(text)
        widget.focus()
        self.notify("Message restored for editing.")

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

    def _run_recall(self, query: str) -> None:
        self._set_busy(True)
        self.run_worker(
            partial(self._recall_work, query), group="ask", thread=True,
            exclusive=True, exit_on_error=False,
        )

    def _recall_work(self, query: str) -> None:
        try:
            hits = self.service.recall(query, 10)
            message = (
                "\n".join(f"[{hit.topic}] {hit.score:.2f} · {hit.text}" for hit in hits)
                if hits else "No matching memories."
            )
        except Exception as exc:
            self.call_from_thread(self._finish_error, f"Recall failed: {exc}")
            return
        self.call_from_thread(self._after_recall, message)

    def _after_recall(self, message: str) -> None:
        self.add_card(message)
        self.update_status()
        self._set_busy(False)
        self._drain_pending_intent()

    def _run_import(self, argument: str) -> None:
        # 不用 POSIX shlex 拆路径，避免吃掉 Windows 反斜线。
        argument = argument.strip()
        apply = argument.endswith(" --apply")
        filename = argument[:-8].strip() if apply else argument
        if len(filename) >= 2 and filename[0] == filename[-1] and filename[0] in ("'", '"'):
            filename = filename[1:-1]
        if not filename:
            self.add_card("用法：/import <文件或目录路径> [--apply]")
            return
        self._set_busy(True)
        self.add_card("开始导入：解析在本地完成；抽取会调用配置的模型并可能产生费用。" + ("本次写入记忆。" if apply else "本次只预览，不写入记忆。"))
        self.run_worker(partial(self._import_work, Path(filename).expanduser(), apply),
                        group="ask", thread=True, exclusive=True, exit_on_error=False)

    def _import_work(self, path: Path, apply: bool) -> None:
        try:
            files = sorted(file for file in path.iterdir() if file.is_file() and file.suffix.lower() in SUPPORTED_SUFFIXES) if path.is_dir() else [path]
            if not files:
                raise ValueError("当前目录没有支持的文件（不递归扫描）。")
            for file in files:
                try:
                    chunks = split_chunks(read_document(file))
                    for index, chunk in enumerate(chunks, 1):
                        self.call_from_thread(self.add_card, escape(f"正在分析 {file.name}：{index}/{len(chunks)} 块"))
                        result = self.service.import_text(chunk, source=f"import:{file.name}#chunk:{index}", dry_run=not apply)
                        lines = [f"{'已处理' if apply else '预览'}：直写 {len(result.admitted)} · 待确认 {len(result.queued)} · 重复 {len(result.duplicates)} · 拦截 {len(result.blocked)}"]
                        # 只展示已通过凭据裁决的候选；被拦截原文不回显。
                        lines += [f"[{entry.topic}] {entry.text}" for entry in result.admitted]
                        lines += [f"待确认 [{item.topic}] {item.text}" for item in result.queued]
                        self.call_from_thread(self.add_card, escape("\n".join(lines)))
                except DocumentError as exc:
                    self.call_from_thread(self.add_card, escape(f"{file.name} 导入失败：{exc}"))
                except Exception:
                    self.call_from_thread(self.add_card, escape(f"{file.name} 导入失败，请检查格式、解析依赖和模型配置；已处理部分不会回滚。"))
            self.call_from_thread(self.refresh_pending)
        except Exception as exc:
            self.call_from_thread(self._finish_error, f"导入失败：{exc}")
            return
        self.call_from_thread(self._after_recall, "导入结束。" if apply else "预览结束；确认后用 /import <路径> --apply 重新分析并写入。")

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

    def _open_models(self, refresh: bool = False) -> None:
        # keyring/供应商元数据查询可能很慢，不能在 UI 线程先算选项再开选择器。
        self.run_worker(
            partial(self._open_models_work, refresh),
            group="models",
            thread=True,
            exit_on_error=False,
        )

    def _open_models_work(self, refresh: bool) -> None:
        fetched = None if refresh else cached_provider_models(self.service.config.llm)
        try:
            entries = self._model_entries(fetched)
        except Exception as exc:
            self.call_from_thread(self._finish_error, f"Loading models failed: {exc}")
            return
        self.call_from_thread(self._show_model_picker, entries, True, refresh)

    def _model_entries(self, fetched: list[str] | None = None) -> list[tuple]:
        options = actions.model_options(self.service, fetched)
        return [
            (option.label, (option.provider, option.model), option.current)
            for option in options
        ]

    def _show_model_picker(
        self,
        entries: list[tuple],
        background_refresh: bool = False,
        refresh: bool = False,
    ) -> None:
        self._drain_pending_intent()
        picker = ModelPickerScreen(
            f"Current model: {self.runtime.model_label}",
            entries,
            self._model_picked,
            searchable=True,
        )
        self.push_screen(picker)
        if background_refresh:
            self._refresh_models(picker, refresh)

    def _refresh_models(self, picker: PickScreen, refresh: bool = False) -> None:
        self.run_worker(
            partial(self._refresh_models_work, picker, refresh),
            group="models",
            thread=True,
            exit_on_error=False,
        )

    def _refresh_models_work(self, picker: PickScreen, refresh: bool = False) -> None:
        try:
            fetched = fetch_provider_models(self.service.config.llm, refresh=refresh)
            entries = self._model_entries(fetched) if fetched else None
        except Exception:
            return
        if entries is not None:
            self.call_from_thread(self._apply_fetched_models, picker, entries)

    def _apply_fetched_models(self, picker: PickScreen, entries: list[tuple]) -> None:
        # 异步刷新完成时用户可能已离开 /model；只更新仍然打开的这一屏。
        if self.screen is not picker:
            return
        picker.update_options(entries)

    def _model_picked(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        provider, model = value
        if provider == "__providers__":
            self.push_screen(PickScreen(
                "供应商管理（Esc 返回模型列表）",
                [("添加供应商", "add"), ("更新供应商 Key", "update"), ("移除供应商", "remove")],
                self._provider_management_picked,
            ))
            return
        if provider == "__refresh__":
            self._open_models(True)
            return
        if provider == "__custom__":
            current = self.service.config.llm
            self.push_screen(
                TextInputScreen("Model name", default=current.model, on_result=self._model_custom)
            )
            return
        provider_action = {
            "__add_provider__": "add",
            "__update_provider__": "update",
            "__remove_provider__": "remove",
        }.get(provider)
        if provider_action:
            self._manage_providers(provider_action)
            return
        message = actions.apply_model(self.service, self.runtime, provider, model)
        self.add_card(message)
        self.update_status()
        self.query_one("#input", Input).focus()

    def _provider_management_picked(self, action: str | None) -> None:
        if action is None:
            self._open_models()
        else:
            self._manage_providers(action)

    def _manage_providers(self, action: str) -> None:
        options = actions.provider_options(self.service)
        current = self.service.config.llm.provider
        if action == "add":
            candidates = [option for option in options if not option[1][2]]
            title = "Add provider"
            empty = "All built-in providers already have API keys."
        elif action == "update":
            candidates = [option for option in options if option[1][2]]
            title = "Update provider key"
            empty = "No built-in provider has a saved API key yet."
        else:
            candidates = [
                option
                for option in options
                if option[1][2] and option[1][0] != current
            ]
            title = "Remove provider"
            empty = "Switch to another model before removing the current provider."
        if not candidates:
            self.add_card(empty)
            self.query_one("#input", Input).focus()
            return
        self.push_screen(
            PickScreen(title, candidates, partial(self._provider_selected, action))
        )

    def _provider_selected(self, action: str, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        provider_key, label, _ = value
        if action == "remove":
            self.push_screen(
                PickScreen(
                    f"Remove {label} provider?",
                    [("Remove provider", True), ("Cancel", False)],
                    partial(self._remove_provider_result, provider_key),
                )
            )
            return
        self.push_screen(
            TextInputScreen(
                f"{label} API key",
                password=True,
                on_result=partial(self._provider_key_entered, action, provider_key),
            )
        )

    def _provider_key_entered(self, action: str, provider_key: str, api_key) -> None:
        if not api_key:
            self.query_one("#input", Input).focus()
            return
        if action == "add":
            message = actions.configure_provider(
                self.service, self.runtime, provider_key, api_key
            )
        else:
            message = actions.update_provider_key(self.service, provider_key, api_key)
        self.add_card(message)
        self.update_status()
        self.query_one("#input", Input).focus()

    def _remove_provider_result(self, provider_key: str, confirmed) -> None:
        if confirmed is not True:
            self.query_one("#input", Input).focus()
            return
        message = actions.remove_provider(self.service, provider_key)
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
