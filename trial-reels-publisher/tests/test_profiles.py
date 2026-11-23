"""--account test / personal credential profiles and per-account duplicate scope."""
import os
import shutil
import sqlite3
import stat
import subprocess

import pytest
import requests

from conftest import IG_ID, FakeResponse, fake_mp4, happy_session
from trial_reels import cli
from trial_reels.config import load_config
from trial_reels.errors import ConfigError, StateError
from trial_reels.meta_client import MetaClient
from trial_reels.remux import Remuxer
from trial_reels.state import SCHEMA_VERSION, StateStore

pytestmark = pytest.mark.usefixtures("legacy_resumable_trial")  # see conftest

TEST_ID, TEST_TOKEN = IG_ID, "EAATESTACCOUNTTOKEN000000000000000000000001"
PERS_ID, PERS_TOKEN = "17841400000000002", "EAAPERSONALACCOUNTTOKEN00000000000000000002"
LEGACY_TOKEN = "EAALEGACYDOTENVTOKEN0000000000000000000000003"
SHELL_TOKEN = "EAASHELLEXPORTEDTOKEN000000000000000000000004"


# ------------------------------------------------------------------ fixtures / helpers
@pytest.fixture
def root(tmp_path):
    r = tmp_path / "project"
    r.mkdir()
    return r


def write_env(path, ig_id, token, mode=0o600, extra=""):
    path.write_text(f"IG_USER_ID={ig_id}\nIG_ACCESS_TOKEN={token}\n{extra}")
    os.chmod(path, mode)
    return path


@pytest.fixture
def profiles(root):
    write_env(root / ".env.test", TEST_ID, TEST_TOKEN)
    write_env(root / ".env.personal", PERS_ID, PERS_TOKEN)
    return root


@pytest.fixture
def base_env(tmp_path, videos_dir):
    return {"TRIAL_REELS_DIR": str(videos_dir), "TRIAL_REELS_DATA_DIR": str(tmp_path / "data"),
            "TRIAL_REELS_LOG_DIR": str(tmp_path / "logs"), "POLL_INTERVAL_SECONDS": "0",
            "MAX_RETRIES": "1",
            # credentials exported in the shell must never leak into a profile
            "IG_USER_ID": "17841499999999999", "IG_ACCESS_TOKEN": SHELL_TOKEN}


@pytest.fixture
def video(videos_dir):
    p = videos_dir / "IMG_3813.mp4"
    p.write_bytes(fake_mp4())
    return p


class Accounts:
    """Maps a token to the Instagram account Meta would say it belongs to."""

    def __init__(self, **overrides):
        self.sessions = {
            TEST_TOKEN: happy_session(ig_id=TEST_ID, username="testacct"),
            PERS_TOKEN: happy_session(ig_id=PERS_ID, username="nash.personal", media_id="2800"),
        }
        self.sessions.update(overrides)
        self.used = []

    def factory(self, config):
        s = self.sessions[config.access_token]
        self.used.append(s)
        return MetaClient(config, session=s, sleep=lambda x: None)

    def writes(self, token):
        return [c for c in self.sessions[token].calls if c["method"] == "POST"]

    def all_calls(self):
        return [c for s in self.sessions.values() for c in s.calls]


class Answers:
    def __init__(self, *a):
        self.a, self.prompts = list(a), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.a.pop(0)


def run(root, base_env, accounts, *argv, answers=None, tty=False, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: tty, raising=False)
    loader = lambda profile=None: load_config(profile=profile, root=root, base_env=base_env)  # noqa: E731
    return cli.main(list(argv), config_loader=loader, client_factory=accounts.factory,
                    input_fn=answers or Answers(), remuxer_factory=lambda c: Remuxer(c.tmp_dir, ffmpeg=None))


def store_for(base_env):
    from pathlib import Path
    return StateStore(Path(base_env["TRIAL_REELS_DATA_DIR"]) / "state.sqlite3")


