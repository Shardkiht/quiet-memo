from __future__ import annotations

import pytest

from quiet_memo.infra.config import Config, LLMConfig
from quiet_memo.memory.extractor import StaticExtractor
from quiet_memo.memory.models import Candidate, ExtractionResult
from quiet_memo.memory.service import MemoryService


@pytest.fixture
def config(tmp_path):
    return Config(vault=tmp_path / "vault", llm=LLMConfig())


@pytest.fixture
def service(config):
    return MemoryService(config)


def extractor(*candidates: Candidate) -> StaticExtractor:
    return StaticExtractor(ExtractionResult(memories=list(candidates)))
