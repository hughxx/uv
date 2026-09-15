"""Bounded HTTP transport with one serialized application entry point."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from typing import Any

from .application import AgentApplication
from .protocol import InvalidObservation, encode_response, idle_response
from .telemetry import decision_outline, exception_location, input_outline, log_event, response_outline, startup_fields

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
        self.request_ids = count(1)
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server: AgentHTTPServer

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(READ_TIMEOUT_SECONDS)
        self.request_id = next(self.server.request_ids)
        self.started = time.monotonic()
        self.input_info: dict[str, Any] = {}
        self.decision_info: dict[str, Any] = {}
        self.response_info: dict[str, Any] = {}
        self.error_info: dict[str, Any] = {}
        self.decision_failed = False

    def do_POST(self) -> None:
        self.started = time.monotonic()
        declared = self.headers.get("Content-Length")
        declared_bytes = int(declared) if declared and declared.isascii() and declared.isdecimal() and len(declared) < 10 else None
        log_event(LOGGER, "request_received", id=self.request_id, method="POST", declaredBytes=declared_bytes,
                  transferEncodingPresent=self.headers.get("Transfer-Encoding") is not None)
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
        try:
            self.input_info = input_outline(payload)
        except Exception as error:
            log_event(LOGGER, "telemetry_error", level=logging.WARNING, id=self.request_id, error=exception_location(error))

        # This guard is not a hard timeout for future decision code. Its work
        # must obey the shared deadline specified in the architecture.
        if not self.server.decision_lock.acquire(timeout=0.1):
            self.reply(503, {"error": "decision_busy"})
            return
        try:
            try:
                response = self.server.application.handle_turn(payload)
                body = encode_response(response)
            except InvalidObservation as error:
                self.error_info = exception_location(error)
                self.reply(400, {"error": "invalid_observation"})
                return
            except Exception as error:
                self.error_info = exception_location(error)
                self.decision_failed = True
                response = idle_response()
                body = encode_response(response)
            try:
                self.response_info = response_outline(response)
                self.decision_info = decision_outline(self.server.application)
            except Exception as error:
                log_event(LOGGER, "telemetry_error", level=logging.WARNING, id=self.request_id, error=exception_location(error))
            self.reply(200, body)
        finally:
            self.server.decision_lock.release()

    def reply(self, status: int, payload: dict[str, Any] | bytes) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.close_connection = True
        delivered = False
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            delivered = True
        except OSError:
            pass
        finally:
            reason = payload.get("error", "") if isinstance(payload, dict) else "decision_exception" if self.decision_failed else ""
            suspicious_probe = self.decision_info.get("status") == "probe" and set(self.input_info.get("keys", [])) != {"roundNo"}
            if suspicious_probe:
                reason = "missing_game_fields_treated_as_probe"
            level = logging.ERROR if self.decision_failed else logging.WARNING if status >= 400 or suspicious_probe or not delivered else logging.INFO
            log_event(LOGGER, "turn", level=level, id=self.request_id, round=self.input_info.get("round"), http=status,
                      status=self.decision_info.get("status", "rejected"), reason=reason, responseWritten=delivered,
                      elapsedMs=round((time.monotonic() - self.started) * 1000, 3), responseBytes=len(body),
                      input=self.input_info, decision=self.decision_info, error=self.error_info, **self.response_info)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        # Covers unsupported HTTP methods and malformed request lines too.
        # Do not echo paths, headers or the base handler's arbitrary message.
        method = getattr(self, "command", None)
        log_event(LOGGER, "http_rejected", level=logging.WARNING, id=self.request_id, http=code,
                  method=method if method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"} else "other",
                  reason="unsupported_method" if code == 501 else "malformed_http")
        super().send_error(code, message, explain)

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(port: int, application: AgentApplication | None = None) -> None:
    with AgentHTTPServer(("0.0.0.0", port), application) as server:
        log_event(LOGGER, "listening", host="0.0.0.0", port=port, **startup_fields(server.application))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