# ------------------------------------------------------------------ profile selection
def test_profile_test_and_personal_load_their_own_file(profiles, base_env):
    t = load_config(profile="test", root=profiles, base_env=base_env)
    p = load_config(profile="personal", root=profiles, base_env=base_env)
    assert (t.ig_user_id, t.access_token, t.profile, t.credentials_file.name) == (TEST_ID, TEST_TOKEN, "test", ".env.test")
    assert (p.ig_user_id, p.access_token, p.profile) == (PERS_ID, PERS_TOKEN, "personal")
    assert str(t.videos_dir) == base_env["TRIAL_REELS_DIR"] == str(p.videos_dir)  # same source folder


def test_profile_ignores_dotenv_and_shell_credentials(profiles, base_env):
    write_env(profiles / ".env", "17841400000000009", LEGACY_TOKEN)
    cfg = load_config(profile="personal", root=profiles, base_env=base_env)
    assert cfg.access_token == PERS_TOKEN and cfg.ig_user_id == PERS_ID
    assert LEGACY_TOKEN not in repr(cfg) and SHELL_TOKEN != cfg.access_token


def test_no_account_flag_keeps_legacy_dotenv(root, monkeypatch, tmp_path, videos_dir):
    write_env(root / ".env", TEST_ID, LEGACY_TOKEN, extra=f"TRIAL_REELS_DIR={videos_dir}\n"
              f"TRIAL_REELS_DATA_DIR={tmp_path / 'data'}\n")
    env = {k: v for k, v in os.environ.items() if k not in ("IG_USER_ID", "IG_ACCESS_TOKEN")}
    monkeypatch.setattr(os, "environ", env)
    cfg = load_config(root=root)
    assert cfg.access_token == LEGACY_TOKEN and cfg.profile is None and cfg.credentials_file.name == ".env"


@pytest.mark.parametrize("profile", ["test", "personal"])
def test_missing_profile_file_never_falls_back(root, base_env, profile):
    write_env(root / ".env", TEST_ID, LEGACY_TOKEN)  # a perfectly good .env must NOT be used
    with pytest.raises(ConfigError, match=rf"needs \.env\.{profile}.*does not exist.*Nothing else was used"):
        load_config(profile=profile, root=root, base_env=base_env)


def test_missing_profile_cli_exits_without_any_request(root, base_env, video):
    write_env(root / ".env.test", TEST_ID, TEST_TOKEN)
    acc = Accounts()
    assert run(root, base_env, acc, "--account", "personal", "--yes") == 2
    assert acc.all_calls() == []


@pytest.mark.parametrize("content,msg", [
    ("IG_USER_ID=YOUR_IG_PROFESSIONAL_ID\nIG_ACCESS_TOKEN=PASTE_LOCALLY_ONLY\n", "IG_USER_ID, IG_ACCESS_TOKEN"),
    (f"IG_USER_ID={PERS_ID}\nIG_ACCESS_TOKEN=\n", "IG_ACCESS_TOKEN"),
    (f"IG_ACCESS_TOKEN={PERS_TOKEN}\n", "IG_USER_ID"),
    (f"IG_USER_ID=nash.personal\nIG_ACCESS_TOKEN={PERS_TOKEN}\n", "numeric"),
])
def test_invalid_profile_contents(root, base_env, content, msg):
    p = root / ".env.personal"
    p.write_text(content)
    os.chmod(p, 0o600)
    with pytest.raises(ConfigError, match=msg):
        load_config(profile="personal", root=root, base_env=base_env)


def test_unknown_profile_name_rejected(root, base_env):
    with pytest.raises(SystemExit):
        cli.parse_args(["--account", "work"])
    with pytest.raises(ConfigError, match="Unknown account profile"):
        load_config(profile="work", root=root, base_env=base_env)


def test_symlinked_profile_rejected(root, base_env, tmp_path):
    real = write_env(tmp_path / "elsewhere.env", PERS_ID, PERS_TOKEN)
    (root / ".env.personal").symlink_to(real)
    with pytest.raises(ConfigError, match="regular file"):
        load_config(profile="personal", root=root, base_env=base_env)


