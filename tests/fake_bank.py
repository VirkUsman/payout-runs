"""A real HTTP server standing in for the bank.

The script is a list of what to do with the next requests, so a test can say
"apply the transfer, then hang up without answering", which a mocked client
cannot express.
"""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeBank:
    def __init__(self):
        self.lock = threading.Lock()
        self.script: list[str] = []
        self.applied: dict[str, dict] = {}
        self.refused: set[str] = set()
        self.keys_seen: list[str] = []
        self.arrived = threading.Event()
        self.release = threading.Event()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def transfers(self) -> int:
        return len(self.applied)

    @property
    def total_cents(self) -> int:
        return sum(t["amount_cents"] for t in self.applied.values())

    def close(self):
        self.release.set()
        self._server.shutdown()
        self._server.server_close()

    def _apply(self, key: str, body: dict) -> tuple[dict, bool]:
        with self.lock:
            if key in self.applied:
                return self.applied[key], False
            record = {**body, "reference": uuid.uuid4().hex}
            self.applied[key] = record
            return record, True

    def _handler(self):
        bank = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key = self.headers["Idempotency-Key"]
                with bank.lock:
                    bank.keys_seen.append(key)
                    step = bank.script.pop(0) if bank.script else "ok"
                    if key in bank.refused:
                        step = "refused"

                if step == "unavailable":
                    return self._send(503, {"error": "maintenance"})
                if step == "busy":
                    return self._send(409, {"error": "same key in progress"})
                if step in ("refuse", "refused"):
                    with bank.lock:
                        bank.refused.add(key)
                    return self._send(422, {"error": "account closed"})

                record, created = bank._apply(key, body)
                if step == "drop_after_applying":
                    self.close_connection = True
                    self.connection.close()
                    return None
                if step == "hold_after_applying":
                    bank.arrived.set()
                    bank.release.wait(30)
                    return None
                return self._send(201 if created else 200, record)

        return Handler
