#!/usr/bin/env python3
"""Manage a loopback-only static server for generated webdoc sites."""

from __future__ import annotations

import argparse
import functools
import http.server
import ipaddress
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

try:
    from settings import read_settings
except Exception:  # pragma: no cover - settings module should sit beside this file
    def read_settings() -> dict:
        return {"auto_open": True}

try:
    import edit_support
except Exception:  # pragma: no cover - editing mode is optional; absence must not break serving
    edit_support = None  # type: ignore[assignment]

try:
    import create_site
except Exception:  # pragma: no cover - rebuild-after-edit is best-effort; absence must not break serving
    create_site = None  # type: ignore[assignment]


DEFAULT_ROOT = Path(os.environ.get("AGENT_ARTIFACT_SITES", "~/agent-artifacts/sites")).expanduser()
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
LOOPBACK_EDIT_MESSAGE = (
    "Editing is restricted to the local machine because it writes back to the "
    "source file. Open this site via 127.0.0.1 or localhost on the computer "
    "serving it to edit. Viewing and feedback work over the network; editing "
    "does not."
)
# TTL is now a backstop, not the primary lifetime control: the page heartbeat +
# idle shutdown reclaim a server once its last tab goes away, so a long TTL is
# safe. 7 days covers a week-long review without a manual restart.
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
# Shut down this long after the last request when no open tab is pinging. Chrome
# throttles a hidden tab's timers to roughly one fire per minute, so the page's
# 20-second heartbeat still lands several times inside a 30-minute window.
DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60
WATCHDOG_POLL_SECONDS = 15
IDLE_GRACE_SECONDS = 60
MAX_FEEDBACK_BYTES = 64 * 1024
MAX_EDIT_BYTES = 512 * 1024
MAX_HEARTBEAT_BYTES = 4 * 1024
FEEDBACK_LOCK = threading.Lock()
EDIT_LOCK = threading.Lock()

# Injected server-side into every served .html so old builds participate in idle
# shutdown with no rebuild. One line, no dependencies, all errors swallowed:
# ping once, then every 20 seconds, and again whenever a hidden tab is reshown.
HEARTBEAT_SNIPPET = (
    b'<script>(function(){var p=function(){try{fetch("/api/heartbeat",'
    b'{method:"POST",keepalive:true}).catch(function(){});}catch(e){}};p();'
    b'setInterval(p,20000);document.addEventListener("visibilitychange",'
    b'function(){if(!document.hidden){p();}});})();</script>'
)
_BODY_CLOSE_RE = re.compile(rb"</body\s*>", re.IGNORECASE)


