"""斜杠命令清单：命令、用法和一句话说明，供命令面板与 /help 共用。"""
from __future__ import annotations

SLASH_COMMANDS = [
    ("new", "/new", "New session"),
    ("sessions", "/sessions", "Browse sessions"),
    ("compact", "/compact", "Compact context"),
    ("undo", "/undo", "Undo and edit last message"),
    ("summary", "/summary", "Show summary"),
    ("status", "/status", "Session status"),
    ("model", "/model [refresh]", "Models & providers"),
    ("think", "/think", "Reasoning effort"),
    ("context", "/context", "Context window"),
    ("setup", "/setup", "Run setup in terminal"),
    ("rename", "/rename <title>", "Rename session"),
    ("import", "/import <path> [--apply]", "导入文件或目录（默认预览）"),
    ("pending", "/pending", "Review pending"),
    ("topics", "/topics [name]", "Browse topics"),
    ("help", "/help", "Show commands"),
    ("exit", "/exit", "Quit"),
    ("quit", "/quit", "Quit"),
]