def test_cli_refuses_config_from_other_source(profiles, base_env, video):
    acc = Accounts()
    wrong = lambda profile=None: load_config(profile="test", root=profiles, base_env=base_env)  # noqa: E731
    rc = cli.main(["--account", "personal", "--check-auth"], config_loader=wrong,
                  client_factory=acc.factory, remuxer_factory=lambda c: Remuxer(c.tmp_dir, ffmpeg=None))
    assert rc == 2 and acc.all_calls() == []


# ------------------------------------------------------------------ permissions
@pytest.mark.parametrize("name", [".env.test", ".env.personal", ".env"])
def test_permissive_credentials_file_tightened_and_warned(root, base_env, name, capsys, monkeypatch):
    content_before = None
    p = write_env(root / name, PERS_ID if name != ".env.test" else TEST_ID,
                  PERS_TOKEN if name != ".env.test" else TEST_TOKEN, mode=0o644)
    content_before = p.read_bytes()
    if name == ".env":
        monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items()
                                            if k not in ("IG_USER_ID", "IG_ACCESS_TOKEN")})
        load_config(root=root)
    else:
        load_config(profile=name.split(".")[-1], root=root, base_env=base_env)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert "permissions tightened to 600" in capsys.readouterr().err
    assert p.read_bytes() == content_before  # contents never rewritten


def test_private_file_no_warning(profiles, base_env, capsys):
    load_config(profile="test", root=profiles, base_env=base_env)
    assert "tightened" not in capsys.readouterr().err


def test_profiles_pointing_at_same_account_warns(root, base_env, capsys):
    write_env(root / ".env.test", TEST_ID, TEST_TOKEN)
    write_env(root / ".env.personal", TEST_ID, PERS_TOKEN)
    load_config(profile="personal", root=root, base_env=base_env)
    assert "same Instagram account" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_gitignore_covers_all_credential_files(tmp_path):
    from pathlib import Path
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy(Path(__file__).resolve().parents[1] / ".gitignore", repo / ".gitignore")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    for name in (".env", ".env.test", ".env.personal", ".env.local", ".env.example"):
        (repo / name).write_text("x")
    ignored = subprocess.run(["git", "check-ignore", ".env", ".env.test", ".env.personal", ".env.local",
                              ".env.example"], cwd=repo, capture_output=True, text=True).stdout.split()
    assert set(ignored) == {".env", ".env.test", ".env.personal", ".env.local"}


# ------------------------------------------------------------------ account verification
@pytest.mark.parametrize("profile,handle,ig", [("test", "@testacct", TEST_ID), ("personal", "@nash.personal", PERS_ID)])
def test_check_auth_per_profile(profiles, base_env, capsys, profile, handle, ig):
    acc = Accounts()
    assert run(profiles, base_env, acc, "--account", profile, "--check-auth") == 0
    out = capsys.readouterr().out
    assert f"{handle} (IG ID {ig})" in out and f"profile '{profile}' (.env.{profile})" in out
    assert not [c for c in acc.all_calls() if c["method"] == "POST"]


def mismatched_personal():
    """Token in .env.personal actually belongs to the test account."""
    s = happy_session(ig_id=TEST_ID)
    s.routes.insert(0, ["GET", f"/{PERS_ID}", [FakeResponse(200, {"id": TEST_ID, "username": "testacct"})]])
    return s


def test_account_mismatch_check_auth(profiles, base_env, capsys):
    acc = Accounts(**{PERS_TOKEN: mismatched_personal()})
    assert run(profiles, base_env, acc, "--account", "personal", "--check-auth") == 2
    assert "Account mismatch" in capsys.readouterr().err


@pytest.mark.parametrize("extra", [(), ("--public",)])
def test_account_mismatch_aborts_before_container(profiles, base_env, video, monkeypatch, extra, capsys):
    acc = Accounts(**{PERS_TOKEN: mismatched_personal()})
    rc = run(profiles, base_env, acc, "--account", "personal", "--yes", *extra,
             answers=Answers("PUBLIC"), tty=True, monkeypatch=monkeypatch)
    assert rc == 2 and acc.writes(PERS_TOKEN) == []
    err = capsys.readouterr().err
    assert "Account mismatch" in err and "no media container was created" in err


