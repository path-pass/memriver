"""Import-level smoke test over the packaged tree."""

import memriver_core
from memriver_core.application.service import MemoryService
from memriver_core.bootstrap import build_service
from memriver_core.content_policy.secret_scanner import SecretScanner
from memriver_core.models import Memory
from memriver_core.repository.sqlite import SqliteMemoryStore, SqliteProjectStore
from memriver_core.settings import Settings


def test_version():
    assert memriver_core.__version__ == "0.1.0"


def test_every_layer_imports(tmp_path):
    assert isinstance(build_service(Settings(root=tmp_path)), MemoryService)
    assert SecretScanner() and Memory
    assert SqliteMemoryStore(tmp_path, busy_timeout_ms=1000)
    assert SqliteProjectStore(tmp_path, home=tmp_path, busy_timeout_ms=1000)
