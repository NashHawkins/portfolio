"""Configuration loaded from environment variables / a project-local .env file."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from .errors import ConfigError, register_secret

PROJECT_ROOT = Path(__file__).resolve().parents[2]

GRAPH_HOST = "https://graph.facebook.com"  # Facebook Login path. Never graph.instagram.com here.
RUPLOAD_HOST = "rupload.facebook.com"
DEFAULT_API_VERSION = "v27.0"  # latest listed in Meta's IG User Media reference, Nov 2026
VALID_STRATEGIES = ("MANUAL", "SS_PERFORMANCE")
# --account NAME  ->  credentials file in the project root
PROFILES = {"test": ".env.test", "personal": ".env.personal"}
CREDENTIAL_KEYS = ("IG_USER_ID", "IG_ACCESS_TOKEN")

_PLACEHOLDERS = {"YOUR_IG_PROFESSIONAL_ID", "PASTE_LOCALLY_ONLY", ""}


@dataclass(frozen=True)
class Config:
    ig_user_id: Optional[str]
    access_token: Optional[str] = field(repr=False)
    api_version: str = DEFAULT_API_VERSION
    videos_dir: Path = Path.home() / "Documents" / "Trial-Reels"
    graduation_strategy: str = "MANUAL"
    default_caption: str = ""
    timezone: str = "America/Denver"
    log_level: str = "INFO"
    data_dir: Path = PROJECT_ROOT / "data"
    log_dir: Path = PROJECT_ROOT / "logs"
    poll_interval_s: float = 5.0
    poll_timeout_s: float = 300.0
    http_timeout_s: float = 30.0
    upload_timeout_s: float = 600.0
    max_retries: int = 3
    # Daily batch (--batch): the LipSync Worker that lists lipsync/<date>/ in R2, and its read-only token.
    lipsync_worker_url: Optional[str] = None
    lipsync_list_token: Optional[str] = field(default=None, repr=False)
    profile: Optional[str] = None             # "test" / "personal" when --account was used
    credentials_file: Optional[Path] = None   # file IG_USER_ID / IG_ACCESS_TOKEN came from

    @property
    def account_label(self) -> str:
        if self.profile:
            return f"profile '{self.profile}' ({self.credentials_file.name if self.credentials_file else '?'})"
        return f"default ({self.credentials_file.name})" if self.credentials_file else "default (environment)"

    @property
    def graph_base(self) -> str:
        return f"{GRAPH_HOST}/{self.api_version}"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.sqlite3"

    @property
    def tmp_dir(self) -> Path:
        """Temporary fast-start upload copies. Always outside the video folder."""
        return self.data_dir / "tmp"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "publish.lock"

    def require_credentials(self) -> None:
        missing = []
        if not self.ig_user_id:
            missing.append("IG_USER_ID")
        if not self.access_token:
            missing.append("IG_ACCESS_TOKEN")
        if missing:
            where = self.credentials_file.name if self.credentials_file else ".env"
            raise ConfigError(
                f"Missing {', '.join(missing)} in {where}. Fill it in locally "
                "(see README 'Account profiles' / 'One-time Meta setup')."
            )


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return None if value in _PLACEHOLDERS else value


def load_config(env: Optional[Mapping[str, str]] = None, dotenv_path: Optional[Path] = None, *,
                profile: Optional[str] = None, root: Optional[Path] = None,
                base_env: Optional[Mapping[str, str]] = None) -> Config:
    """Build a Config.

    profile=None (no --account): legacy behaviour. Load ``.env`` (os.environ wins), or use ``env``.
    profile="test"/"personal": credentials come ONLY from ``.env.<profile>``. ``.env`` is not read
    and IG_USER_ID / IG_ACCESS_TOKEN from the shell environment are ignored, so a missing or
    incomplete profile can never fall through to a different account.
    """
    root = Path(root) if root else PROJECT_ROOT
    credentials_file: Optional[Path] = None
    if profile is not None:
        env, credentials_file = _profile_env(profile, root, os.environ if base_env is None else base_env)
    elif env is None:
        from dotenv import load_dotenv

        path = dotenv_path or (root / ".env")
        if path.exists():
            secure_credentials_file(path)
            load_dotenv(path, override=False)
            credentials_file = path
        env = os.environ

    ig_user_id = _clean(env.get("IG_USER_ID"))
    if ig_user_id and not ig_user_id.isdigit():
        raise ConfigError("IG_USER_ID must be the numeric Instagram professional account ID, "
                          "not a username, Page ID label or app ID.")

    token = _clean(env.get("IG_ACCESS_TOKEN"))
    register_secret(token)
    list_token = _clean(env.get("LIPSYNC_LIST_TOKEN"))
    register_secret(list_token)

    version = (env.get("GRAPH_API_VERSION") or DEFAULT_API_VERSION).strip()
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise ConfigError(f"GRAPH_API_VERSION must look like v27.0 (got {version!r}).")

    strategy = (env.get("TRIAL_GRADUATION_STRATEGY") or "MANUAL").strip()
    if strategy not in VALID_STRATEGIES:
        raise ConfigError(f"TRIAL_GRADUATION_STRATEGY must be one of {VALID_STRATEGIES} "
                          f"(uppercase), got {strategy!r}.")

    videos_dir_raw = env.get("TRIAL_REELS_DIR") or str(Path.home() / "Documents" / "Trial-Reels")
    videos_dir = Path(os.path.expandvars(videos_dir_raw)).expanduser()

    data_dir = Path(env.get("TRIAL_REELS_DATA_DIR") or root / "data").expanduser()
    log_dir = Path(env.get("TRIAL_REELS_LOG_DIR") or root / "logs").expanduser()
    for d in (data_dir, log_dir):
        if _is_inside(d, videos_dir):
            raise ConfigError(f"{d} is inside the monitored video folder; keep state/logs outside it.")

    def num(name: str, default: float) -> float:
        raw = env.get(name)
        if raw in (None, ""):
            return default
        try:
            return float(raw)
        except ValueError:
            raise ConfigError(f"{name} must be a number (got {raw!r}).")

    return Config(
        ig_user_id=ig_user_id,
        access_token=token,
        api_version=version,
        videos_dir=videos_dir,
        graduation_strategy=strategy,
        default_caption=env.get("DEFAULT_CAPTION") or "",
        timezone=(env.get("TIMEZONE") or "America/Denver").strip(),
        log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper(),
        data_dir=data_dir,
        log_dir=log_dir,
        poll_interval_s=num("POLL_INTERVAL_SECONDS", 5.0),
        poll_timeout_s=num("POLL_TIMEOUT_SECONDS", 300.0),
        http_timeout_s=num("HTTP_TIMEOUT_SECONDS", 30.0),
        upload_timeout_s=num("UPLOAD_TIMEOUT_SECONDS", 600.0),
        max_retries=int(num("MAX_RETRIES", 3)),
        lipsync_worker_url=_clean(env.get("LIPSYNC_WORKER_URL")),
        lipsync_list_token=list_token,
        profile=profile,
        credentials_file=credentials_file,
    )


def _profile_env(profile: str, root: Path, base_env: Mapping[str, str]):
    if profile not in PROFILES:
        raise ConfigError(f"Unknown account profile {profile!r}; use one of: {', '.join(PROFILES)}.")
    path = root / PROFILES[profile]
    if not path.exists():
        raise ConfigError(
            f"--account {profile} needs {path.name} in {root}, but it does not exist. Nothing else was "
            f"used instead. Create it (README 'Account profiles'):\n"
            f"  cp .env.example {path.name} && chmod 600 {path.name}   # then fill in IG_USER_ID / IG_ACCESS_TOKEN")
    if path.is_symlink() or not path.is_file():
        raise ConfigError(f"{path.name} must be a regular file (not a symlink or folder).")
    secure_credentials_file(path)
    from dotenv import dotenv_values

    values = {k: v for k, v in dotenv_values(path).items() if v is not None}
    missing = [k for k in CREDENTIAL_KEYS if _clean(values.get(k)) is None]
    if missing:
        raise ConfigError(f"{path.name} (--account {profile}) is missing {', '.join(missing)} "
                          "(empty or still the placeholder). Nothing else was used instead.")
    merged = {k: v for k, v in base_env.items() if k not in CREDENTIAL_KEYS}  # never inherit credentials
    merged.update(values)
    _warn_if_profiles_share_account(profile, root, values.get("IG_USER_ID", "").strip())
    return merged, path


def _warn_if_profiles_share_account(profile: str, root: Path, ig_user_id: str) -> None:
    from dotenv import dotenv_values

    for other, name in PROFILES.items():
        other_path = root / name
        if other == profile or not other_path.is_file():
            continue
        other_id = (dotenv_values(other_path).get("IG_USER_ID") or "").strip()
        if other_id and other_id == ig_user_id:
            import sys

            print(f"Warning: {PROFILES[profile]} and {name} both use IG_USER_ID {ig_user_id}; "
                  "they point at the same Instagram account.", file=sys.stderr)


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def secure_credentials_file(path: Path) -> None:
    """Credential files must be private to the current user. Tighten to 0600 and warn."""
    import sys

    st = path.stat()
    mode = st.st_mode & 0o777
    if st.st_uid != os.getuid():
        print(f"Warning: {path.name} is owned by another user; check who can read your token.",
              file=sys.stderr)
        return
    if mode & 0o077:
        try:
            os.chmod(path, 0o600)
            print(f"Warning: {path.name} was readable by other users (mode {oct(mode)}); "
                  "permissions tightened to 600 (owner read/write only).", file=sys.stderr)
        except OSError as e:
            print(f"Warning: {path.name} is readable by other users (mode {oct(mode)}) and could not be "
                  f"fixed ({e}). Run: chmod 600 {path}", file=sys.stderr)