def test_identity_reverified_right_before_container(profiles, base_env, video):
    """Preflight passes, then the token resolves to another account: still no container."""
    s = happy_session(ig_id=PERS_ID, username="nash.personal")
    s.routes.insert(0, ["GET", f"/{PERS_ID}", [
        FakeResponse(200, {"id": PERS_ID, "username": "nash.personal"}),
        FakeResponse(200, {"id": TEST_ID, "username": "testacct"})]])
    acc = Accounts(**{PERS_TOKEN: s})
    assert run(profiles, base_env, acc, "--account", "personal", "--yes") == 2
    assert acc.writes(PERS_TOKEN) == []
    assert sum(c["url"].endswith(f"/{PERS_ID}") for c in s.calls) == 2


def test_summary_before_confirmation(profiles, base_env, video, monkeypatch, capsys):
    acc = Accounts()
    seen = {}

    def answer(prompt):
        seen["out"] = capsys.readouterr().out
        seen["writes"] = list(acc.writes(PERS_TOKEN))
        return "post"
    assert run(profiles, base_env, acc, "--account", "personal", answers=answer, tty=True,
               monkeypatch=monkeypatch) == 0
    out = seen["out"]
    assert "About to publish:" in out and "@nash.personal (IG ID 17841400000000002)" in out
    assert "File:         IMG_3813.mp4" in out and "Posting mode: TRIAL / MANUAL" in out
    assert "profile 'personal' (.env.personal)" in out
    assert seen["writes"] == []  # nothing written before confirmation


def test_declined_confirmation_posts_nothing(profiles, base_env, video, monkeypatch):
    acc = Accounts()
    assert run(profiles, base_env, acc, "--account", "test", answers=Answers("no"), tty=True,
               monkeypatch=monkeypatch) == 1
    assert acc.writes(TEST_TOKEN) == []


# ------------------------------------------------------------------ both modes, per profile
def test_trial_on_personal(profiles, base_env, video):
    acc = Accounts()
    assert run(profiles, base_env, acc, "--account", "personal", "--yes") == 0
    create = next(c for c in acc.writes(PERS_TOKEN) if c["url"].endswith(f"{PERS_ID}/media"))
    assert "trial_params" in create["data"]
    rec = store_for(base_env).all()[0]
    assert (rec.account_id, rec.account_profile, rec.account_username, rec.publish_mode) == \
        (PERS_ID, "personal", "nash.personal", "TRIAL")


def test_public_on_test(profiles, base_env, video, monkeypatch):
    acc = Accounts()
    assert run(profiles, base_env, acc, "--account", "test", "--public", answers=Answers("post", "PUBLIC"),
               tty=True, monkeypatch=monkeypatch) == 0
    create = next(c for c in acc.writes(TEST_TOKEN) if c["url"].endswith(f"{TEST_ID}/media"))
    assert "trial_params" not in create["data"]
    rec = store_for(base_env).all()[0]
    assert (rec.account_id, rec.account_profile, rec.publish_mode) == (TEST_ID, "test", "PUBLIC")


def test_public_dry_run_with_profile(profiles, base_env, video, capsys):
    acc = Accounts()
    assert run(profiles, base_env, acc, "--account", "test", "--public", "--dry-run") == 0
    out = capsys.readouterr().out
    assert f"POST https://graph.facebook.com/v27.0/{TEST_ID}/media" in out and "trial_params: OMITTED" in out
    assert acc.writes(TEST_TOKEN) == []


def test_personal_dry_run(profiles, base_env, video, capsys):
    acc = Accounts()
    assert run(profiles, base_env, acc, "--account", "personal", "--dry-run") == 0
    out = capsys.readouterr().out
    assert f"/{PERS_ID}/media" in out and "trial_params=" in out and "@nash.personal" in out
    assert [c for c in acc.all_calls() if c["method"] == "POST"] == []


