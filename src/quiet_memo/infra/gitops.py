from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


class GitRepo:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _run(self, args: list[str], check: bool = False) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and completed.returncode != 0:
            raise GitError(completed.stderr.strip() or completed.stdout.strip() or "git 命令失败")
        return completed

    @property
    def is_repo(self) -> bool:
        return (self.path / ".git").exists()

    def ensure_init(self) -> bool:
        self.path.mkdir(parents=True, exist_ok=True)
        if self.is_repo:
            return False
        completed = self._run(["init"], check=True)
        if completed.returncode != 0:
            raise GitError(completed.stderr.strip())
        return True

    def _identity_args(self) -> list[str]:
        name = self._run(["config", "user.name"]).stdout.strip()
        email = self._run(["config", "user.email"]).stdout.strip()
        args: list[str] = []
        if not name:
            args += ["-c", "user.name=quiet-memo"]
        if not email:
            args += ["-c", "user.email=quiet-memo@localhost"]
        return args

    def status_short(self) -> str:
        return self._run(["status", "--short"]).stdout

    def has_staged(self) -> bool:
        completed = self._run(["diff", "--cached", "--quiet"])
        return completed.returncode == 1

    def has_changes(self) -> bool:
        return bool(self.status_short().strip())

    def stage_all(self) -> None:
        self._run(["add", "-A"], check=True)

    def staged_stat(self) -> str:
        return self._run(["diff", "--cached", "--stat"]).stdout

    def staged_diff(self, max_chars: int = 24000) -> str:
        diff = self._run(["diff", "--cached", "--unified=1"]).stdout
        return diff[:max_chars]

    def commit(self, message: str) -> str | None:
        if not self.has_staged():
            return None
        completed = self._run([*self._identity_args(), "commit", "-m", message])
        if completed.returncode != 0:
            raise GitError(completed.stderr.strip() or completed.stdout.strip())
        return self._run(["rev-parse", "--short", "HEAD"]).stdout.strip()

    def history(self, limit: int = 10) -> list[dict[str, str]]:
        if not self.is_repo:
            return []
        completed = self._run(
            ["log", f"-{limit}", "--pretty=format:%h%x1f%ad%x1f%s", "--date=format:%Y-%m-%d %H:%M"]
        )
        result: list[dict[str, str]] = []
        for line in completed.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                result.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
        return result

    def has_remote(self) -> bool:
        return bool(self._run(["remote"]).stdout.strip())

    def push(self) -> str:
        if not self.has_remote():
            raise GitError("未配置 git remote，无法 push。")
        completed = self._run(["push"])
        if completed.returncode != 0:
            raise GitError(completed.stderr.strip() or completed.stdout.strip())
        return completed.stdout.strip() or completed.stderr.strip()
