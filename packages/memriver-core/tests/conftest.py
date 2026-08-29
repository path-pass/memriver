import pytest
from memriver_core.store import MemoryStore


@pytest.fixture
def store(tmp_path) -> MemoryStore:
    return MemoryStore(tmp_path / "mem")
