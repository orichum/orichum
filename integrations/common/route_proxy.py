#!/usr/bin/env python3
"""Transparent bounded same-family recovery proxy for Orichum routes."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .leanctx_profiles import (
    LEANCTX_PROFILE_FULL,
    resident_tool_names,
)
from .model_routing import ROLES
from .orichum_sessions import LogicalSessionError
from .route_selection import Route
from .route_status import RouteStatusStore
from .tool_deferral import transform_request

RETRYABLE_STATUSES = frozenset({401, 403, 408, 429, 500, 502, 503, 504})
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_CONCURRENT_REQUESTS = 32
CLIENT_READ_TIMEOUT_SECONDS = 30
ATTESTATION_TTL_SECONDS = 30
MAX_RESPONSE_PRELUDE_BYTES = 1024 * 1024
RESPONSE_READ_BYTES = 64 * 1024
_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_LOGICAL_SESSION_ID = re.compile(r"oc-s-[a-f0-9]{16}")


class RouteProxyError(RuntimeError):
    """A request cannot be proxied without violating the recovery contract."""


class RequestTooLarge(RouteProxyError):
    """An inbound request exceeds the local proxy safety bound."""


class UpstreamStreamError(RouteProxyError):
    """An upstream response failed before or during delivery."""

    def __init__(
        self,
        http_status: int | None,
        bytes_forwarded: int,
    ):
        super().__init__("upstream response stream failed")
        self.http_status = http_status
        self.bytes_forwarded = bytes_forwarded


class ClientStreamError(RouteProxyError):
    """The downstream client disconnected during response delivery."""

    def __init__(self, http_status: int, bytes_forwarded: int):
        super().__init__("client response stream failed")
        self.http_status = http_status
        self.bytes_forwarded = bytes_forwarded


@dataclass(frozen=True)
class ProxyConfig:
    upstream_port: int
    state_home: Path
    data_home: Path | None = None
    cooldown_seconds: int = 60
    catalog_port: int | None = None

    def selected_port(self, catalog: bool) -> int:
        if catalog and self.catalog_port is not None:
            return self.catalog_port
        return self.upstream_port


@dataclass
class PreparedResponse:
    connection: http.client.HTTPConnection
    response: http.client.HTTPResponse
    prelude: bytes


@dataclass
class RouteRequestTrace:
    request_id: str
    session_id: str | None
    status_store: RouteStatusStore
    event_logger: Callable[[dict[str, object]], None]
    started: float
    selected_route: Route | None = None
    route_state: str | None = None
    route_reason: str | None = None

    def select(self, route: Route, state: str, reason: str) -> None:
        self.selected_route = route
        self.route_state = state
        self.route_reason = reason
        if self.session_id is not None:
            self.status_store.select(
                self.session_id,
                self.request_id,
                route,
                route_state=state,
                reason=reason,
            )

    def emit(self, event: str, **fields: object) -> None:
        document: dict[str, object] = {
            "event": event,
            "requestId": self.request_id,
            "durationMs": round((time.monotonic() - self.started) * 1000),
            **fields,
        }
        if self.session_id is not None:
            document["sessionId"] = self.session_id
        if self.selected_route is not None:
            document.update(
                {
                    "accountId": self.selected_route.account_id,
                    "provider": self.selected_route.provider,
                    "logicalModel": self.selected_route.logical_model,
                    "routeState": self.route_state,
                    "reason": self.route_reason,
                }
            )
        self.event_logger(document)

    def streaming(self, http_status: int) -> None:
        if self.session_id is not None and self.selected_route is not None:
            self.status_store.streaming(
                self.session_id,
                self.request_id,
                http_status,
            )
        self.emit("route-streaming", httpStatus=http_status)

    def complete(self, http_status: int, bytes_forwarded: int) -> None:
        if self.session_id is not None and self.selected_route is not None:
            self.status_store.complete(
                self.session_id,
                self.request_id,
                http_status,
                bytes_forwarded,
            )
        self.emit(
            "route-complete",
            httpStatus=http_status,
            bytesForwarded=bytes_forwarded,
        )

    def fail(
        self,
        failure_kind: str,
        http_status: int | None,
        bytes_forwarded: int,
        stage: str,
    ) -> None:
        if self.session_id is not None and self.selected_route is not None:
            self.status_store.fail(
                self.session_id,
                self.request_id,
                http_status,
                bytes_forwarded,
                failure_kind,
            )
        self.emit(
            "route-failed",
            httpStatus=http_status,
            bytesForwarded=bytes_forwarded,
            failureKind=failure_kind,
            stage=stage,
        )


class RouteIndex:
    def __init__(self, state_home: Path):
        self.state_home = Path(state_home)

    def routes_for(
        self, session_id: str | None, primary_model: str
    ) -> tuple[Route, Route | None] | None:
        return self.request_policy_for(session_id, primary_model)[0]

    def request_policy_for(
        self, session_id: str | None, primary_model: str
    ) -> tuple[tuple[Route, Route | None] | None, str]:
        if session_id is None:
            return None, LEANCTX_PROFILE_FULL
        from .orichum_sessions import load_logical_session

        session = load_logical_session(self.state_home, session_id)
        bindings = (
            session.controller,
            *(session.agents[role] for role in ROLES),
        )
        matches = {
            (
                binding.primary,
                binding.fallbacks[0] if binding.fallbacks else None,
            )
            for binding in bindings
            if binding.primary.upstream_model == primary_model
        }
        routes = next(iter(matches)) if len(matches) == 1 else None
        return routes, session.leanctx_profile

    def fallback_for(
        self, session_id: str | None, primary_model: str
    ) -> str | None:
        routes = self.routes_for(session_id, primary_model)
        return (
            None
            if routes is None or routes[1] is None
            else routes[1].upstream_model
        )


class Cooldowns:
    def __init__(self, seconds: int, clock: Callable[[], float] = time.monotonic):
        self.seconds = seconds
        self.clock = clock
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def active(self, model: str) -> bool:
        now = self.clock()
        with self._lock:
            until = self._until.get(model, 0)
            if until <= now:
                self._until.pop(model, None)
                return False
            return True

    def trip(self, model: str) -> None:
        with self._lock:
            self._until[model] = self.clock() + self.seconds

    def clear(self, model: str) -> None:
        with self._lock:
            self._until.pop(model, None)


class AttestationGate:
    def __init__(
        self,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._verified_until = 0.0
        self._service_pid: int | None = None
        self._refreshing = False
        self._generation = 0
        self._condition = threading.Condition()

    def verify(
        self,
        client_port: int,
        full_verifier: Callable[[int], int],
    ) -> None:
        while True:
            with self._condition:
                observed_generation = self._generation
                while self._refreshing:
                    self._condition.wait()
                if (
                    self._generation != observed_generation
                    and self._service_pid is None
                ):
                    raise RouteProxyError(
                        "upstream ownership could not be verified"
                    )
                if (
                    self._service_pid is not None
                    and self._verified_until > self.clock()
                ):
                    return
                self._refreshing = True
                refresh_generation = self._generation

            try:
                service_pid = full_verifier(client_port)
                if (
                    not isinstance(service_pid, int)
                    or isinstance(service_pid, bool)
                    or service_pid <= 0
                ):
                    raise RouteProxyError(
                        "upstream service identity is invalid"
                    )
            except BaseException:
                with self._condition:
                    self._service_pid = None
                    self._verified_until = 0.0
                    self._refreshing = False
                    self._generation += 1
                    self._condition.notify_all()
                raise

            with self._condition:
                if self._generation != refresh_generation:
                    self._refreshing = False
                    self._condition.notify_all()
                    continue
                self._service_pid = service_pid
                self._verified_until = self.clock() + self.ttl_seconds
                self._refreshing = False
                self._generation += 1
                self._condition.notify_all()
                return

    def invalidate(self) -> None:
        with self._condition:
            self._service_pid = None
            self._verified_until = 0.0
            self._generation += 1
            self._condition.notify_all()


def _request_model(body: bytes) -> str | None:
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        return None
    model = document.get("model") if isinstance(document, dict) else None
    return model if isinstance(model, str) else None


def _strip_unsupported_prompt_cache_retention(body: bytes) -> bytes:
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        return body
    model = document.get("model") if isinstance(document, dict) else None
    if (
        not isinstance(model, str)
        or model.rsplit("/", 1)[-1] != "gpt-5.6-sol"
        or "prompt_cache_retention" not in document
    ):
        return body
    document.pop("prompt_cache_retention")
    return json.dumps(
        document, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _replace_model(body: bytes, expected: str, replacement: str) -> bytes:
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as failure:
        raise RouteProxyError("request body is not valid JSON") from failure
    if not isinstance(document, dict) or document.get("model") != expected:
        raise RouteProxyError("request model changed before recovery")
    document["model"] = replacement
    return json.dumps(
        document, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _upstream_path(path: str) -> str:
    profile_prefix = "/proxy/gpt"
    route_path, separator, query = path.partition("?")
    if route_path == profile_prefix:
        normalized = "/"
    elif route_path.startswith(f"{profile_prefix}/"):
        normalized = route_path[len(profile_prefix) :]
    elif route_path.startswith("/proxy/"):
        raise RouteProxyError("request names an unowned Claudex profile")
    else:
        normalized = route_path
    return normalized + (f"?{query}" if separator else "")


def _read_request_body(handler: BaseHTTPRequestHandler) -> bytes:
    raw_length = handler.headers.get("Content-Length")
    if raw_length is None:
        if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            total = 0
            while True:
                line = handler.rfile.readline(128)
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError as failure:
                    raise RouteProxyError("invalid chunked request") from failure
                if size == 0:
                    while handler.rfile.readline(8192) not in (b"\r\n", b"\n", b""):
                        pass
                    break
                if size < 0 or total + size > MAX_REQUEST_BYTES:
                    raise RequestTooLarge("request body is too large")
                chunks.append(handler.rfile.read(size))
                if len(chunks[-1]) != size:
                    raise RouteProxyError("request body ended early")
                total += size
                if handler.rfile.read(2) != b"\r\n":
                    raise RouteProxyError("invalid chunked request boundary")
            return b"".join(chunks)
        return b""
    try:
        length = int(raw_length, 10)
    except ValueError as failure:
        raise RouteProxyError("invalid Content-Length") from failure
    if length < 0:
        raise RouteProxyError("invalid Content-Length")
    if length > MAX_REQUEST_BYTES:
        raise RequestTooLarge("request body is too large")
    body = handler.rfile.read(length)
    if len(body) != length:
        raise RouteProxyError("request body ended early")
    return body


def _has_sse_data_event(payload: bytes) -> bool:
    normalized = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    for event in normalized.split(b"\n\n")[:-1]:
        if any(
            line == b"data" or line.startswith(b"data:")
            for line in event.split(b"\n")
        ):
            return True
    return False


def _write_route_event(document: dict[str, object]) -> None:
    print(
        json.dumps(document, sort_keys=True, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )


class RouteProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OrichumRouteProxy/1"
    sys_version = ""

    def log_message(self, _format: str, *_arguments: object) -> None:
        return

    def send_response(
        self, code: int, message: str | None = None
    ) -> None:
        super().send_response(code, message)
        request_id = getattr(self, "_request_id", None)
        if request_id is not None:
            self.send_header("X-Orichum-Request-ID", request_id)

    def _upstream(
        self,
        body: bytes,
        *,
        catalog: bool = False,
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        config: ProxyConfig = self.server.proxy_config
        upstream_port = config.selected_port(catalog)
        connection = http.client.HTTPConnection(
            "127.0.0.1", upstream_port, timeout=300
        )
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_HEADERS
            and key.lower() not in {
                "host",
                "content-length",
                "x-orichum-request-id",
                "x-orichum-session-id",
            }
        }
        headers["Content-Length"] = str(len(body))
        headers["Connection"] = "close"
        headers["X-Orichum-Request-ID"] = self._request_id
        connected = None
        try:
            connected = self._open_upstream_socket(catalog=catalog)
            connection.sock = connected
            connected = None
            connection.request(
                self.command,
                _upstream_path(self.path),
                body=body,
                headers=headers,
            )
            return connection, connection.getresponse()
        except Exception:
            if connected is not None:
                connected.close()
            connection.close()
            if config.data_home is not None:
                gate = (
                    self.server.catalog_attestation_gate
                    if catalog
                    else self.server.attestation_gate
                )
                gate.invalidate()
            raise

    def _candidate_upstream(
        self,
        original: bytes,
        resident_names: Collection[str],
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        original = _strip_unsupported_prompt_cache_retention(original)
        candidate = transform_request(original, resident_names)
        connection, response = self._upstream(candidate.body)
        if candidate.transformed and response.status in {400, 422}:
            connection.close()
            return self._upstream(original)
        return connection, response

    def _open_upstream_socket(self, *, catalog: bool = False) -> socket.socket:
        config: ProxyConfig = self.server.proxy_config
        upstream_port = config.selected_port(catalog)
        connected = socket.create_connection(
            ("127.0.0.1", upstream_port), timeout=3
        )
        try:
            connected.settimeout(300)
            client_host, client_port = connected.getsockname()
            if client_host != "127.0.0.1":
                raise RouteProxyError("upstream connection is not loopback")
            if config.data_home is not None:
                verifier = (
                    Path(__file__).resolve().parents[2]
                    / "bin"
                    / (
                        "orichum-verify-cliproxy"
                        if catalog
                        else "orichum-verify-leanctx-proxy"
                    )
                )

                def run_verifier(
                    arguments: list[str],
                ) -> tuple[subprocess.CompletedProcess[str], float]:
                    started = time.monotonic()
                    try:
                        completed = subprocess.run(
                            [str(verifier), *arguments],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            check=False,
                            timeout=3,
                            text=True,
                        )
                    except subprocess.TimeoutExpired as failure:
                        duration = time.monotonic() - started
                        raise RouteProxyError(
                            "upstream ownership verifier timed out after "
                            f"{duration:.3f}s"
                        ) from failure
                    except (OSError, subprocess.SubprocessError) as failure:
                        duration = time.monotonic() - started
                        raise RouteProxyError(
                            "upstream ownership verifier failed after "
                            f"{duration:.3f}s: {failure}"
                        ) from failure
                    return completed, time.monotonic() - started

                def full_verify(current_client_port: int) -> int:
                    completed, duration = run_verifier(
                        [
                            str(config.data_home),
                            str(upstream_port),
                            str(current_client_port),
                        ]
                    )
                    if completed.returncode != 0:
                        raise RouteProxyError(
                            "upstream ownership verification failed after "
                            f"{duration:.3f}s with exit "
                            f"{completed.returncode}"
                        )
                    service_pid = completed.stdout.strip()
                    if not service_pid.isascii() or not service_pid.isdecimal():
                        raise RouteProxyError(
                            "upstream service identity is invalid"
                        )
                    return int(service_pid, 10)

                gate = (
                    self.server.catalog_attestation_gate
                    if catalog
                    else self.server.attestation_gate
                )
                gate.verify(client_port, full_verify)
            return connected
        except Exception:
            connected.close()
            raise

    def _prepare_response(
        self,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
    ) -> PreparedResponse:
        try:
            content_type = response.getheader("Content-Type", "")
            media_type = content_type.partition(";")[0].strip().lower()
            if media_type == "text/event-stream":
                buffered = bytearray()
                while len(buffered) < MAX_RESPONSE_PRELUDE_BYTES:
                    block = response.read1(
                        min(
                            RESPONSE_READ_BYTES,
                            MAX_RESPONSE_PRELUDE_BYTES - len(buffered),
                        )
                    )
                    if not block:
                        raise UpstreamStreamError(response.status, 0)
                    buffered.extend(block)
                    if _has_sse_data_event(buffered):
                        return PreparedResponse(
                            connection,
                            response,
                            bytes(buffered),
                        )
                raise UpstreamStreamError(response.status, 0)

            raw_length = response.getheader("Content-Length")
            content_length = None
            if raw_length is not None:
                try:
                    content_length = int(raw_length, 10)
                except ValueError:
                    content_length = None
            if (
                content_length is not None
                and 0 <= content_length <= MAX_RESPONSE_PRELUDE_BYTES
            ):
                prelude = response.read(content_length)
                if len(prelude) != content_length:
                    raise UpstreamStreamError(response.status, 0)
            else:
                prelude = response.read1(RESPONSE_READ_BYTES)
                if (
                    not prelude
                    and isinstance(content_length, int)
                    and content_length > 0
                ):
                    raise UpstreamStreamError(response.status, 0)
            return PreparedResponse(connection, response, prelude)
        except UpstreamStreamError:
            connection.close()
            raise
        except (http.client.HTTPException, TimeoutError, OSError) as failure:
            connection.close()
            raise UpstreamStreamError(response.status, 0) from failure

    def _send_response(self, prepared: PreparedResponse) -> int:
        connection = prepared.connection
        response = prepared.response
        bytes_forwarded = 0
        try:
            try:
                self._response_started = True
                self.send_response_only(response.status, response.reason)
                for key, value in response.getheaders():
                    if (
                        key.lower() not in _HOP_HEADERS
                        and key.lower()
                        not in {"content-length", "x-orichum-request-id"}
                    ):
                        self.send_header(key, value)
                self.send_header("X-Orichum-Request-ID", self._request_id)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                if prepared.prelude:
                    self.wfile.write(prepared.prelude)
                    self.wfile.flush()
                    bytes_forwarded += len(prepared.prelude)
            except OSError as failure:
                raise ClientStreamError(
                    response.status, bytes_forwarded
                ) from failure

            while True:
                try:
                    block = response.read1(RESPONSE_READ_BYTES)
                except (
                    http.client.HTTPException,
                    TimeoutError,
                    OSError,
                ) as failure:
                    raise UpstreamStreamError(
                        response.status, bytes_forwarded
                    ) from failure
                if not block:
                    remaining = response.length
                    if isinstance(remaining, int) and remaining > 0:
                        raise UpstreamStreamError(
                            response.status, bytes_forwarded
                        )
                    break
                try:
                    self.wfile.write(block)
                    self.wfile.flush()
                    bytes_forwarded += len(block)
                except OSError as failure:
                    raise ClientStreamError(
                        response.status, bytes_forwarded
                    ) from failure
            return bytes_forwarded
        finally:
            connection.close()

    def _safe_error(self, status: int, message: str) -> None:
        if getattr(self, "_response_started", False):
            self.close_connection = True
            return
        self.close_connection = True
        self.send_error(status, message)

    def _health(self) -> None:
        ready = False
        try:
            config: ProxyConfig = self.server.proxy_config
            ports = {config.upstream_port, config.selected_port(True)}
            for port in ports:
                connected = socket.create_connection(
                    ("127.0.0.1", port),
                    timeout=1,
                )
                connected.close()
            ready = True
        except (TimeoutError, OSError):
            pass
        body = json.dumps(
            {
                "pid": os.getpid(),
                "ready": ready,
                "service": "orichum-route-proxy",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response_only(200 if ready else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _status_json(self, status: int, document: dict[str, object]) -> None:
        body = json.dumps(
            document, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _status(self) -> None:
        parameters = parse_qsl(
            urlsplit(self.path).query, keep_blank_values=True
        )
        if (
            len(parameters) != 1
            or parameters[0][0] != "session_id"
            or not _LOGICAL_SESSION_ID.fullmatch(parameters[0][1])
        ):
            self._status_json(400, {"error": "invalid status request"})
            return
        status = self.server.route_status_store.get(parameters[0][1])
        if status is None:
            self._status_json(404, {"error": "status not found"})
            return
        self._status_json(200, status.as_public_json())

    def _fallback_upstream(
        self,
        body: bytes,
        primary: str,
        fallback_route: Route,
        resident_names: Collection[str],
        trace: RouteRequestTrace,
        *,
        cause: str,
        http_status: int | None,
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        previous_account = (
            None
            if trace.selected_route is None
            else trace.selected_route.account_id
        )
        trace.select(fallback_route, "fallback", "retry")
        trace.emit(
            "route-retry",
            cause=cause,
            fromAccountId=previous_account,
            httpStatus=http_status,
        )
        fallback_body = _replace_model(
            body, primary, fallback_route.upstream_model
        )
        return self._candidate_upstream(fallback_body, resident_names)

    def _prepare_with_fallback(
        self,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
        body: bytes,
        primary: str | None,
        fallback_route: Route | None,
        resident_names: Collection[str],
        trace: RouteRequestTrace,
        used_primary: bool,
    ) -> tuple[PreparedResponse, http.client.HTTPResponse, bool]:
        try:
            return self._prepare_response(connection, response), response, used_primary
        except UpstreamStreamError as failure:
            if not used_primary or primary is None or fallback_route is None:
                raise
            self.server.cooldowns.trip(primary)
            connection, response = self._fallback_upstream(
                body,
                primary,
                fallback_route,
                resident_names,
                trace,
                cause="pre-output-stream",
                http_status=failure.http_status,
            )
            return self._prepare_response(connection, response), response, False

    def _deliver_response(
        self,
        prepared: PreparedResponse,
        response: http.client.HTTPResponse,
        trace: RouteRequestTrace,
        *,
        used_primary: bool,
        primary: str | None,
        has_fallback: bool,
    ) -> None:
        trace.streaming(response.status)
        try:
            bytes_forwarded = self._send_response(prepared)
        except UpstreamStreamError as failure:
            if used_primary and primary is not None and has_fallback:
                self.server.cooldowns.trip(primary)
            trace.fail(
                "upstream",
                failure.http_status,
                failure.bytes_forwarded,
                "after-output",
            )
            return
        except ClientStreamError as failure:
            trace.fail(
                "client",
                failure.http_status,
                failure.bytes_forwarded,
                "after-output",
            )
            return

        if used_primary and primary is not None and response.status < 400:
            self.server.cooldowns.clear(primary)
        trace.complete(response.status, bytes_forwarded)

    def _handle(self) -> None:
        self._response_started = False
        self._request_id = f"oc-rq-{secrets.token_hex(8)}"
        self.connection.settimeout(CLIENT_READ_TIMEOUT_SECONDS)
        trace = RouteRequestTrace(
            self._request_id,
            None,
            self.server.route_status_store,
            self.server.event_logger,
            time.monotonic(),
        )

        try:
            body = _read_request_body(self)
            if (
                self.command == "GET"
                and urlsplit(_upstream_path(self.path)).path == "/v1/models"
            ):
                connection, response = self._upstream(body, catalog=True)
                prepared = self._prepare_response(connection, response)
                self._send_response(prepared)
                return

            primary = _request_model(body)
            supplied_session_id = self.headers.get("X-Orichum-Session-ID")
            trace.session_id = (
                supplied_session_id
                if supplied_session_id is not None
                and _LOGICAL_SESSION_ID.fullmatch(supplied_session_id)
                else None
            )
            routes, leanctx_profile = (
                self.server.route_index.request_policy_for(
                    trace.session_id, primary
                )
                if primary is not None
                else (None, LEANCTX_PROFILE_FULL)
            )
            resident_names = resident_tool_names(leanctx_profile)
            primary_route = None if routes is None else routes[0]
            fallback_route = None if routes is None else routes[1]
            used_primary = not (
                primary is not None
                and fallback_route is not None
                and self.server.cooldowns.active(primary)
            )
            if used_primary:
                request_body = body
                if primary_route is not None:
                    trace.select(primary_route, "primary", "primary")
            else:
                trace.select(fallback_route, "fallback", "cooldown")
                request_body = _replace_model(
                    body, primary, fallback_route.upstream_model
                )

            connection, response = self._candidate_upstream(
                request_body,
                resident_names,
            )
            if (
                used_primary
                and primary is not None
                and fallback_route is not None
                and response.status in RETRYABLE_STATUSES
            ):
                connection.close()
                self.server.cooldowns.trip(primary)
                connection, response = self._fallback_upstream(
                    body,
                    primary,
                    fallback_route,
                    resident_names,
                    trace,
                    cause="http-status",
                    http_status=response.status,
                )
                used_primary = False

            prepared, response, used_primary = self._prepare_with_fallback(
                connection,
                response,
                body,
                primary,
                fallback_route,
                resident_names,
                trace,
                used_primary,
            )
            self._deliver_response(
                prepared,
                response,
                trace,
                used_primary=used_primary,
                primary=primary,
                has_fallback=fallback_route is not None,
            )
        except RequestTooLarge:
            self._safe_error(413, "Orichum request body is too large")
        except UpstreamStreamError as failure:
            trace.fail(
                "upstream",
                failure.http_status,
                failure.bytes_forwarded,
                "before-output",
            )
            self._safe_error(502, "Orichum upstream response failed")
        except RouteProxyError as failure:
            trace.fail("upstream", None, 0, "request")
            self._safe_error(
                502,
                f"Orichum route state is unavailable: {failure}",
            )
        except LogicalSessionError:
            trace.fail("upstream", None, 0, "request")
            self._safe_error(502, "Orichum route state is unavailable")
        except (http.client.HTTPException, TimeoutError, OSError):
            trace.fail("upstream", None, 0, "request")
            self._safe_error(502, "Orichum upstream connection failed")

    do_DELETE = _handle

    def do_GET(self) -> None:
        if self.path == "/health":
            self._health()
        elif urlsplit(self.path).path == "/status":
            self._status()
        else:
            self._handle()

    do_PATCH = _handle
    do_POST = _handle
    do_PUT = _handle


class RouteProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        config: ProxyConfig,
        *,
        route_index: RouteIndex | None = None,
        cooldowns: Cooldowns | None = None,
        route_status_store: RouteStatusStore | None = None,
        event_logger: Callable[[dict[str, object]], None] | None = None,
    ):
        if address[0] != "127.0.0.1":
            raise RouteProxyError("route proxy must bind to 127.0.0.1")
        self.proxy_config = config
        self.route_index = route_index or RouteIndex(config.state_home)
        self.cooldowns = cooldowns or Cooldowns(config.cooldown_seconds)
        self.route_status_store = route_status_store or RouteStatusStore()
        self.event_logger = event_logger or (lambda _document: None)
        self.attestation_gate = AttestationGate(
            ATTESTATION_TTL_SECONDS
        )
        self.catalog_attestation_gate = AttestationGate(
            ATTESTATION_TTL_SECONDS
        )
        self.request_slots = threading.BoundedSemaphore(
            MAX_CONCURRENT_REQUESTS
        )
        super().__init__(address, RouteProxyHandler)

    def process_request(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        if not self.request_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
            finally:
                request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.request_slots.release()
            raise

    def process_request_thread(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


def main() -> int:
    parser = argparse.ArgumentParser(prog="orichum-route-proxy")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--upstream-port", required=True, type=int)
    parser.add_argument("--catalog-port", type=int)
    parser.add_argument("--state-home", required=True, type=Path)
    parser.add_argument("--data-home", required=True, type=Path)
    arguments = parser.parse_args()
    for port in (
        arguments.port,
        arguments.upstream_port,
        arguments.catalog_port or arguments.upstream_port,
    ):
        if port < 1024 or port > 65535:
            raise SystemExit("ports must be between 1024 and 65535")
    server = RouteProxyServer(
        ("127.0.0.1", arguments.port),
        ProxyConfig(
            arguments.upstream_port,
            arguments.state_home,
            arguments.data_home,
            catalog_port=arguments.catalog_port,
        ),
        event_logger=_write_route_event,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