def inject_heartbeat(raw: bytes) -> bytes:
    """Insert HEARTBEAT_SNIPPET before the last </body> (case-insensitive, at the
    byte level), or append it when the document has no </body>."""
    matches = list(_BODY_CLOSE_RE.finditer(raw))
    if matches:
        at = matches[-1].start()
        return raw[:at] + HEARTBEAT_SNIPPET + raw[at:]
    return raw + HEARTBEAT_SNIPPET


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def addr_is_loopback(addr: str) -> bool:
    """True when a raw peer IP is a loopback address (127.0.0.0/8 or ::1).

    Unlike the Host header (client-supplied, spoofable), the TCP peer address is
    the kernel's view of who connected. IPv4-mapped IPv6 (::ffff:127.0.0.1) is
    unwrapped first."""
    if not addr:
        return False
    if addr.startswith("::ffff:"):
        addr = addr[len("::ffff:"):]
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def resolve_site(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.exists():
        return candidate.resolve()
    by_id = DEFAULT_ROOT / str(value)
    return by_id.resolve()


def server_json(site_dir: Path) -> Path:
    return site_dir / "server.json"


def feedback_jsonl(site_dir: Path) -> Path:
    return site_dir / "feedback.jsonl"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_server_info(site_dir: Path) -> dict[str, object] | None:
    path = server_json(site_dir)
    if not path.exists():
        return None
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return info if isinstance(info, dict) else None


def validate_site(site_dir: Path, allow_symlinks: bool = False) -> None:
    if not site_dir.exists() or not site_dir.is_dir():
        raise SystemExit(f"site directory not found: {site_dir}")
    if not (site_dir / "index.html").exists():
        raise SystemExit(f"index.html not found in: {site_dir}")
    if not allow_symlinks:
        for item in site_dir.rglob("*"):
            if item.is_symlink():
                raise SystemExit(f"refusing to serve symlink inside site directory: {item}")


class NoListingHandler(http.server.SimpleHTTPRequestHandler):
    def site_dir(self) -> Path:
        return Path(self.directory).resolve()

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _touch_activity(self) -> None:
        """Record this request as activity on the server (guarded so a handler
        built without a live server, as the test harness does, does not break)."""
        touch = getattr(getattr(self, "server", None), "touch", None)
        if callable(touch):
            touch()

    def _idle_enabled(self) -> bool:
        return bool(getattr(getattr(self, "server", None), "idle_shutdown_enabled", False))

    def do_GET(self) -> None:
        self._touch_activity()
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json(200, {"ok": True, "site_dir": str(self.site_dir())})
            return
        if parsed.path == "/api/version":
            # Cheap stat-on-demand version for the client's live-reload poll. No
            # watcher thread: the mtime IS the generation. A string, because
            # mtime_ns exceeds JS Number.MAX_SAFE_INTEGER.
            self.send_json(200, {"v": self._site_version()})
            return
        if parsed.path == "/api/feedback":
            entries: list[object] = []
            path = feedback_jsonl(self.site_dir())
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        entries.append({"error": "bad_feedback_line", "raw": line})
            self.send_json(200, {"feedback_path": str(path), "entries": entries})
            return
        if self._forbidden_static_path(self.path):
            self.send_error(404, "File not found")
            return
        if self._idle_enabled():
            html_file = self._resolve_html_file()
            if html_file is not None:
                self._serve_html_with_heartbeat(html_file)
                return
        super().do_GET()

    def do_HEAD(self) -> None:
        self._touch_activity()
        if self._forbidden_static_path(self.path):
            self.send_error(404, "File not found")
            return
        if self._idle_enabled():
            html_file = self._resolve_html_file()
            if html_file is not None:
                self._serve_html_with_heartbeat(html_file, head_only=True)
                return
        super().do_HEAD()

    def _peer_is_loopback(self) -> bool:
        """True when the connecting TCP peer is a loopback address.

        This is the authoritative write-path gate: the Host header check below
        defends DNS-rebinding but trusts client-supplied text, so under
        --allow-lan a LAN peer can forge `Host: 127.0.0.1`. The peer address
        cannot be forged over a real TCP connection."""
        peer = ""
        try:
            peer = self.client_address[0]
        except (AttributeError, IndexError, TypeError):
            try:
                peer = self.connection.getpeername()[0]
            except Exception:
                return False
        return addr_is_loopback(peer)

    def _host_is_loopback(self) -> bool:
        """True when the request's Host header names a loopback address.

        The write path (/api/edit) is loopback-only regardless of --allow-lan:
        it mutates the canonical source file, so it must never be reachable from
        the LAN even when read/feedback serving is intentionally exposed."""
        host = self.headers.get("Host", "")
        if host.startswith("["):  # bracketed IPv6, e.g. [::1]:8000
            host = host[1:].split("]", 1)[0]
        else:
            host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return host in LOOPBACK_HOSTS

    def _read_json_body(self, limit: int) -> dict | None:
        """Read + parse a size-capped JSON request body, or send the error and
        return None. Mirrors the feedback endpoint's guards."""
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            self.send_json(400, {"error": "bad_content_length"})
            return None
        if length <= 0:
            self.send_json(400, {"error": "empty_body"})
            return None
        if length > limit:
            self.send_json(413, {"error": "body_too_large", "limit_bytes": limit})
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "bad_json"})
            return None
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "bad_json"})
            return None
        return payload

    def do_POST(self) -> None:
        self._touch_activity()
        parsed = urlparse(self.path)
        if parsed.path == "/api/feedback":
            self.handle_feedback()
        elif parsed.path == "/api/audit":
            self.handle_audit()
        elif parsed.path == "/api/edit":
            self.handle_edit()
        elif parsed.path == "/api/undo":
            self.handle_undo()
        elif parsed.path == "/api/heartbeat":
            self.handle_heartbeat()
        else:
            self.send_json(404, {"error": "not_found"})

    def handle_heartbeat(self) -> None:
        """Keep-alive ping from an open tab. The activity touch already happened in
        do_POST; the body is ignored (read and discarded within a cap so a stuck or
        oversized client cannot tie up the handler). Zero-length and non-JSON bodies
        are fine. Reachable over the LAN too, so a --allow-lan viewer keeps it up."""
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            length = 0
        if length > MAX_HEARTBEAT_BYTES:
            # Drain a bounded slice so the 413 reaches the client cleanly rather than
            # racing a connection reset, without buffering an unbounded body.
            try:
                self.rfile.read(MAX_HEARTBEAT_BYTES)
            except Exception:
                pass
            self.send_json(413, {"error": "body_too_large", "limit_bytes": MAX_HEARTBEAT_BYTES})
            return
        if length > 0:
            try:
                self.rfile.read(length)
            except Exception:
                pass
        self.send_json(200, {"ok": True})

    def _append_feedback(self, entry: dict) -> Path:
        path = feedback_jsonl(self.site_dir())
        with FEEDBACK_LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
        return path

    def handle_feedback(self) -> None:
        payload = self._read_json_body(MAX_FEEDBACK_BYTES)
        if payload is None:
            return
        if payload.get("kind") == "annotations":
            self._handle_annotations(payload)
            return
        feedback = str(payload.get("feedback", "")).strip()
        if not feedback:
            self.send_json(400, {"error": "empty_feedback"})
            return
        entry = {
            "received_at": now_iso(),
            "artifact_id": str(payload.get("artifact_id", ""))[:160],
            "page": str(payload.get("page", ""))[:300],
            "feedback": feedback,
            "user_agent": self.headers.get("user-agent", "")[:300],
        }
        path = self._append_feedback(entry)
        self.send_json(200, {"ok": True, "feedback_path": str(path), "received_at": entry["received_at"]})

    @staticmethod
    def _clean_block(raw: object) -> dict:
        """Sanitize a client-sent block identity down to the known keys. Values
        are advisory anchors for the agent (hash-verified against the source at
        read time, like the edit ledger), never used to write anything."""
        if not isinstance(raw, dict):
            return {}
        out: dict = {}
        if raw.get("type"):
            out["type"] = str(raw["type"])[:24]
        for key in ("start", "end", "line", "cell"):
            try:
                out[key] = int(raw[key])
            except (KeyError, TypeError, ValueError):
                continue
        if raw.get("hash"):
            out["hash"] = str(raw["hash"])[:32]
        return out

    def _handle_annotations(self, payload: dict) -> None:
        """One batch of block-anchored annotations, sent when the READER decides
        (they queue locally while reading; the agent sees one entry per send,
        never a live stream)."""
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            self.send_json(400, {"error": "empty_annotations"})
            return
        items: list[dict] = []
        for raw in raw_items[:100]:
            if not isinstance(raw, dict):
                continue
            comment = str(raw.get("comment", "")).strip()
            if not comment:
                continue
            item: dict = {
                "comment": comment[:2000],
                "block": self._clean_block(raw.get("block")),
                "excerpt": str(raw.get("excerpt", ""))[:240],
            }
            selected = raw.get("selected")
            if isinstance(selected, dict) and str(selected.get("text", "")).strip():
                item["selected"] = str(selected["text"])[:500]
            if raw.get("queued_at"):
                item["queued_at"] = str(raw["queued_at"])[:40]
            items.append(item)
        if not items:
            self.send_json(400, {"error": "empty_annotations"})
            return
        entry = {
            "received_at": now_iso(),
            "kind": "annotations",
            "page": str(payload.get("page", ""))[:300],
            "count": len(items),
            "items": items,
            "user_agent": self.headers.get("user-agent", "")[:300],
        }
        path = self._append_feedback(entry)
        self.send_json(200, {"ok": True, "count": len(items), "feedback_path": str(path)})

    def handle_audit(self) -> None:
        """Layout-audit findings from the page's own render (assets/audit.js).
        Telemetry-only: appended to feedback.jsonl for the agent, writes nothing
        else. The client only posts when the finding set changed, so a clean
        page costs zero entries."""
        payload = self._read_json_body(MAX_FEEDBACK_BYTES)
        if payload is None:
            return
        raw_findings = payload.get("findings")
        if not isinstance(raw_findings, list):
            self.send_json(400, {"error": "bad_findings"})
            return
        findings: list[dict] = []
        for raw in raw_findings[:200]:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind", ""))[:40]
            severity = str(raw.get("severity", ""))
            if not kind or severity not in ("error", "warning"):
                continue
            finding: dict = {
                "kind": kind,
                "severity": severity,
                "selector": str(raw.get("selector", ""))[:300],
                "persistent": bool(raw.get("persistent")),
            }
            for key in ("overflowPx", "viewportWidth"):
                try:
                    value = float(raw[key])
                except (KeyError, TypeError, ValueError):
                    continue
                # A crafted client could send Infinity/NaN; json.dumps would emit
                # bare tokens that break strict JSON consumers of feedback.jsonl.
                if not math.isfinite(value):
                    continue
                finding[key] = round(value, 1)
            block = self._clean_block(raw.get("block"))
            if block:
                finding["block"] = block
            findings.append(finding)
        entry = {
            "received_at": now_iso(),
            "kind": "layout_warnings",
            "page": str(payload.get("page", ""))[:300],
            "count": len(findings),
            "error_count": sum(1 for f in findings if f["severity"] == "error"),
            "findings": findings,
            "user_agent": self.headers.get("user-agent", "")[:300],
        }
        path = self._append_feedback(entry)
        self.send_json(200, {"ok": True, "count": len(findings), "feedback_path": str(path)})

    def _require_loopback_edit(self) -> bool:
        """Gate any write path on loopback: the real TCP peer first (unspoofable),
        then the Host header (DNS-rebinding defence). Both must be loopback even
        under --allow-lan. Sends the 403 and returns False when either fails."""
        if not self._peer_is_loopback() or not self._host_is_loopback():
            self.send_json(403, {"error": "loopback_only", "message": LOOPBACK_EDIT_MESSAGE})
            return False
        return True

    def _edit_source(self) -> "Path | None":
        """The one writable target: the manifest's source_path. Sends the 500 and
        returns None when the manifest or source file is missing."""
        manifest_path = self.site_dir() / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.send_json(500, {"error": "no_manifest"})
            return None
        source_path = manifest.get("source_path") if isinstance(manifest, dict) else None
        if not source_path:
            self.send_json(500, {"error": "no_source_path"})
            return None
        source = Path(str(source_path))
        if not source.is_file():
            self.send_json(500, {"error": "source_missing"})
            return None
        return source

    def _site_version(self) -> str:
        """The served page's generation, matching the client's baseline exactly:
        the render-time <meta name="webdoc-version"> stamp in index.html (the
        head fits well inside the first 4KiB). Falls back to mtime_ns for a page
        built before the stamp existed; "0" when index.html is missing."""
        index = self.site_dir() / "index.html"
        try:
            with index.open("r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(4096)
        except OSError:
            return "0"
        match = re.search(r'<meta name="webdoc-version" content="(\d+)"', head)
        if match:
            return match.group(1)
        try:
            return str(index.stat().st_mtime_ns)
        except OSError:
            return "0"

    def handle_edit(self) -> None:
        if edit_support is None:
            self.send_json(500, {"error": "editing_unavailable"})
            return
        if not self._require_loopback_edit():
            return
        payload = self._read_json_body(MAX_EDIT_BYTES)
        if payload is None:
            return
        source = self._edit_source()
        if source is None:
            return
        try:
            with EDIT_LOCK:
                status, body = edit_support.apply_edit(source, payload)
                # Regenerate the served page from the updated source so the edit
                # survives a reload (best-effort; a rebuild failure never fails the
                # edit, which already persisted). Inside the lock so index.html and
                # the source stay consistent. Log a failure so a stale served page
                # after an edit isn't silent.
                if status == 200 and create_site is not None:
                    if not create_site.rebuild_html(source, self.site_dir()):
                        self.log_message("rebuild after edit failed; served page may be stale until next rebuild")
                if status == 200:
                    # The initiating tab adopts this as its baseline so its own
                    # rebuild never trips its live-reload poll.
                    body["site_version"] = self._site_version()
        except Exception as exc:  # never leak a stack trace to the client
            self.log_message("edit error: %r", exc)
            self.send_json(500, {"error": "edit_failed"})
            return
        self.send_json(status, body)

    def handle_undo(self) -> None:
        if edit_support is None:
            self.send_json(500, {"error": "editing_unavailable"})
            return
        if not self._require_loopback_edit():
            return
        # Undo needs no payload, but the client POSTs "{}" so the body guards stay
        # uniform; read and discard it.
        if self._read_json_body(MAX_EDIT_BYTES) is None:
            return
        source = self._edit_source()
        if source is None:
            return
        try:
            with EDIT_LOCK:
                status, body = edit_support.apply_undo(source)
                if status == 200 and create_site is not None:
                    if not create_site.rebuild_html(source, self.site_dir()):
                        self.log_message("rebuild after undo failed; served page may be stale until next rebuild")
                if status == 200:
                    body["site_version"] = self._site_version()
        except Exception as exc:  # never leak a stack trace to the client
            self.log_message("undo error: %r", exc)
            self.send_json(500, {"error": "undo_failed"})
            return
        self.send_json(status, body)

    # Site control files that must never be served: they carry server state,
    # feedback, the manifest, and the edit ledger. *.edits.json is matched by
    # suffix. Page assets (index.html, doc.html, style.css, assets/*, the bundled
    # JS) are not in this set and keep serving.
    _CONTROL_FILES = {"server.json", "server.log", "feedback.jsonl", "manifest.json"}

    def _forbidden_static_path(self, url_path: str) -> bool:
        """True when a GET/HEAD resolves to a control file or any dotfile/dot-dir,
        which the server must refuse. The check is on the resolved filesystem path
        (via translate_path) so percent-encoding and '.'/'..' segments cannot slip
        a control file through. /api/* is routed before this and is unaffected."""
        try:
            resolved = Path(self.translate_path(url_path)).resolve()
            site = self.site_dir()
            rel = resolved.relative_to(site)
        except ValueError:
            # translate_path clamps into the served tree; anything that still lands
            # outside it is refused rather than served.
            return True
        parts = rel.parts
        if not parts:
            return False  # the site root itself -> index.html handling downstream
        for segment in parts:
            if segment.startswith("."):
                return True
        # Compare case-insensitively: macOS's default filesystem is
        # case-insensitive while Path.resolve() preserves the REQUESTED case, so
        # a case-sensitive check would pass /SERVER.JSON straight through to the
        # OS, which opens the real server.json. On a case-sensitive filesystem
        # the case-varied name is a different, nonexistent file, so refusing it
        # loses nothing.
        name = parts[-1].lower()
        return name in self._CONTROL_FILES or name.endswith(".edits.json")

    def _resolve_html_file(self) -> "Path | None":
        """The existing .html file this GET would serve, mirroring
        SimpleHTTPRequestHandler: a directory maps to its index.html only when the
        URL path ends with '/'. None for non-HTML targets and the no-slash
        directory case (left to the base handler's 301 redirect)."""
        clean = self.path.split("?", 1)[0].split("#", 1)[0]
        p = Path(self.translate_path(self.path))
        if p.is_dir():
            if not clean.endswith("/"):
                return None
            for index in ("index.html", "index.htm"):
                candidate = p / index
                if candidate.is_file():
                    return candidate
            return None
        if p.is_file() and p.suffix.lower() in (".html", ".htm"):
            # Never inject into the doc export: it is the artifact users save and
            # upload to Google Docs, its contract is script-free, and it must stay
            # byte-identical however it is fetched.
            if p.name.lower() == "doc.html":
                return None
            return p
        return None

    def _serve_html_with_heartbeat(self, path: Path, head_only: bool = False) -> None:
        """Serve an .html file with the keep-alive snippet injected. Content-Length
        is recomputed; the type is whatever guess_type reports (text/html).
        head_only sends the same headers with no body, so a HEAD reports exactly
        what the corresponding GET would send (RFC 7231 section 4.3.2)."""
        try:
            body = inject_heartbeat(path.read_bytes())
        except OSError:
            self.send_error(404, "File not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def list_directory(self, path: str):  # type: ignore[override]
        self.send_error(403, "Directory listing disabled")
        return None

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[%s] %s\n" % (datetime.now().isoformat(timespec="seconds"), fmt % args))


class ThreadingHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # Set by run_server; gates server-side heartbeat-snippet injection so pages
    # only carry the ping when idle shutdown is actually watching.
    idle_shutdown_enabled = False

    def __init__(self, *args: object, **kwargs: object) -> None:
        # Set before binding so a request handled during startup never races an
        # unset attribute.
        self._activity_lock = threading.Lock()
        self._last_activity = time.monotonic()
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def touch(self) -> None:
        with self._activity_lock:
            self._last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        with self._activity_lock:
            return time.monotonic() - self._last_activity


def _watch(httpd: ThreadingHTTPServer, deadline: "float | None", idle_timeout: float,
           poll: float, grace: float, stop_event: threading.Event) -> "str | None":
    """Block until the TTL deadline passes or the server has been idle long enough,
    returning the shutdown reason ("ttl" or "idle"), or None if serving stopped for
    another cause first (the stop_event is set in run_server's finally).

    time.monotonic() does not advance while macOS is asleep, so neither the idle
    clock nor the TTL counts sleep time: a laptop shut overnight resumes with the
    same remaining budget.

    Idle shutdown never fires on first sight of an over-threshold reading. It arms
    a strike, then only shuts down if the server is STILL idle at least `grace`
    seconds later. Any request in between clears the strike. This absorbs the
    wake-from-sleep race where the watchdog samples before a re-shown tab's first
    heartbeat lands."""
    idle_since: "float | None" = None
    while not stop_event.is_set():
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            return "ttl"
        if idle_timeout > 0:
            if httpd.idle_seconds() > idle_timeout:
                if idle_since is None:
                    idle_since = now
                elif (now - idle_since) >= grace and httpd.idle_seconds() > idle_timeout:
                    return "idle"
            else:
                idle_since = None
        if stop_event.wait(poll):  # returns True once serving has stopped
            return None
    return None


def run_server(site_dir: Path, host: str, port: int, ttl: int, idle_timeout: float,
               poll_interval: float = WATCHDOG_POLL_SECONDS,
               idle_grace: float = IDLE_GRACE_SECONDS) -> int:
    validate_site(site_dir)
    handler = functools.partial(NoListingHandler, directory=str(site_dir))
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.idle_shutdown_enabled = idle_timeout > 0
    actual_port = int(httpd.server_address[1])
    pid = os.getpid()
    info = {
        "pid": pid,
        "host": host,
        "port": actual_port,
        "url": f"http://{host}:{actual_port}/",
        "site_dir": str(site_dir),
        "started_at": now_iso(),
        "ttl_seconds": ttl,
        "idle_timeout_seconds": idle_timeout,
        "command": " ".join(sys.argv),
        "manager": "webdoc/serve_site.py",
    }
    server_json(site_dir).write_text(json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Why the server stopped: "ttl" | "idle" | "signal". The signal handler and
    # the watchdog both write here; whichever fires first wins.
    shutdown_reason: dict[str, "str | None"] = {"reason": None}

    def shutdown(signum: int, frame: object) -> None:
        shutdown_reason["reason"] = "signal"
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
    except ValueError:
        # signal.signal only works in the main thread; when run_server is driven
        # from a worker thread (tests) the watchdog and explicit shutdown suffice.
        pass

    stop_event = threading.Event()
    if ttl > 0 or idle_timeout > 0:
        deadline = time.monotonic() + ttl if ttl > 0 else None

        def watchdog() -> None:
            reason = _watch(httpd, deadline, idle_timeout, poll_interval, idle_grace, stop_event)
            if reason and shutdown_reason["reason"] is None:
                shutdown_reason["reason"] = reason
                httpd.shutdown()

        threading.Thread(target=watchdog, daemon=True).start()

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        stop_event.set()  # release the watchdog
        httpd.server_close()
        info["stopped_at"] = now_iso()
        info["shutdown_reason"] = shutdown_reason["reason"]
        try:
            server_json(site_dir).write_text(json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            pass
    return 0


def open_url(url: str) -> bool:
    """Open a URL in the default browser; best-effort, never raises."""
    for cmd in (["open", url], ["xdg-open", url]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except (FileNotFoundError, OSError):
            continue
    try:
        import webbrowser

        return webbrowser.open(url)
    except Exception:
        return False


def start(site_dir: Path, host: str, port: int, ttl: int, idle_timeout: int, allow_lan: bool, allow_symlinks: bool, want_open: bool = False) -> int:
    if host not in LOOPBACK_HOSTS and not allow_lan:
        raise SystemExit("refusing non-loopback host without --allow-lan")
    validate_site(site_dir, allow_symlinks=allow_symlinks)
    info = load_server_info(site_dir)
    if info and pid_alive(int(info.get("pid", -1))) and str(info.get("site_dir")) == str(site_dir):
        if want_open and host in LOOPBACK_HOSTS and info.get("url"):
            open_url(str(info["url"]))
        print(json.dumps(info, indent=2, sort_keys=True))
        return 0

    try:
        server_json(site_dir).unlink()
    except FileNotFoundError:
        pass

    log = (site_dir / "server.log").open("ab")
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run-server",
        str(site_dir),
        "--host",
        host,
        "--port",
        str(port),
        "--ttl",
        str(ttl),
        "--idle-timeout",
        str(idle_timeout),
    ]
    child = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )

    deadline = time.time() + 5
    while time.time() < deadline:
        info = load_server_info(site_dir)
        if info and int(info.get("pid", -1)) == child.pid:
            if want_open and host in LOOPBACK_HOSTS and info.get("url"):
                open_url(str(info["url"]))
            print(json.dumps(info, indent=2, sort_keys=True))
            return 0
        if child.poll() is not None:
            raise SystemExit(f"server failed to start; see {site_dir / 'server.log'}")
        time.sleep(0.1)
    raise SystemExit(f"server did not report readiness; see {site_dir / 'server.log'}")


def stop(site_dir: Path, quiet: bool = False) -> int:
    info = load_server_info(site_dir)
    if not info:
        if not quiet:
            print(json.dumps({"status": "not-running", "site_dir": str(site_dir)}, indent=2))
        return 0
    pid = int(info.get("pid", -1))
    if not pid_alive(pid):
        if not quiet:
            print(json.dumps({"status": "stale", "pid": pid, "site_dir": str(site_dir)}, indent=2))
        return 0
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise SystemExit(f"permission denied stopping pid {pid}: {exc}") from exc

    deadline = time.time() + 5
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.1)
    status = "stopped" if not pid_alive(pid) else "stop-timeout"
    if not quiet:
        print(json.dumps({"status": status, "pid": pid, "site_dir": str(site_dir)}, indent=2))
    return 0 if status == "stopped" else 1


def status(site_dir: Path) -> int:
    info = load_server_info(site_dir)
    if not info:
        print(json.dumps({"status": "not-running", "site_dir": str(site_dir)}, indent=2))
        return 0
    pid = int(info.get("pid", -1))
    alive = pid_alive(pid)
    if alive:
        info["status"] = "running"
    elif info.get("stopped_at"):
        info["status"] = "stopped"
    else:
        info["status"] = "stale"
    print(json.dumps(info, indent=2, sort_keys=True))
    return 0


def cleanup(root: Path, stop_running: bool = False) -> int:
    root = root.expanduser().resolve()
    results: list[dict[str, object]] = []
    for path in root.glob("*/server.json"):
        site_dir = path.parent
        info = load_server_info(site_dir)
        if not info:
            continue
        pid = int(info.get("pid", -1))
        alive = pid_alive(pid)
        if alive and stop_running:
            code = stop(site_dir, quiet=True)
            alive = code != 0
        results.append({"site_dir": str(site_dir), "pid": pid, "alive": alive})
    print(json.dumps({"root": str(root), "servers": results}, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage localhost static preview servers for webdoc sites.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="Start serving a site directory")
    p_start.add_argument("site")
    p_start.add_argument("--host", default="127.0.0.1")
    p_start.add_argument("--port", type=int, default=0, help="0 lets the OS choose an unused port")
    p_start.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS, help="Seconds before the TTL backstop shuts the server down (default 7 days); 0 disables TTL")
    p_start.add_argument("--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT_SECONDS, help="Seconds of no requests before idle shutdown (default 30 min); the served page's heartbeat keeps an open tab alive, so this fires after the last tab goes away. 0 disables idle shutdown")
    p_start.add_argument("--allow-lan", action="store_true")
    p_start.add_argument("--allow-symlinks", action="store_true")
    p_start.add_argument("--open", dest="open", action="store_true", help="Open the site in the browser after start (overrides config)")
    p_start.add_argument("--no-open", dest="no_open", action="store_true", help="Do not open the browser after start (overrides config)")

    p_stop = sub.add_parser("stop", help="Stop a managed server for a site directory")
    p_stop.add_argument("site")

    p_status = sub.add_parser("status", help="Show managed server status for a site directory")
    p_status.add_argument("site")

    p_cleanup = sub.add_parser("cleanup", help="List or stop managed servers under the artifact root")
    p_cleanup.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    p_cleanup.add_argument("--stop-running", action="store_true")

    p_run = sub.add_parser("run-server", help=argparse.SUPPRESS)
    p_run.add_argument("site")
    p_run.add_argument("--host", required=True)
    p_run.add_argument("--port", type=int, required=True)
    p_run.add_argument("--ttl", type=int, required=True)
    p_run.add_argument("--idle-timeout", type=int, required=True)

    args = parser.parse_args()
    if args.command == "start":
        want_open = bool(read_settings().get("auto_open", True))
        if args.no_open:
            want_open = False
        elif args.open:
            want_open = True
        return start(resolve_site(args.site), args.host, args.port, args.ttl, args.idle_timeout, args.allow_lan, args.allow_symlinks, want_open=want_open)
    if args.command == "stop":
        return stop(resolve_site(args.site))
    if args.command == "status":
        return status(resolve_site(args.site))
    if args.command == "cleanup":
        return cleanup(args.root, stop_running=args.stop_running)
    if args.command == "run-server":
        return run_server(resolve_site(args.site), args.host, args.port, args.ttl, args.idle_timeout)
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
