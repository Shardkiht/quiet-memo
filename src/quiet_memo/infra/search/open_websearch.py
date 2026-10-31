from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import httpx

from .base import (
    FetchQuery,
    FetchResult,
    ProviderError,
    ProviderErrorKind,
    SearchProvider,
    SearchQuery,
    SearchResult,
)

NAME = "open_websearch"
DAEMON_HINT = (
    "未连接到 open-websearch daemon；请先运行 "
    "`npx open-websearch@latest serve`（或 `open-websearch serve`）。"
)
MAX_DAEMON_LIMIT = 50
MAX_FETCH_CHARS = 8000
MIN_FETCH_CHARS = 1000
SEARCH_ENGINES = (
    "bing",
    "baidu",
    "sogou",
    "duckduckgo",
    "brave",
    "startpage",
    "csdn",
    "juejin",
    "linuxdo",
    "hackernews",
    "exa",
)
ClientFactory = Callable[[], httpx.Client]


def npx_command(
    command: str = "npx",
    args: tuple[str, ...] = ("-y", "open-websearch@latest"),
) -> list[str]:
    """解析 npx 路径；Windows 下 .cmd/.bat 通过 cmd /c 启动。"""
    resolved = shutil.which(command) or shutil.which(f"{command}.cmd")
    if not resolved:
        raise ProviderError(
            NAME, ProviderErrorKind.NETWORK, "未找到 npx；请先安装 Node.js"
        )
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", resolved, *args]
    return [resolved, *args]


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                return 0.0
            return max(0.0, (moment - datetime.now(moment.tzinfo)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def _error_kind(code: str) -> ProviderErrorKind:
    lowered = code.lower()
    if "rate" in lowered or "limit" in lowered:
        return ProviderErrorKind.RATE_LIMIT
    if "browser" in lowered or "engine" in lowered:
        return ProviderErrorKind.UNAVAILABLE
    return ProviderErrorKind.BAD_RESPONSE


def _map_result(item: Any) -> SearchResult | None:
    """只在这里映射 open-websearch 字段；上层不依赖其原始 schema。"""
    if not isinstance(item, dict):
        return None
    url = _text(item.get("url"))
    if not url:
        return None
    published = item.get("published") or item.get("published_at") or item.get("date")
    return SearchResult(
        title=_text(item.get("title")) or url,
        url=url,
        snippet=_text(item.get("description")) or _text(item.get("snippet")),
        published=_text(published) or None,
        provider=NAME,
    )


class HttpTransport:
    """默认传输：调用本地 open-websearch daemon 的 POST /search。"""

    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        client_factory: ClientFactory | None = None,
        engines: list[str] | tuple[str, ...] | None = None,
        fetch_timeout: float = 45.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.engines = list(engines or [])
        self.fetch_timeout = max(float(fetch_timeout), self.timeout)
        self._client_factory = client_factory or (
            # localhost 不走系统代理；否则开了代理时可能连不上本地 daemon。
            lambda: httpx.Client(timeout=timeout, trust_env=False)
        )

    def search(
        self,
        query: str,
        limit: int,
        engines: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict]:
        body = {"query": query, "limit": max(1, min(limit, MAX_DAEMON_LIMIT))}
        selected = list(engines) if engines else self.engines
        if selected:
            # 不传时用 daemon 的 DEFAULT_SEARCH_ENGINE（默认 bing）。
            body["engines"] = selected
        return self._parse(self._post("/search", body))

    def fetch(self, url: str, max_chars: int) -> dict:
        body = {
            "url": url,
            "maxChars": max(MIN_FETCH_CHARS, min(max_chars, MAX_FETCH_CHARS)),
            "readability": True,
            "renderMode": "request",
        }
        # 抓正文比搜索慢，给它单独的请求超时，避免 10s 默认值过早中断。
        response = self._post("/fetch-web", body, timeout=self.fetch_timeout)
        return self._parse_data(response)

    def _post(self, path: str, body: dict, timeout: float | None = None) -> httpx.Response:
        kwargs = {"json": body}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            with self._client_factory() as client:
                response = client.post(f"{self.base_url}{path}", **kwargs)
                if response.status_code == 429:
                    delay = _retry_after_seconds(response.headers.get("Retry-After"))
                    if delay:
                        time.sleep(min(delay, 5.0))
                    response = client.post(f"{self.base_url}{path}", **kwargs)
        except httpx.TimeoutException as exc:
            raise ProviderError(NAME, ProviderErrorKind.TIMEOUT, str(exc)) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                NAME, ProviderErrorKind.NETWORK, f"{exc}；{DAEMON_HINT}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(NAME, ProviderErrorKind.NETWORK, str(exc)) from exc

        if response.status_code == 429:
            raise ProviderError(
                NAME, ProviderErrorKind.RATE_LIMIT, "open-websearch 仍在限流（HTTP 429）"
            )
        if response.status_code >= 500:
            raise ProviderError(
                NAME, ProviderErrorKind.UNAVAILABLE, f"open-websearch HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise ProviderError(
                NAME, ProviderErrorKind.BAD_RESPONSE, f"open-websearch HTTP {response.status_code}"
            )
        return response

    def _parse_data(self, response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                NAME, ProviderErrorKind.BAD_RESPONSE, "open-websearch 返回的不是 JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "open-websearch 返回结构异常")
        if payload.get("status") != "ok":
            error = payload.get("error")
            code = _text(error.get("code")) if isinstance(error, dict) else ""
            detail = _text(error.get("message")) if isinstance(error, dict) else code
            raise ProviderError(NAME, _error_kind(code), detail or "open-websearch 调用失败")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "open-websearch 响应缺少 data")
        return data

    def _parse(self, response: httpx.Response) -> list[dict]:
        data = self._parse_data(response)
        results = data.get("results")
        if not isinstance(results, list):
            raise ProviderError(
                NAME, ProviderErrorKind.BAD_RESPONSE, "open-websearch 响应缺少 data.results"
            )
        failures = data.get("partialFailures")
        if not results and isinstance(failures, list) and failures:
            details = "; ".join(
                f"{item.get('engine', 'unknown')}: {item.get('code', '')} {item.get('message', '')}"
                for item in failures if isinstance(item, dict)
            )
            kind = ProviderErrorKind.TIMEOUT if "TIMEOUT" in details.upper() or "TIMEDOUT" in details.upper() else ProviderErrorKind.NETWORK
            raise ProviderError(NAME, kind, details or "All search engines failed")
        return [item for item in results if isinstance(item, dict)]


class McpStdioTransport:
    """可选传输：通过 stdio MCP 调用 open-websearch 的 search 工具。

    每次搜索启动一次进程，避免在安静优先的 CLI 里维护后台子进程；
    日常使用仍推荐先启动 HTTP daemon。
    """

    def __init__(
        self,
        timeout: float = 10.0,
        command: str = "npx",
        args: tuple[str, ...] = ("-y", "open-websearch@latest"),
        engines: list[str] | tuple[str, ...] | None = None,
    ):
        self.timeout = timeout
        self.command = command
        self.args = args
        self.engines = list(engines or [])

    def search(
        self,
        query: str,
        limit: int,
        engines: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict]:
        command = self._command_line()
        env = os.environ.copy()
        env["MODE"] = "stdio"
        env.setdefault("OPEN_WEBSEARCH_QUIET_STARTUP", "true")
        arguments = {"query": query, "limit": max(1, min(limit, MAX_DAEMON_LIMIT))}
        selected = list(engines) if engines else self.engines
        if selected:
            arguments["engines"] = selected
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
        except FileNotFoundError as exc:
            raise ProviderError(
                NAME,
                ProviderErrorKind.NETWORK,
                f"未找到 npx；请先安装 Node.js，或改用 HTTP daemon。{exc}",
            ) from exc
        except OSError as exc:
            raise ProviderError(NAME, ProviderErrorKind.NETWORK, str(exc)) from exc

        messages: queue.Queue[str | None] = queue.Queue()
        threading.Thread(
            target=self._read_stdout, args=(process, messages), daemon=True
        ).start()
        try:
            deadline = time.monotonic() + self.timeout
            self._request(
                process,
                messages,
                1,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "quiet-memo", "version": "0.3.2"},
                },
                deadline,
            )
            self._notify(process, "notifications/initialized", {})
            response = self._request(
                process,
                messages,
                2,
                "tools/call",
                {
                    "name": "search",
                    "arguments": arguments,
                },
                deadline,
            )
            return self._parse_result(response)
        except TimeoutError as exc:
            raise ProviderError(
                NAME, ProviderErrorKind.TIMEOUT, f"open-websearch MCP 超时（{self.timeout:.0f}s）"
            ) from exc
        except ProviderError:
            raise
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                NAME, ProviderErrorKind.BAD_RESPONSE, f"无法解析 open-websearch MCP 响应：{exc}"
            ) from exc
        finally:
            self._stop(process)

    def _command_line(self) -> list[str]:
        return npx_command(self.command, self.args)

    @staticmethod
    def _read_stdout(process: subprocess.Popen, messages: queue.Queue) -> None:
        if process.stdout is None:
            messages.put(None)
            return
        try:
            for line in process.stdout:
                messages.put(line)
        finally:
            messages.put(None)

    @staticmethod
    def _send(process: subprocess.Popen, message: dict) -> None:
        if process.stdin is None:
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "MCP stdin 不可用")
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def _notify(self, process: subprocess.Popen, method: str, params: dict) -> None:
        self._send(process, {"jsonrpc": "2.0", "method": method, "params": params})

    def _request(
        self,
        process: subprocess.Popen,
        messages: queue.Queue,
        request_id: int,
        method: str,
        params: dict,
        deadline: float,
    ) -> dict:
        self._send(
            process,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                line = messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError from exc
            if line is None:
                raise ProviderError(NAME, ProviderErrorKind.UNAVAILABLE, "open-websearch MCP 进程已退出")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # 启动横幅等非协议输出不应影响 JSON-RPC 消息。
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message.get("error") or {}
                detail = _text(error.get("message")) if isinstance(error, dict) else str(error)
                raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, detail or "MCP 调用失败")
            return message

    @staticmethod
    def _parse_result(message: dict) -> list[dict]:
        result = message.get("result")
        if not isinstance(result, dict):
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "MCP 响应缺少 result")
        structured = result.get("structuredContent")
        payload = structured if isinstance(structured, dict) else None
        if payload is None:
            content = result.get("content")
            if not isinstance(content, list) or not content:
                raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "MCP 响应缺少 content")
            text = _text(content[0].get("text")) if isinstance(content[0], dict) else ""
            payload = json.loads(text)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "MCP 响应缺少 results")
        return [item for item in results if isinstance(item, dict)]

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass


