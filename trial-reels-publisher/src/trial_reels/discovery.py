"""Where videos come from.

Stage 1: ``SingleFileSource`` -- exactly one .mp4 or .mov at the root of the folder.
Stage 2 will add a ``DatedFolderSource`` implementing the same ``VideoSource``
protocol; the Meta transport and publisher do not change.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import List

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

from .errors import DiscoveryError

VIDEO_SUFFIXES = (".mp4", ".mov")  # compared case-insensitively
# macOS "dataless" flag: file content lives in iCloud and has not been downloaded.
SF_DATALESS = 0x40000000


class VideoSource(Protocol):
    def discover(self) -> List[Path]:
        """Return the videos due for publishing, in deterministic order."""
        ...


class SingleFileSource:
    """Exactly one *.mp4 / *.mov directly inside ``folder``; anything else is an error."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def discover(self) -> List[Path]:
        folder = self.folder
        if not folder.exists():
            raise DiscoveryError(f"Video folder not found: {folder}\n"
                                 f"Create it (exact name 'Trial-Reels') and put one .mp4 or .mov in it.")
        if not folder.is_dir():
            raise DiscoveryError(f"{folder} exists but is not a folder.")
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name)
        except PermissionError:
            raise DiscoveryError(
                f"Permission denied reading {folder}. In System Settings > Privacy & Security > "
                "Files and Folders (or Full Disk Access), allow your Terminal app to access Documents."
            )

        # Evicted iCloud files appear as ".<name>.mp4.icloud" placeholders.
        placeholders = [p for p in entries if p.name.startswith(".")
                        and any(p.name.lower().endswith(s + ".icloud") for s in VIDEO_SUFFIXES)]
        if placeholders:
            names = ", ".join(p.name[1:-len(".icloud")] for p in placeholders)
            raise DiscoveryError(f"iCloud has not downloaded: {names}. In Finder, right-click it "
                                 "and choose 'Download Now', then run again.")

        videos = [p for p in entries
                  if p.suffix.lower() in VIDEO_SUFFIXES and not p.name.startswith(".")
                  and p.is_file()]  # is_file() also skips directories named "x.mp4"/"x.mov"

        if not videos:
            others = [p.name for p in entries if p.is_file() and not p.name.startswith(".")]
            msg = f"No .mp4 or .mov video found directly inside {folder}."
            if others:
                msg += f" Found other files: {', '.join(others)} (only .mp4 and .mov are picked up in Stage 1)."
            raise DiscoveryError(msg)
        if len(videos) > 1:
            raise DiscoveryError(
                f"Found {len(videos)} videos (.mp4/.mov) in {folder}; Stage 1 needs exactly one so it never "
                f"guesses: {', '.join(p.name for p in videos)}"
            )
        video = videos[0]
        check_locally_available(video)
        return [video]


def check_locally_available(path: Path) -> None:
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise DiscoveryError(f"{path.name} is not a regular file.")
    flags = getattr(st, "st_flags", 0)
    if flags & SF_DATALESS:
        raise DiscoveryError(f"{path.name} is an iCloud placeholder that has not been downloaded. "
                             "In Finder choose 'Download Now', then run again.")
    if not os.access(path, os.R_OK):
        raise DiscoveryError(f"{path.name} is not readable (check permissions).")
