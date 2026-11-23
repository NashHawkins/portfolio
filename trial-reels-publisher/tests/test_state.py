import multiprocessing as mp
import time

import pytest

from trial_reels.errors import LockError, StateError
from trial_reels.state import StateStore, process_lock


def new(store, key="k1"):
    return store.insert(job_key=key, account_id="1", source_path="/v/a.mp4", relative_path="a.mp4",
                        content_hash="h", size_bytes=10, graduation_strategy="MANUAL")


def test_insert_and_unique(tmp_path):
    s = StateStore(tmp_path / "s.db")
    new(s)
    with pytest.raises(StateError):
        new(s)
    with pytest.raises(StateError):  # same account/hash/path, different key
        new(s, key="k2")


def test_state_persists_across_connections(tmp_path):
    s = StateStore(tmp_path / "s.db")
    new(s)
    s.update("k1", "READY", container_id="900")
    s.close()
    r = StateStore(tmp_path / "s.db").get("k1")
    assert r.state == "READY" and r.container_id == "900"


def test_begin_publish_only_from_ready(tmp_path):
    s = StateStore(tmp_path / "s.db")
    new(s)
    with pytest.raises(StateError):
        s.begin_publish("k1")
    s.update("k1", "READY")
    r = s.begin_publish("k1")
    assert r.state == "PUBLISHING" and r.publish_attempts == 1 and r.publish_started_at
    with pytest.raises(StateError):  # second concurrent attempt loses the compare-and-set
        s.begin_publish("k1")


def test_error_text_redacted(tmp_path):
    s = StateStore(tmp_path / "s.db")
    new(s)
    s.update("k1", "FAILED", failure_phase="PRE_PUBLISH",
             last_error="bad url ?access_token=EAAsecretsecretsecretsecretsecret123")
    assert "EAAsecret" not in s.get("k1").last_error


def _hold_lock(path, ready, release):
    with process_lock(path):
        ready.set()
        release.wait(10)


def test_process_lock_excludes_second_process(tmp_path):
    path = tmp_path / "publish.lock"
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold_lock, args=(path, ready, release))
    p.start()
    try:
        assert ready.wait(10)
        with pytest.raises(LockError, match="Another publish.py run"):
            with process_lock(path):
                pass
    finally:
        release.set()
        p.join(10)
    with process_lock(path):  # free again afterwards
        pass
