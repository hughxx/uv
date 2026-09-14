"""Bounded HTTP transport with one serialized application entry point."""

from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .application import AgentApplication
from .protocol import InvalidObservation, encode_response, idle_response

LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 4 * 1024 * 1024
READ_TIMEOUT_SECONDS = 2.0


def reject_nonfinite(value: str) -> None:
    raise ValueError("non-finite JSON number")


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], application: AgentApplication | None = None
    ) -> None:
        self.application = application if application is not None else AgentApplication()
        self.decision_lock = threading.Lock()
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server: AgentHTTPServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(READ_TIMEOUT_SECONDS)

    def do_POST(self) -> None:
        if self.headers.get("Transfer-Encoding") is not None:
            self.reply(400, {"error": "unsupported_transfer_encoding"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self.reply(411, {"error": "content_length_required"})
            return
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            self.reply(400, {"error": "invalid_content_length"})
            return
        try:
            length = int(lengths[0])
        except ValueError:
            self.reply(400, {"error": "invalid_content_length"})
            return
        if length > MAX_REQUEST_BYTES:
            self.reply(413, {"error": "request_too_large"})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete request")
            payload = json.loads(raw.decode("utf-8"), parse_constant=reject_nonfinite)
        except socket.timeout:
            self.reply(408, {"error": "request_timeout"})
            return
        except (ValueError, UnicodeError, RecursionError):
            self.reply(400, {"error": "invalid_json"})
            return

        # This guard is not a hard timeout for future decision code. Its work
        # must obey the shared deadline specified in the architecture.
        if not self.server.decision_lock.acquire(timeout=0.1):
            self.reply(503, {"error": "decision_busy"})
            return
        try:
            try:
                response = self.server.application.handle_turn(payload)
                body = encode_response(response)
            except InvalidObservation:
                self.reply(400, {"error": "invalid_observation"})
                return
            except Exception as error:
                # Do not log raw observations, task contents or command output.
                LOGGER.error("decision failed (%s)", type(error).__name__)
                body = encode_response(idle_response())
            self.reply(200, body)
        finally:
            self.server.decision_lock.release()

    def reply(self, status: int, payload: dict[str, Any] | bytes) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            LOGGER.warning("client disconnected before response completed")

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(port: int, application: AgentApplication | None = None) -> None:
    with AgentHTTPServer(("0.0.0.0", port), application) as server:
        LOGGER.info("listening on 0.0.0.0:%d (baseline strategy)", port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
