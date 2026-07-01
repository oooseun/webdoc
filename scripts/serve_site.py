#!/usr/bin/env python3
"""Manage a loopback-only static server for generated webdoc sites."""

from __future__ import annotations

import argparse
import functools
import http.server
import ipaddress
import json
import os
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
DEFAULT_TTL_SECONDS = 4 * 60 * 60
MAX_FEEDBACK_BYTES = 64 * 1024
MAX_EDIT_BYTES = 512 * 1024
FEEDBACK_LOCK = threading.Lock()
EDIT_LOCK = threading.Lock()


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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json(200, {"ok": True, "site_dir": str(self.site_dir())})
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
        super().do_GET()

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
        parsed = urlparse(self.path)
        if parsed.path == "/api/feedback":
            self.handle_feedback()
        elif parsed.path == "/api/edit":
            self.handle_edit()
        elif parsed.path == "/api/undo":
            self.handle_undo()
        else:
            self.send_json(404, {"error": "not_found"})

    def handle_feedback(self) -> None:
        payload = self._read_json_body(MAX_FEEDBACK_BYTES)
        if payload is None:
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
        path = feedback_jsonl(self.site_dir())
        with FEEDBACK_LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
        self.send_json(200, {"ok": True, "feedback_path": str(path), "received_at": entry["received_at"]})

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
        except Exception as exc:  # never leak a stack trace to the client
            self.log_message("undo error: %r", exc)
            self.send_json(500, {"error": "undo_failed"})
            return
        self.send_json(status, body)

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


def run_server(site_dir: Path, host: str, port: int, ttl: int) -> int:
    validate_site(site_dir)
    handler = functools.partial(NoListingHandler, directory=str(site_dir))
    httpd = ThreadingHTTPServer((host, port), handler)
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
        "command": " ".join(sys.argv),
        "manager": "webdoc/serve_site.py",
    }
    server_json(site_dir).write_text(json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def shutdown(signum: int, frame: object) -> None:
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if ttl > 0:
        deadline = time.monotonic() + ttl

        def ttl_loop() -> None:
            while time.monotonic() < deadline:
                time.sleep(min(5, max(0.1, deadline - time.monotonic())))
            httpd.shutdown()

        threading.Thread(target=ttl_loop, daemon=True).start()

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        info["stopped_at"] = now_iso()
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


def start(site_dir: Path, host: str, port: int, ttl: int, allow_lan: bool, allow_symlinks: bool, want_open: bool = False) -> int:
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
    p_start.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS, help="Seconds before auto-shutdown; 0 disables TTL")
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

    args = parser.parse_args()
    if args.command == "start":
        want_open = bool(read_settings().get("auto_open", True))
        if args.no_open:
            want_open = False
        elif args.open:
            want_open = True
        return start(resolve_site(args.site), args.host, args.port, args.ttl, args.allow_lan, args.allow_symlinks, want_open=want_open)
    if args.command == "stop":
        return stop(resolve_site(args.site))
    if args.command == "status":
        return status(resolve_site(args.site))
    if args.command == "cleanup":
        return cleanup(args.root, stop_running=args.stop_running)
    if args.command == "run-server":
        return run_server(resolve_site(args.site), args.host, args.port, args.ttl)
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
