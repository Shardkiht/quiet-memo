from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .base import ProviderError, ProviderErrorKind
from .open_websearch import NAME, npx_command

START_TIMEOUT = 20.0
HEALTH_TIMEOUT = 1.5
DEFAULT_LOG = Path(tempfile.gettempdir()) / "quiet-memo-open-websearch.log"


def daemon_health(base_url: str, timeout: float = HEALTH_TIMEOUT) -> bool:
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.get(f"{base_url.rstrip('/')}/health")
        if response.status_code != 200:
            return False
        payload = response.json()
        return isinstance(payload, dict) and payload.get("status") == "ok"
    except (httpx.HTTPError, ValueError):
        return False


def ensure_daemon(
    base_url: str,
    *,
    timeout: float = START_TIMEOUT,
    log_path: Path | None = None,
    health_check=None,
    spawn=None,
    sleep=time.sleep,
) -> str:
    """确保 HTTP daemon 可用；返回 running / started，失败抛 ProviderError。"""
    check = health_check or (lambda: daemon_health(base_url))
    if check():
        return "running"

    port = urlsplit(base_url).port or 3000
    target_log = log_path or DEFAULT_LOG

    def default_spawn() -> None:
        command = npx_command("npx", ("-y", "open-websearch@latest")) + [
            "serve",
            "--port",
            str(port),
        ]
        _spawn_daemon(command, target_log)

    (spawn or default_spawn)()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sleep(0.3)
        if check():
            return "started"
    raise ProviderError(
        NAME,
        ProviderErrorKind.TIMEOUT,
        f"open-websearch 启动超时（{timeout:.0f}s），日志：{target_log}",
    )


ensure_search_daemon = ensure_daemon


def _spawn_daemon(command: list[str], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {}
    if os.name == "nt":
        # CREATE_NO_WINDOW 让 npx/cmd 不创建控制台窗口；qm 退出后进程仍独立常驻。
        # 不用 DETACHED_PROCESS：它会让 CREATE_NO_WINDOW 失效，可能重新弹出窗口。
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
    else:
        kwargs["start_new_session"] = True

    try:
        with open(log_path, "ab") as log:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env={**os.environ, "OPEN_WEBSEARCH_QUIET_STARTUP": "true"},
                **kwargs,
            )
    except OSError as exc:
        raise ProviderError(
            NAME,
            ProviderErrorKind.NETWORK,
            f"无法启动 open-websearch：{exc}；日志：{log_path}",
        ) from exc
