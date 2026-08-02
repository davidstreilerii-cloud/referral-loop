"""A TLS server that answers /metadata and a token endpoint, for testing preflight.

Real TLS on loopback rather than a mocked opener, because three of the four things egress.py
overrides -- the TLS floor, the timeout, the redirect refusal -- do not exist at all in a mocked
transport. A test that patches urlopen proves the code calls urlopen.

Configurable per test: the fhirVersion it advertises, whether the token endpoint succeeds, and
whether either endpoint redirects instead of answering.
"""
from __future__ import annotations

import http.server
import json
import ssl
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class ServerBehaviour:
    fhir_version: str = "4.0.1"
    token_status: int = 200
    token_body: dict = field(default_factory=lambda: {"access_token": "test-token", "expires_in": 300})
    redirect_metadata_to: str | None = None
    redirect_token_to: str | None = None
    requests: list = field(default_factory=list)


def _handler_for(behaviour: ServerBehaviour):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # keep pytest output readable
            return

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, target: str) -> None:
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's interface
            behaviour.requests.append(("GET", self.path))
            if not self.path.endswith("/metadata"):
                self._send(404, {"resourceType": "OperationOutcome"})
                return
            if behaviour.redirect_metadata_to:
                self._redirect(behaviour.redirect_metadata_to)
                return
            self._send(
                200,
                {
                    "resourceType": "CapabilityStatement",
                    "status": "active",
                    "fhirVersion": behaviour.fhir_version,
                    "format": ["json"],
                },
            )

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            behaviour.requests.append(("POST", self.path, self.rfile.read(length).decode("ascii")))
            if behaviour.redirect_token_to:
                self._redirect(behaviour.redirect_token_to)
                return
            self._send(behaviour.token_status, behaviour.token_body)

    return Handler


@contextmanager
def fhir_server(certfile, keyfile, behaviour: ServerBehaviour | None = None):
    """Yields (base_url, behaviour). The certificate must carry `localhost` as a SAN."""
    behaviour = behaviour or ServerBehaviour()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(behaviour))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    server.socket = context.wrap_socket(server.socket, server_side=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{server.server_address[1]}", behaviour
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
