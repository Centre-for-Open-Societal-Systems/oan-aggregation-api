"""A stand-in partner endpoint, so the callback half of the flow is observable.

The aggregator POSTs its on-search envelope to whatever `sender_uri` said. In a
test there is nobody on the other end, so this plays the partner: it accepts the
POST, keeps the body, and serves it back so the test can assert on what a real
partner would actually have received.

    POST /on-search   <- the aggregator delivers here
    GET  /last        -> the most recent envelope, or {} if none
    GET  /all         -> every envelope received, newest last
    POST /reset       -> forget everything
"""
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("CALLBACK_PORT", "9099"))
RECEIVED = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, payload):
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/reset":
            RECEIVED.clear()
            return self._send(200, {"ok": True, "cleared": True})
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {"_unparseable": raw.decode("utf-8", "replace")[:2000]}
        RECEIVED.append({"received_at": datetime.now(timezone.utc).isoformat(),
                         "path": path, "body": body})
        sys.stderr.write("[callback] %s received (%d bytes)\n" % (path, length))
        # Answer the way a partner would, so the aggregator records a success.
        return self._send(200, {"ack": True})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/last":
            return self._send(200, RECEIVED[-1] if RECEIVED else {})
        if path == "/all":
            return self._send(200, {"count": len(RECEIVED), "items": RECEIVED})
        if path == "/health":
            return self._send(200, {"ok": True, "received": len(RECEIVED)})
        return self._send(404, {"error": "not_found"})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print("callback receiver on :%d" % PORT, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