class OpenWebSearchProvider:
    name = NAME
    capabilities = {"search"}

    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        transport: str = "http",
        client_factory: ClientFactory | None = None,
        mcp_command: str = "npx",
        mcp_args: tuple[str, ...] = ("-y", "open-websearch@latest"),
        engines: list[str] | tuple[str, ...] | None = None,
        fetch_timeout: float = 45.0,
    ):
        if transport == "mcp":
            self.transport: HttpTransport | McpStdioTransport = McpStdioTransport(
                timeout=timeout, command=mcp_command, args=mcp_args, engines=engines
            )
            # MCP 传输暂未接 fetchWebContent，只暴露搜索能力。
            self.capabilities = {"search"}
        else:
            self.transport = HttpTransport(
                base_url,
                timeout=timeout,
                client_factory=client_factory,
                engines=engines,
                fetch_timeout=fetch_timeout,
            )
            self.capabilities = {"search", "fetch"}

    def search(self, query: SearchQuery) -> list[SearchResult]:
        try:
            raw = self.transport.search(query.query, query.limit, query.engines or None)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(NAME, ProviderErrorKind.NETWORK, str(exc)) from exc
        results = (_map_result(item) for item in raw)
        return [result for result in results if result is not None]

    def fetch(self, query: FetchQuery) -> FetchResult:
        if "fetch" not in self.capabilities:
            raise ProviderError(
                NAME, ProviderErrorKind.UNAVAILABLE, "当前 transport 不支持网页抓取，请改用 http"
            )
        try:
            data = self.transport.fetch(query.url, query.max_chars)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(NAME, ProviderErrorKind.NETWORK, str(exc)) from exc
        content = _text(data.get("content"))
        if not content:
            raise ProviderError(NAME, ProviderErrorKind.BAD_RESPONSE, "open-websearch 未返回正文")
        final_url = _text(data.get("finalUrl")) or query.url
        return FetchResult(
            url=query.url,
            final_url=final_url,
            title=_text(data.get("title")) or final_url,
            content=content,
            truncated=bool(data.get("truncated")),
            site_name=_text(data.get("siteName")),
        )
