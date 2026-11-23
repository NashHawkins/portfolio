"""Read-only checks of a publicly hosted video for the URL upload method (``--upload-method url``).

Meta fetches ``video_url`` itself ("We cURL the video using the passed-in URL, so it must be on a public
server"). Before handing a URL to Meta we fetch it the same anonymous way and check it:

* URL shape: https only, no credentials/userinfo, default port, no query or fragment, a DNS name (not an
  IP literal) that resolves ONLY to public addresses (SSRF guard: this tool never fetches private,
  loopback, link-local or metadata addresses).
* HTTP: a direct 200 with no redirect (redirects are refused, not followed), sent without cookies,
  auth headers, .netrc or proxy credentials. Content-Type video/mp4, Content-Length within Meta's limit.
* Bytes: an MP4 'ftyp' box (not an HTML error page), the exact advertised length, moov at the front,
  then the same ffprobe rules as local files.
* Identity: the hosted file's video stream (packet data) must hash identically to the ORIGINAL local
  video's, which proves it is a stream copy of that video. Duplicate protection and state keep using
  the original's path and SHA-256.

Only GET/HEAD requests are made, and only to the given URL. The downloaded copy lives in data/tmp and is
deleted afterwards. Nothing here talks to Meta or modifies the hosted object.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import urlsplit

import requests

from .errors import PublisherError, VideoValidationError, redact
from .validation import MAX_FILE_BYTES, VideoReport, validate_video

log = logging.getLogger(__name__)

MAX_URL_LEN = 2048
CHUNK = 1 << 20
HTML_MARKERS = (b"<!doctype", b"<html", b"<?xml", b"<head", b"<body", b"{")


class HostedVideoError(PublisherError):
    """The hosted URL is not something Meta should be asked to fetch. Nothing was sent to Meta."""


@dataclass
class HostedVideo:
    url: str
    host: str
    addresses: List[str]
    content_type: str
    content_length: int
    sha256: str
    report: VideoReport
    headers: dict = field(default_factory=dict)
    stream_sha256: Optional[str] = None
    source_stream_sha256: Optional[str] = None
    identity: str = "NOT CHECKED"   # MATCH | MISMATCH | NOT CHECKED

    @property
    def identity_ok(self) -> bool:
        return self.identity == "MATCH"


# ------------------------------------------------------------------ URL shape + SSRF
def check_url_shape(url: str) -> "tuple":
    if not isinstance(url, str) or not url.strip():
        raise HostedVideoError("--video-url is empty.")
    if url != url.strip() or any(c.isspace() or ord(c) < 32 for c in url):
        raise HostedVideoError("--video-url contains whitespace or control characters.")
    if len(url) > MAX_URL_LEN:
        raise HostedVideoError(f"--video-url is longer than {MAX_URL_LEN} characters.")
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError as e:
        raise HostedVideoError(f"--video-url is not a valid URL ({e}).")
    if p.scheme != "https":
        raise HostedVideoError(f"--video-url must use https:// (got {p.scheme or 'no scheme'}).")
    if p.username is not None or p.password is not None or "@" in p.netloc:
        raise HostedVideoError("--video-url must not contain a username or password.")
    if port not in (None, 443):
        raise HostedVideoError(f"--video-url must use the default https port (got {port}).")
    if p.query or p.fragment or "?" in url or "#" in url:
        raise HostedVideoError("--video-url must be a plain public URL with no query string or fragment "
                               "(signed/expiring URLs and tokens are not supported).")
    host = (p.hostname or "").rstrip(".").lower()
    try:
        ipaddress.ip_address(host.strip("[]"))
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip:
        raise HostedVideoError("--video-url must use a DNS host name, not an IP address.")
    if not host or "." not in host:
        raise HostedVideoError("--video-url needs a fully qualified public host name.")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise HostedVideoError(f"--video-url host {host!r} is not a public host.")
    if not p.path.lower().endswith(".mp4"):
        raise HostedVideoError("--video-url must point at an .mp4 file.")
    return p, host


def resolve_public(host: str, resolver: Callable = socket.getaddrinfo) -> List[str]:
    try:
        infos = resolver(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError) as e:
        raise HostedVideoError(f"Cannot resolve {host}: {e}")
    addrs = sorted({str(i[4][0]).split("%")[0] for i in infos})
    if not addrs:
        raise HostedVideoError(f"{host} did not resolve to any address.")
    bad = []
    for a in addrs:
        ip = ipaddress.ip_address(a)
        mapped = getattr(ip, "ipv4_mapped", None)
        ip = mapped or ip
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local \
                or ip.is_private or ip.is_unspecified:
            bad.append(a)
    if bad:
        raise HostedVideoError(f"{host} resolves to non-public address(es) {', '.join(bad)}; refusing to fetch "
                               "it (and Meta could not fetch it either).")
    return addrs


def anonymous_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False          # no .netrc credentials, no proxy credentials from the environment
    s.headers.clear()
    s.headers.update({"User-Agent": "trial-reels-publisher/1.0 (video_url check)", "Accept": "*/*"})
    return s


def _check_response(resp, url: str, what: str) -> (str, int):
    status = resp.status_code
    if 300 <= status < 400:
        loc = resp.headers.get("Location", "?")
        raise HostedVideoError(f"{what}: HTTP {status} redirect to {redact(loc)[:200]}. Meta needs the final, "
                               "direct URL; redirects are not followed. Use the final URL instead.")
    if status in (401, 403, 407):
        raise HostedVideoError(f"{what}: HTTP {status}: the URL needs authentication, so Meta cannot fetch it.")
    if status != 200:
        raise HostedVideoError(f"{what}: HTTP {status} (expected 200).")
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "video/mp4":
        raise HostedVideoError(f"{what}: Content-Type is {ctype or 'missing'!r}, expected 'video/mp4'"
                               + (" (looks like a web page, not the video)" if "html" in ctype else "") + ".")
    raw_len = resp.headers.get("Content-Length")
    try:
        length = int(raw_len)
    except (TypeError, ValueError):
        raise HostedVideoError(f"{what}: no valid Content-Length header.")
    if length <= 0:
        raise HostedVideoError(f"{what}: Content-Length is {length}.")
    if length > MAX_FILE_BYTES:
        raise HostedVideoError(f"{what}: {length / 1e6:.0f} MB is over Meta's 300 MB Reels limit.")
    if (resp.headers.get("Content-Encoding") or "identity").lower() not in ("identity", ""):
        raise HostedVideoError(f"{what}: served with Content-Encoding {resp.headers['Content-Encoding']!r}; "
                               "Meta expects the raw MP4.")
    return ctype, length


# ------------------------------------------------------------------ identity
def video_stream_sha256(path: Path, ffmpeg: Optional[str], run: Callable = subprocess.run) -> Optional[str]:
    """SHA-256 of the first video stream's packets (``ffmpeg -c copy -f hash``). A stream-copy remux
    (MOV -> MP4, audio re-encoded) keeps these packets byte-identical. None if ffmpeg is unavailable."""
    if not ffmpeg:
        return None
    proc = run([ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0", "-c", "copy",
                "-f", "hash", "-hash", "sha256", "-"], capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise VideoValidationError(f"ffmpeg could not read the video stream of {Path(path).name}: "
                                   f"{redact(proc.stderr.strip())[:300]}")
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if not line.upper().startswith("SHA256="):
        raise VideoValidationError(f"unexpected ffmpeg hash output: {line[:100]!r}")
    return line.split("=", 1)[1].strip().lower()


# ------------------------------------------------------------------ fetch + validate
def inspect_hosted_video(url: str, *, tmp_root: Path, source_path: Optional[Path] = None,
                         ffmpeg: Optional[str] = None, session: Optional[requests.Session] = None,
                         resolver: Callable = socket.getaddrinfo, validator: Callable = validate_video,
                         hasher: Callable = video_stream_sha256, timeout_s: float = 120.0) -> HostedVideo:
    """Read-only: HEAD + GET the URL anonymously, validate the bytes, compare with the original."""
    _, host = check_url_shape(url)
    addrs = resolve_public(host, resolver)
    s = session or anonymous_session()

    head = s.request("HEAD", url, allow_redirects=False, timeout=30)
    _check_response(head, url, "HEAD")

    tmp_root = Path(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="hosted-", dir=str(tmp_root)))
    try:
        resolve_public(host, resolver)  # re-check right before the download (DNS may change)
        resp = s.request("GET", url, allow_redirects=False, stream=True, timeout=(30, timeout_s))
        try:
            ctype, length = _check_response(resp, url, "GET")
            dest = work / "hosted.mp4"
            h = hashlib.sha256()
            got = 0
            first = b""
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(CHUNK):
                    if not chunk:
                        continue
                    if len(first) < 64:
                        first += chunk[:64 - len(first)]
                    got += len(chunk)
                    if got > length or got > MAX_FILE_BYTES:
                        raise HostedVideoError(f"server sent more than the advertised {length} bytes.")
                    h.update(chunk)
                    f.write(chunk)
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
        lowered = first.lstrip().lower()
        if any(lowered.startswith(m) for m in HTML_MARKERS):
            raise HostedVideoError("the URL returned a web page or text, not an MP4 video.")
        if len(first) < 8 or first[4:8] != b"ftyp":
            raise HostedVideoError("the URL's bytes do not start with an MP4 'ftyp' box; not a valid MP4.")
        if got != length:
            raise HostedVideoError(f"download was {got} bytes but Content-Length said {length}.")
        report = validator(dest, stable_checks=0)
        if report.needs_faststart:
            raise HostedVideoError("the hosted MP4 has its 'moov' atom at the end. Meta requires it at the front; "
                                   "re-create it with -movflags +faststart and upload it again.")
        hv = HostedVideo(url=url, host=host, addresses=addrs, content_type=ctype, content_length=length,
                         sha256=h.hexdigest(), report=report,
                         headers={k: v for k, v in head.headers.items()
                                  if k.lower() in ("etag", "last-modified", "server", "accept-ranges")})
        if source_path is not None and ffmpeg:
            hv.stream_sha256 = hasher(dest, ffmpeg)
            hv.source_stream_sha256 = hasher(Path(source_path), ffmpeg)
            hv.identity = "MATCH" if hv.stream_sha256 == hv.source_stream_sha256 else "MISMATCH"
        return hv
    finally:
        shutil.rmtree(work, ignore_errors=True)


def identity_evidence(hv: HostedVideo, source_name: str) -> str:
    return (f"hosted {hv.url} sha256={hv.sha256} size={hv.content_length}; video stream sha256="
            f"{hv.stream_sha256} == original {source_name} video stream sha256={hv.source_stream_sha256}")
