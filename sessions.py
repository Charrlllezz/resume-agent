#!/usr/bin/env python3
"""Who is using this, and what is theirs.

The local build has one implicit user: the key comes from .env and the resume
from master_resume.json. Hosted, every visitor brings their own of both. Rather
than run two apps that drift apart -- this project's recurring bug is a fix
landing in one place and not its sibling -- there is one app with a session
layer, and local mode is a session seeded from disk at startup.

**The API key never leaves this process.** Not in the cookie, not in a log, not
on disk. Flask's session cookie is signed but not encrypted, so anything put
there is readable by whoever holds the cookie; the cookie carries an opaque id
and nothing else. Keys live in memory, are dropped when a session goes idle,
and die with the process.
"""

import os
import secrets
import threading
import time
from dataclasses import dataclass, field

# Idle sessions are dropped, which also drops the key they were holding.
TTL_SECONDS = 2 * 60 * 60
SWEEP_EVERY = 5 * 60

# Chromium is memory-hungry and a run holds one for a few seconds. Without a
# ceiling, a handful of simultaneous runs will exhaust a small machine; with
# one, extra runs wait their turn instead of taking the box down.
MAX_CONCURRENT_RUNS = int(os.environ.get("MAX_CONCURRENT_RUNS", "2"))
RUN_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)

# One person should not be able to queue fifty runs on someone else's machine.
MAX_RUNS_PER_SESSION = int(os.environ.get("MAX_RUNS_PER_SESSION", "40"))


def single_user() -> bool:
    """Local unless told otherwise. Hosting must be opted into explicitly.

    The dangerous default is the other way round: a build that assumes hosted
    and falls back to local would happily serve one person's resume and key to
    every visitor if the flag were ever unset.
    """
    return os.environ.get("RESUME_AGENT_HOSTED", "").lower() not in ("1", "true", "yes")


@dataclass
class Session:
    id: str
    api_key: str = ""
    resume: dict | None = None
    jobs: dict = field(default_factory=dict)
    drafts: dict = field(default_factory=dict)
    runs_started: int = 0
    last_seen: float = field(default_factory=time.time)

    @property
    def ready(self) -> bool:
        return bool(self.api_key) and bool(self.resume)

    def touch(self):
        self.last_seen = time.time()


class Store:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._swept = time.time()

    def _sweep(self):
        cutoff = time.time() - TTL_SECONDS
        for sid in [s for s, v in self._sessions.items() if v.last_seen < cutoff]:
            # Drops the key with it. Nothing to scrub because nothing was
            # written anywhere else.
            self._sessions.pop(sid, None)
        self._swept = time.time()

    def get(self, sid: str | None) -> Session | None:
        with self._lock:
            if time.time() - self._swept > SWEEP_EVERY:
                self._sweep()
            session = self._sessions.get(sid) if sid else None
            if session:
                session.touch()
            return session

    def create(self) -> Session:
        with self._lock:
            session = Session(id=secrets.token_urlsafe(24))
            self._sessions[session.id] = session
            return session

    def drop(self, sid: str):
        with self._lock:
            self._sessions.pop(sid, None)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


STORE = Store()


def redact(text: str) -> str:
    """Strip anything shaped like an API key out of a message before it is shown.

    Belt and braces: keys should never reach an error string, but an SDK is
    free to quote the request it failed on, and that page is rendered.
    """
    import re
    return re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", text)


# ---------------------------------------------------------------------------
# Outbound URL safety
# ---------------------------------------------------------------------------

def safe_url(url: str) -> str:
    """Refuse a URL that points somewhere private. Returns '' if it is fine.

    Hosted, the posting URL is attacker-controlled and this process will fetch
    it and render the result. Unguarded, that is a request forgery primitive:
    http://169.254.169.254/ is cloud metadata, anything.internal is Fly's
    private network, and 127.0.0.1 is this container. The name is resolved
    first, because a hostname that looks public can answer with 10.0.0.5.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Only http and https URLs can be fetched."
    host = parsed.hostname or ""
    if not host:
        return "That URL has no host in it."
    if host.endswith(".internal") or host.endswith(".local") or host == "localhost":
        return "That address is on a private network."

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return "That address doesn't resolve — check the URL."

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return "That address is on a private network."
    return ""
