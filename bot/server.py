#!/usr/bin/env python3
"""HTTP layer only: POST /decide -> bot.strategy.decide(). Mirrors the kit's stdlib server.

Host/port default to the kit's 127.0.0.1:8080; set YENO_HOST=0.0.0.0 inside a container.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.strategy import decide  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/decide":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            response = decide(json.loads(self.rfile.read(length)))
            body = json.dumps(response, separators=(",", ":")).encode()
        except Exception:
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> None:
    host = os.environ.get("YENO_HOST", "127.0.0.1")
    port = int(os.environ.get("YENO_PORT", "8080"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