def test_trial_rejection_on_personal_never_falls_back(profiles, base_env, video, capsys):
    s = happy_session(ig_id=PERS_ID, username="nash.personal")
    s.routes.insert(0, ["POST", f"/{PERS_ID}/media", [FakeResponse(400, {"error": {
        "message": "The instagram account does not meet the trial reel follower requirement to create "
                   "trial reels.", "type": "OAuthException", "code": 10, "error_subcode": 2207081}})]])
    acc = Accounts(**{PERS_TOKEN: s})
    assert run(profiles, base_env, acc, "--account", "personal", "--yes") == 2
    posts = acc.writes(PERS_TOKEN)
    assert len(posts) == 1 and "trial_params" in posts[0]["data"]
    err = capsys.readouterr().err
    assert "not eligible for Trial Reels" in err and "NOT retry as an ordinary Reel" in err


# ------------------------------------------------------------------ per-account duplicate scope
def test_same_video_once_per_account(profiles, base_env, video, monkeypatch, capsys):
    acc = Accounts()
    assert run(profiles, base_env, acc, "--account", "test", "--yes") == 0
    assert run(profiles, base_env, acc, "--account", "personal", "--yes") == 0      # other account: allowed
    capsys.readouterr()

    again = Accounts()
    assert run(profiles, base_env, again, "--account", "test", "--yes") == 0       # same: already published
    assert "Already published" in capsys.readouterr().out
    for profile, token in (("test", TEST_TOKEN), ("personal", PERS_TOKEN)):        # other mode: blocked
        assert run(profiles, base_env, again, "--account", profile, "--public", answers=Answers(),
                   tty=True, monkeypatch=monkeypatch) == 2
        assert again.writes(token) == []
    assert "as a TRIAL Reel" in capsys.readouterr().err
    recs = store_for(base_env).all()
    assert sorted((r.account_id, r.publish_mode, r.state) for r in recs) == [
        (TEST_ID, "TRIAL", "PUBLISHED"), (PERS_ID, "TRIAL", "PUBLISHED")]


def test_public_then_trial_same_account_blocked_other_account_allowed(profiles, base_env, video, monkeypatch):
    acc = Accounts()
    assert run(profiles, base_env, acc, "--account", "test", "--public", answers=Answers("post", "PUBLIC"),
               tty=True, monkeypatch=monkeypatch) == 0
    assert run(profiles, base_env, Accounts(), "--account", "test", "--yes") == 2
    assert run(profiles, base_env, Accounts(), "--account", "personal", "--public",
               answers=Answers("post", "PUBLIC"), tty=True, monkeypatch=monkeypatch) == 0


def test_unknown_state_blocks_only_its_account(profiles, base_env, video, capsys):
    s = happy_session(ig_id=TEST_ID)
    s.routes.insert(0, ["POST", "/media_publish", [requests.ReadTimeout("t")]])
    assert run(profiles, base_env, Accounts(**{TEST_TOKEN: s}), "--account", "test", "--yes") == 3
    blocked = Accounts()
    assert run(profiles, base_env, blocked, "--account", "test", "--yes") == 3      # still needs reconcile
    assert blocked.writes(TEST_TOKEN) == []
    capsys.readouterr()
    assert run(profiles, base_env, Accounts(), "--account", "personal", "--reconcile") == 0
    assert "No unresolved publication attempts for IG ID 17841400000000002" in capsys.readouterr().out
    assert run(profiles, base_env, Accounts(), "--account", "personal", "--yes") == 0  # other account OK


def test_begin_publish_atomic_guard(tmp_path):
    st = StateStore(tmp_path / "s.db")
    for key, path in (("a", "/v/a.mp4"), ("b", "/v/b.mp4")):
        st.insert(job_key=key, account_id=TEST_ID, source_path=path, relative_path=path, content_hash="h",
                  size_bytes=1, graduation_strategy="MANUAL")
    st.update("a", "PUBLISHED", media_id="1")
    st.update("b", "READY")
    with pytest.raises(StateError, match="already"):
        st.begin_publish("b")
    st.insert(job_key="c", account_id=PERS_ID, source_path="/v/a.mp4", relative_path="a", content_hash="h",
              size_bytes=1, graduation_strategy="MANUAL")
    st.update("c", "READY")
    assert st.begin_publish("c").state == "PUBLISHING"  # different account: allowed


