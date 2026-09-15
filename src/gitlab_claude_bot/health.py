import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)


@dataclass
class Status:
    poll_interval: int
    job_timeout: int
    last_poll_ok: datetime | None = None
    last_poll_error: str | None = None
    job_running: str | None = None
    jobs_done: int = 0
    jobs_failed: int = 0
    bot_username: str | None = None

    def healthy(self, now: datetime) -> bool:
        if self.last_poll_ok is None:
            return False
        if self.job_running:
            return True
        return now - self.last_poll_ok < timedelta(seconds=3 * self.poll_interval + self.job_timeout)

    def report(self, now: datetime) -> dict:
        fields = asdict(self)
        fields["last_poll_ok"] = self.last_poll_ok.isoformat() if self.last_poll_ok else None
        return {"healthy": self.healthy(now), **fields}


class _HealthServer(ThreadingHTTPServer):
    def __init__(self, port: int, status: Status):
        super().__init__(("0.0.0.0", port), _Handler)
        self.status = status


class _Handler(BaseHTTPRequestHandler):
    server: _HealthServer

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        status = self.server.status
        now = datetime.now(UTC)
        body = json.dumps(status.report(now), indent=2).encode()
        self.send_response(200 if status.healthy(now) else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        log.debug("health: " + format, *args)


class Server(threading.Thread):
    def __init__(self, port: int, status: Status):
        self.http = _HealthServer(port, status)
        super().__init__(target=self.http.serve_forever, name="health", daemon=True)

    @property
    def port(self) -> int:
        return self.http.server_port


def serve(port: int, status: Status) -> Server:
    server = Server(port, status)
    server.start()
    log.info("health endpoint on port %d", server.port)
    return server
