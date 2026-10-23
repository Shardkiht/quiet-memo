from __future__ import annotations

import sys
import types

import pytest

from quiet_memo.infra.config import Config, LLMConfig
from quiet_memo.memory.extractor import StaticExtractor
from quiet_memo.memory.models import Candidate, ExtractionResult
from quiet_memo.memory.service import MemoryService


@pytest.fixture(autouse=True)
def isolate_llm_credentials(monkeypatch):
    """测试禁止读取本机 keyring / 真实 Key，避免误调模型和产生费用。"""
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service, name: None
    fake.set_password = lambda service, name, value: None
    monkeypatch.setitem(sys.modules, "keyring", fake)
    for name in ("DEEPSEEK_API_KEY", "SILICONFLOW_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config(tmp_path):
    return Config(vault=tmp_path / "vault", llm=LLMConfig())


@pytest.fixture
def service(config):
    return MemoryService(config)


def extractor(*candidates: Candidate) -> StaticExtractor:
    return StaticExtractor(ExtractionResult(memories=list(candidates)))