# ------------------------------------------------------------------ unknown-account records
def insert_unknown_account_row(db_path, content_hash, account_id=""):
    st = StateStore(db_path)
    st.conn.execute(
        "INSERT INTO jobs (job_key, account_id, source_path, relative_path, content_hash, size_bytes, "
        "publish_mode, graduation_strategy, state, media_id, created_at, updated_at) "
        "VALUES ('legacy', ?, '/old/IMG.mp4', 'IMG.mp4', ?, 1, 'TRIAL', 'MANUAL', 'PUBLISHED', '999', 't', 't')",
        (account_id, content_hash))
    st.close()


@pytest.mark.parametrize("bad_id", ["", "  ", "unknown", "None"])
def test_unknown_account_record_blocks_every_account(profiles, base_env, video, bad_id, capsys):
    import hashlib
    from pathlib import Path
    db = Path(base_env["TRIAL_REELS_DATA_DIR"]) / "state.sqlite3"
    insert_unknown_account_row(db, hashlib.sha256(video.read_bytes()).hexdigest(), bad_id)
    for profile, token in (("test", TEST_TOKEN), ("personal", PERS_TOKEN)):
        acc = Accounts()
        assert run(profiles, base_env, acc, "--account", profile, "--yes") == 2
        assert acc.all_calls() == []
        assert "no valid Instagram account ID" in capsys.readouterr().err
    assert StateStore(db).get("legacy").state == "PUBLISHED"  # preserved untouched
    assert run(profiles, base_env, Accounts(), "--account", "test", "--reconcile") == 0
    assert "blocks that video on every account" in capsys.readouterr().out


def test_unknown_account_record_does_not_block_other_videos(profiles, base_env, video):
    from pathlib import Path
    insert_unknown_account_row(Path(base_env["TRIAL_REELS_DATA_DIR"]) / "state.sqlite3", "f" * 64)
    assert run(profiles, base_env, Accounts(), "--account", "personal", "--yes") == 0


# ------------------------------------------------------------------ migration
V2_SCHEMA = """CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_key TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL, source_path TEXT NOT NULL, relative_path TEXT NOT NULL,
    content_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL,
    publish_mode TEXT NOT NULL DEFAULT 'TRIAL' CHECK (publish_mode IN ('TRIAL', 'PUBLIC')),
    graduation_strategy TEXT, scheduled_for TEXT, state TEXT NOT NULL, container_id TEXT, upload_uri TEXT,
    media_id TEXT, permalink TEXT, last_error TEXT, publish_attempts INTEGER NOT NULL DEFAULT 0,
    publish_started_at TEXT, upload_sha256 TEXT, upload_size_bytes INTEGER,
    upload_remuxed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (account_id, content_hash, source_path, publish_mode));
CREATE INDEX jobs_state ON jobs(state);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_key TEXT NOT NULL, at TEXT NOT NULL,
    state TEXT NOT NULL, detail TEXT);"""


def make_v2_db(path, content_hash):
    """Same shape as the real data/state.sqlite3 before profiles: a FAILED trial and a PUBLISHED public."""
    c = sqlite3.connect(path)
    c.executescript(V2_SCHEMA)
    c.execute("INSERT INTO jobs (job_key, account_id, source_path, relative_path, content_hash, size_bytes, "
              "publish_mode, graduation_strategy, state, last_error, upload_remuxed, created_at, updated_at) "
              "VALUES ('k1', ?, '/v/IMG_3813.mp4', 'IMG_3813.mp4', ?, 1, 'TRIAL', 'MANUAL', 'FAILED', "
              "'follower requirement', 1, 't1', 't1')", (TEST_ID, content_hash))
    c.execute("INSERT INTO jobs (job_key, account_id, source_path, relative_path, content_hash, size_bytes, "
              "publish_mode, state, container_id, media_id, permalink, upload_remuxed, created_at, updated_at) "
              "VALUES ('k2', ?, '/v/IMG_3813.mp4', 'IMG_3813.mp4', ?, 1, 'PUBLIC', 'PUBLISHED', '183', "
              "'18108325910203954', 'https://www.instagram.com/reel/x/', 1, 't2', 't2')", (TEST_ID, content_hash))
    for i in range(12):
        c.execute("INSERT INTO events (job_key, at, state, detail) VALUES ('k2', 't', 'X', ?)", (str(i),))
    c.commit()
    c.close()


def test_migration_preserves_history_and_makes_backup(tmp_path):
    db = tmp_path / "state.sqlite3"
    make_v2_db(db, "h" * 64)
    before = sqlite3.connect(db).execute("SELECT * FROM jobs ORDER BY id").fetchall()
    st = StateStore(db)
    assert st.backup_path is not None and st.backup_path.exists()
    assert stat.S_IMODE(st.backup_path.stat().st_mode) == 0o600
    assert sqlite3.connect(st.backup_path).execute("SELECT * FROM jobs ORDER BY id").fetchall() == before
    recs = {r.job_key: r for r in st.all()}
    assert recs["k2"].state == "PUBLISHED" and recs["k2"].media_id == "18108325910203954"
    assert recs["k2"].account_id == TEST_ID and recs["k2"].account_profile is None
    assert recs["k1"].state == "FAILED" and recs["k1"].last_error == "follower requirement"
    # 12 original events kept + 1 appended by the v4 migration (k1 annotated as pre-publication failure)
    assert st.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 13
    assert recs["k1"].failure_phase == "PRE_PUBLISH"
    assert st.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    st.close()
    st2 = StateStore(db)                       # second open: no further migration, no new backup
    assert st2.backup_path is None
    assert len(list(tmp_path.glob("state.sqlite3.bak-*"))) == 1


def test_no_backup_for_new_empty_db(tmp_path):
    st = StateStore(tmp_path / "new.sqlite3")
    assert st.backup_path is None and list(tmp_path.glob("*.bak-*")) == []


def test_migrated_db_scopes_duplicates_per_account(profiles, base_env, video, monkeypatch, capsys):
    import hashlib
    from pathlib import Path
    db = Path(base_env["TRIAL_REELS_DATA_DIR"]) / "state.sqlite3"
    db.parent.mkdir(parents=True)
    make_v2_db(db, hashlib.sha256(video.read_bytes()).hexdigest())
    # test account already has it (PUBLIC) -> blocked in both modes
    acc = Accounts()
    assert run(profiles, base_env, acc, "--account", "test", "--yes") == 2
    assert run(profiles, base_env, acc, "--account", "test", "--public", answers=Answers(), tty=True,
               monkeypatch=monkeypatch) == 2
    assert acc.writes(TEST_TOKEN) == []
    assert "18108325910203954" in capsys.readouterr().err
    # personal account never had it -> allowed once
    assert run(profiles, base_env, Accounts(), "--account", "personal", "--yes") == 0
    assert run(profiles, base_env, Accounts(), "--account", "personal", "--yes") == 0  # 2nd: already published
    pers = [r for r in store_for(base_env).all() if r.account_id == PERS_ID]
    assert len(pers) == 1 and pers[0].state == "PUBLISHED"


# ------------------------------------------------------------------ secrecy
def test_tokens_never_printed_logged_or_stored(profiles, base_env, video, capsys):
    from pathlib import Path
    s = happy_session(ig_id=PERS_ID, username="nash.personal")
    s.routes.insert(0, ["POST", f"/{PERS_ID}/media", [FakeResponse(400, {"error": {
        "message": f"bad token {PERS_TOKEN} access_token={PERS_TOKEN}", "code": 100}})]])
    run(profiles, base_env, Accounts(**{PERS_TOKEN: s}), "--account", "personal", "--yes")
    run(profiles, base_env, Accounts(), "--account", "test", "--yes")
    import logging
    logging.shutdown()
    cap = capsys.readouterr()
    logs = "".join(p.read_text() for p in Path(base_env["TRIAL_REELS_LOG_DIR"]).glob("*.log"))
    db = (Path(base_env["TRIAL_REELS_DATA_DIR"]) / "state.sqlite3").read_bytes().decode("latin-1")
    for text in (cap.out, cap.err, logs, db):
        for tok in (TEST_TOKEN, PERS_TOKEN, SHELL_TOKEN):
            assert tok not in text
