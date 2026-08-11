#!/usr/bin/env python3
from __future__ import annotations

import http.client
import io
import json
import os
import socket
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from integrations.common.model_routing import ROLES
from integrations.common.orichum_sessions import RouteBinding
from integrations.common.route_proxy import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_PRELUDE_BYTES,
    AttestationGate,
    Cooldowns,
    ProxyConfig,
    RequestTooLarge,
    RouteIndex,
    RouteProxyError,
    RouteProxyServer,
    _has_sse_data_event,
    _read_request_body,
)
from integrations.common.route_selection import Route
from integrations.common.route_status import RouteStatusStore


def client_tool(name: str) -> dict:
    return {
        "name": name,
        "description": name,
        "input_schema": {"type": "object", "properties": {}},
    }


class StaticRouteIndex:
    def __init__(self, routes: dict[str, str], leanctx_profile: str = "full"):
        self.routes = routes
        self.leanctx_profile = leanctx_profile

    @staticmethod
    def _route(model: str, suffix: str) -> Route:
        return Route(
            account_id=f"oc-a-{suffix}",
            provider="openai",
            family="gpt",
            logical_model=model.rsplit("/", 1)[-1],
            upstream_model=model,
            claudex_profile=f"ocp-{suffix}",
            priority=100,
            pool="shared",
        )

    def routes_for(
        self, _session_id: str | None, primary_model: str
    ) -> tuple[Route, Route | None] | None:
        fallback = self.routes.get(primary_model)
        return (
            self._route(primary_model, "0000000000000001"),
            (
                self._route(fallback, "0000000000000002")
                if fallback is not None
                else None
            ),
        )

    def request_policy_for(
        self, session_id: str | None, primary_model: str
    ) -> tuple[tuple[Route, Route | None] | None, str]:
        return self.routes_for(session_id, primary_model), self.leanctx_profile

    def fallback_for(
        self, _session_id: str | None, primary_model: str
    ) -> str | None:
        return self.routes.get(primary_model)


class RecordingUpstream:
    def __init__(self, responses: list[tuple[int, bytes]]):
        self.responses = list(responses)
        self.documents: list[dict[str, object]] = []
        self.models: list[str | None] = []
        self.paths: list[str] = []
        self.session_headers: list[str | None] = []
        self.request_ids: list[str | None] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                owner.paths.append(self.path)
                length = int(self.headers["Content-Length"])
                document = json.loads(self.rfile.read(length))
                owner.documents.append(document)
                owner.models.append(document.get("model"))
                owner.session_headers.append(
                    self.headers.get("X-Orichum-Session-ID")
                )
                owner.request_ids.append(
                    self.headers.get("X-Orichum-Request-ID")
                )
                status, body = owner.responses.pop(0)
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                owner.paths.append(self.path)
                owner.session_headers.append(
                    self.headers.get("X-Orichum-Session-ID")
                )
                owner.request_ids.append(
                    self.headers.get("X-Orichum-Request-ID")
                )
                status, body = owner.responses.pop(0)
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def __enter__(self) -> RecordingUpstream:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class TruncatedUpstream:
    def __init__(
        self,
        *,
        fallback_body: bytes | None = None,
        event_before_truncation: bool = False,
        event_separator: bytes = b"\n",
        empty_large_response: bool = False,
    ):
        self.fallback_body = fallback_body
        self.event_before_truncation = event_before_truncation
        self.event_separator = event_separator
        self.empty_large_response = empty_large_response
        self.models: list[str | None] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                document = json.loads(self.rfile.read(length))
                owner.models.append(document.get("model"))
                if len(owner.models) > 1:
                    if owner.fallback_body is None:
                        raise AssertionError("unexpected fallback request")
                    self.send_response(200)
                    self.send_header(
                        "Content-Length", str(len(owner.fallback_body))
                    )
                    self.end_headers()
                    self.wfile.write(owner.fallback_body)
                    return

                if owner.empty_large_response:
                    partial = b""
                elif owner.event_before_truncation:
                    partial = (
                        b'data: {"type":"content_block_delta"}'
                        + owner.event_separator
                        + owner.event_separator
                    )
                else:
                    partial = b'data: {"type":"content_block_delta"'
                self.send_response(200)
                if not owner.empty_large_response:
                    self.send_header("Content-Type", "text/event-stream")
                announced_length = (
                    MAX_RESPONSE_PRELUDE_BYTES + 1
                    if owner.empty_large_response
                    else len(partial) + 100
                )
                self.send_header("Content-Length", str(announced_length))
                self.end_headers()
                self.wfile.write(partial)
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def __enter__(self) -> TruncatedUpstream:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ProxyHarness:
    def __init__(
        self,
        upstream_port: int,
        routes: dict[str, str],
        *,
        cooldowns: Cooldowns | None = None,
        data_home: Path | None = None,
        catalog_port: int | None = None,
        leanctx_profile: str = "full",
    ):
        self.events: list[dict[str, object]] = []
        self.last_response_headers: dict[str, str] = {}
        config_arguments: dict[str, object] = {}
        if catalog_port is not None:
            config_arguments["catalog_port"] = catalog_port
        self.server = RouteProxyServer(
            ("127.0.0.1", 0),
            ProxyConfig(
                upstream_port,
                Path("/unused"),
                data_home,
                **config_arguments,
            ),
            route_index=StaticRouteIndex(routes, leanctx_profile),
            cooldowns=cooldowns,
            event_logger=self.events.append,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def __enter__(self) -> ProxyHarness:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(
        self, model: str, path: str = "/v1/messages"
    ) -> tuple[int, bytes]:
        return self.post_document(
            {"model": model, "messages": []},
            path,
        )

    def post_document(
        self,
        document: dict[str, object],
        path: str = "/v1/messages",
    ) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=3
        )
        body = json.dumps(document).encode()
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Orichum-Session-ID": "oc-s-0000000000000001",
            },
        )
        response = connection.getresponse()
        self.last_response_headers = dict(response.getheaders())
        payload = response.read()
        connection.close()
        return response.status, payload

    def get(self, path: str) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=3
        )
        connection.request("GET", path)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, payload


class RouteProxyTests(unittest.TestCase):
    primary = "oc-r-0000000000000001/gpt-5.6-sol"
    fallback = "oc-r-0000000000000002/gpt-5.6-sol"

    def test_health_is_not_ready_when_upstream_is_unavailable(self) -> None:
        unavailable = socket.socket()
        unavailable.bind(("127.0.0.1", 0))
        unavailable_port = unavailable.getsockname()[1]
        unavailable.close()
        with ProxyHarness(unavailable_port, {}) as proxy:
            status, body = proxy.get("/health")
        self.assertEqual(status, 503)
        self.assertEqual(
            json.loads(body),
            {
                "pid": os.getpid(),
                "ready": False,
                "service": "orichum-route-proxy",
            },
        )

    def test_health_is_ready_when_upstream_is_connectable(self) -> None:
        with RecordingUpstream([]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ready"])

    def test_health_checks_reachability_without_slow_ownership_attestation(
        self,
    ) -> None:
        with (
            RecordingUpstream([]) as upstream,
            mock.patch(
                "integrations.common.route_proxy.subprocess.run",
                side_effect=AssertionError("health must not run a verifier"),
            ),
        ):
            with ProxyHarness(
                upstream.port,
                {},
                data_home=Path("/managed"),
            ) as proxy:
                status, body = proxy.get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ready"])

    def test_health_requires_optimizer_and_catalogue(self) -> None:
        unavailable = socket.socket()
        unavailable.bind(("127.0.0.1", 0))
        unavailable_port = unavailable.getsockname()[1]
        unavailable.close()
        with RecordingUpstream([]) as optimizer:
            with ProxyHarness(
                optimizer.port,
                {},
                catalog_port=unavailable_port,
            ) as proxy:
                status, body = proxy.get("/health")
        self.assertEqual(status, 503)
        self.assertFalse(json.loads(body)["ready"])

    def test_model_catalogue_bypasses_optimizer(self) -> None:
        with (
            RecordingUpstream([]) as optimizer,
            RecordingUpstream([(200, b'{"data":[{"id":"model"}]}')])
            as catalogue,
        ):
            with ProxyHarness(
                optimizer.port,
                {},
                catalog_port=catalogue.port,
            ) as proxy:
                status, body = proxy.get("/v1/models")

        self.assertEqual((status, body), (200, b'{"data":[{"id":"model"}]}'))
        self.assertEqual(optimizer.paths, [])
        self.assertEqual(catalogue.paths, ["/v1/models"])

    def test_concurrent_attestations_share_one_successful_refresh(self) -> None:
        gate = AttestationGate(30)
        barrier = threading.Barrier(24)
        full_calls = 0
        calls_lock = threading.Lock()
        attested_ports: list[int] = []
        failures: list[BaseException] = []

        def full_verifier(client_port: int) -> int:
            nonlocal full_calls
            with calls_lock:
                full_calls += 1
                attested_ports.append(client_port)
            time.sleep(0.05)
            return 48123

        def attest(client_port: int) -> None:
            try:
                barrier.wait()
                gate.verify(client_port, full_verifier)
            except BaseException as failure:
                failures.append(failure)

        threads = [
            threading.Thread(target=attest, args=(port,))
            for port in range(48000, 48024)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [])
        self.assertEqual(full_calls, 1)
        self.assertEqual(len(attested_ports), 1)

    def test_successful_attestation_expires(self) -> None:
        clock = [100.0]
        gate = AttestationGate(30, clock=lambda: clock[0])
        full_ports: list[int] = []

        def full_verifier(client_port: int) -> int:
            full_ports.append(client_port)
            return 48123

        gate.verify(48000, full_verifier)
        clock[0] = 129.9
        gate.verify(48001, full_verifier)
        clock[0] = 130.0
        gate.verify(48002, full_verifier)

        self.assertEqual(full_ports, [48000, 48002])

    def test_failed_attestation_is_not_cached(self) -> None:
        gate = AttestationGate(30)
        calls = 0

        def full_verifier(_client_port: int) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RouteProxyError("rejected")
            return 48123

        with self.assertRaises(RouteProxyError):
            gate.verify(48000, full_verifier)
        gate.verify(48001, full_verifier)

        self.assertEqual(calls, 2)

    def test_concurrent_attestation_failure_is_shared_by_waiters(self) -> None:
        gate = AttestationGate(30)
        barrier = threading.Barrier(24)
        full_calls = 0
        calls_lock = threading.Lock()
        failures: list[BaseException] = []

        def failing_full_verifier(_client_port: int) -> int:
            nonlocal full_calls
            with calls_lock:
                full_calls += 1
            time.sleep(0.05)
            raise RouteProxyError("rejected")

        def attest(client_port: int) -> None:
            try:
                barrier.wait()
                gate.verify(client_port, failing_full_verifier)
            except BaseException as failure:
                failures.append(failure)

        threads = [
            threading.Thread(target=attest, args=(port,))
            for port in range(48000, 48024)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(full_calls, 1)
        self.assertEqual(len(failures), 24)

        gate.verify(49000, lambda _port: 48123)

    def test_invalidation_refreshes_service_identity(self) -> None:
        gate = AttestationGate(30)
        identities = iter((48123, 48124))
        full_ports: list[int] = []

        def full_verifier(client_port: int) -> int:
            full_ports.append(client_port)
            return next(identities)

        gate.verify(48000, full_verifier)
        gate.verify(48001, full_verifier)
        gate.invalidate()
        gate.verify(48002, full_verifier)

        self.assertEqual(full_ports, [48000, 48002])

    def test_cached_production_sockets_do_not_rerun_verifier(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="40541\n")
        with RecordingUpstream(
            [(200, b"first"), (200, b"second")]
        ) as upstream:
            with mock.patch(
                "integrations.common.route_proxy.subprocess.run",
                return_value=completed,
            ) as verifier:
                with ProxyHarness(
                    upstream.port, {}, data_home=Path("/data")
                ) as proxy:
                    self.assertEqual(
                        proxy.post(self.primary), (200, b"first")
                    )
                    self.assertEqual(
                        proxy.post(self.primary), (200, b"second")
                    )

        self.assertEqual(verifier.call_count, 1)
        first_arguments = verifier.call_args.args[0]
        self.assertEqual(
            Path(first_arguments[0]).name,
            "orichum-verify-leanctx-proxy",
        )
        self.assertEqual(first_arguments[1:3], ["/data", str(upstream.port)])

    def test_concurrent_routing_shares_one_attestation_refresh(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="40541\n")
        with RecordingUpstream([(200, b"ok")] * 16) as upstream:
            with mock.patch(
                "integrations.common.route_proxy.subprocess.run",
                return_value=completed,
            ) as verifier:
                with ProxyHarness(
                    upstream.port, {}, data_home=Path("/data")
                ) as proxy:
                    with ThreadPoolExecutor(max_workers=16) as executor:
                        responses = tuple(
                            executor.map(
                                lambda _: proxy.post(self.primary),
                                range(16),
                            )
                        )

        self.assertEqual(responses, ((200, b"ok"),) * 16)
        self.assertEqual(verifier.call_count, 1)

    def test_verifier_timeout_reports_duration(self) -> None:
        with RecordingUpstream([]) as upstream:
            with mock.patch(
                "integrations.common.route_proxy.subprocess.run",
                side_effect=subprocess.TimeoutExpired("verify", 3),
            ):
                with ProxyHarness(
                    upstream.port, {}, data_home=Path("/data")
                ) as proxy:
                    status, body = proxy.post(self.primary)

        self.assertEqual(status, 502)
        self.assertIn(b"ownership verifier timed out after", body)

    def test_invalid_service_identity_is_rejected(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="not-a-pid\n")
        with RecordingUpstream([]) as upstream:
            with mock.patch(
                "integrations.common.route_proxy.subprocess.run",
                return_value=completed,
            ):
                with ProxyHarness(
                    upstream.port, {}, data_home=Path("/data")
                ) as proxy:
                    status, _body = proxy.post(self.primary)

        self.assertEqual(status, 502)

    def test_normalizes_claudex_profile_path_for_cliproxy(self) -> None:
        with RecordingUpstream([(200, b"ok")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.post(
                    self.primary,
                    "/proxy/gpt/v1/messages?beta=true",
                )
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(upstream.paths, ["/v1/messages?beta=true"])

    def test_normalizes_claudex_profile_root_with_query(self) -> None:
        with RecordingUpstream([(200, b"ok")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.post(
                    self.primary,
                    "/proxy/gpt?probe=1",
                )
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(upstream.paths, ["/?probe=1"])

    def test_rejects_unowned_claudex_profile_path(self) -> None:
        with RecordingUpstream([(200, b"ok")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, _ = proxy.post(
                    self.primary,
                    "/proxy/unowned/v1/messages",
                )
        self.assertEqual(status, 502)
        self.assertEqual(upstream.paths, [])

    def test_rejects_oversized_content_length_before_reading(self) -> None:
        handler = SimpleNamespace(
            headers={"Content-Length": str(MAX_REQUEST_BYTES + 1)},
            rfile=io.BytesIO(b""),
        )
        with self.assertRaises(RequestTooLarge):
            _read_request_body(handler)

    def test_rejects_chunked_body_when_aggregate_exceeds_limit(self) -> None:
        handler = SimpleNamespace(
            headers={"Transfer-Encoding": "chunked"},
            rfile=io.BytesIO(
                b"4\r\n"
                + b"xxxx"
                + b"\r\n1\r\ny\r\n0\r\n\r\n"
            ),
        )
        with mock.patch(
            "integrations.common.route_proxy.MAX_REQUEST_BYTES", 4
        ):
            with self.assertRaises(RequestTooLarge):
                _read_request_body(handler)

    def binding(self, primary: str, fallback: str | None) -> RouteBinding:
        def route(upstream: str, suffix: str) -> Route:
            return Route(
                account_id=f"oc-a-{suffix}",
                provider="openai",
                family="gpt",
                logical_model="gpt-5.6-sol",
                upstream_model=upstream,
                claudex_profile=f"ocp-{suffix}",
                priority=100,
                pool="shared",
            )

        return RouteBinding(
            route(primary, "0000000000000001"),
            (
                (route(fallback, "0000000000000002"),)
                if fallback is not None
                else ()
            ),
        )

    def test_route_index_uses_only_the_calling_logical_session(self) -> None:
        bound = self.binding(self.primary, self.fallback)
        session = SimpleNamespace(
            controller=bound,
            agents={role: bound for role in ROLES},
            leanctx_profile="full",
        )
        with mock.patch(
            "integrations.common.orichum_sessions.load_logical_session",
            return_value=session,
        ) as load:
            selected = RouteIndex(Path("/state")).fallback_for(
                "oc-s-0000000000000001", self.primary
            )

        self.assertEqual(selected, self.fallback)
        load.assert_called_once_with(
            Path("/state"), "oc-s-0000000000000001"
        )
        self.assertIsNone(
            RouteIndex(Path("/state")).fallback_for(None, self.primary)
        )

    def test_route_index_returns_the_logical_session_leanctx_profile(self) -> None:
        bound = self.binding(self.primary, self.fallback)
        session = SimpleNamespace(
            controller=bound,
            agents={role: bound for role in ROLES},
            leanctx_profile="lean",
        )
        with mock.patch(
            "integrations.common.orichum_sessions.load_logical_session",
            return_value=session,
        ) as load:
            routes, profile = RouteIndex(Path("/state")).request_policy_for(
                "oc-s-0000000000000001", self.primary
            )

        self.assertEqual(routes, (bound.primary, bound.fallbacks[0]))
        self.assertEqual(profile, "lean")
        load.assert_called_once_with(
            Path("/state"), "oc-s-0000000000000001"
        )

    def test_successful_primary_is_not_retried_or_rewritten(self) -> None:
        with RecordingUpstream([(200, b"primary")]) as upstream:
            with ProxyHarness(
                upstream.port, {self.primary: self.fallback}
            ) as proxy:
                status, body = proxy.post(self.primary)

        self.assertEqual((status, body), (200, b"primary"))
        self.assertEqual(upstream.models, [self.primary])
        self.assertRegex(
            upstream.request_ids[0] or "",
            r"^oc-rq-[a-f0-9]{16}$",
        )
        self.assertEqual(
            proxy.last_response_headers["X-Orichum-Request-ID"],
            upstream.request_ids[0],
        )
        self.assertEqual(
            [event["event"] for event in proxy.events],
            ["route-streaming", "route-complete"],
        )
        self.assertEqual(upstream.session_headers, [None])

    def test_verified_request_defers_tools_before_forwarding(self) -> None:
        tools = [
            client_tool("mcp__leanctx__ctx_shell"),
            *[
                client_tool(f"mcp__sample__tool_{index}")
                for index in range(11)
            ],
        ]
        with RecordingUpstream([(200, b"ok")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, _ = proxy.post_document(
                    {"model": self.primary, "messages": [], "tools": tools}
                )
        self.assertEqual(status, 200)
        forwarded = upstream.documents[0]
        self.assertEqual(
            forwarded["tools"][-1]["type"],
            "tool_search_tool_regex_20251119",
        )

    def test_lean_profile_defers_non_core_leanctx_tools(self) -> None:
        leanctx_names = (
            "ctx_read",
            "ctx_search",
            "ctx_tree",
            "ctx_expand",
            "ctx_graph",
            "ctx_impact",
            "ctx_callgraph",
            "ctx_knowledge",
            "ctx_overview",
            "ctx_patch",
            "ctx_shell",
        )
        tools = [
            *(client_tool(f"mcp__leanctx__{name}") for name in leanctx_names),
            client_tool("Bash"),
        ]
        with RecordingUpstream([(200, b"ok")]) as upstream:
            with ProxyHarness(
                upstream.port,
                {},
                leanctx_profile="lean",
            ) as proxy:
                status, _ = proxy.post_document(
                    {"model": self.primary, "messages": [], "tools": tools}
                )

        self.assertEqual(status, 200)
        by_name = {
            tool.get("name"): tool
            for tool in upstream.documents[0]["tools"]
            if isinstance(tool, dict) and "name" in tool
        }
        resident = {
            "mcp__leanctx__ctx_read",
            "mcp__leanctx__ctx_search",
            "mcp__leanctx__ctx_tree",
            "mcp__leanctx__ctx_shell",
        }
        for name in resident:
            self.assertNotIn("defer_loading", by_name[name])
        for name in {
            f"mcp__leanctx__{value}" for value in leanctx_names
        } - resident:
            self.assertIs(by_name[name].get("defer_loading"), True)

    def test_unknown_model_request_is_forwarded_unchanged(self) -> None:
        document = {
            "model": "oc-r-0000000000000001/future-model",
            "messages": [{"role": "user", "content": "test"}],
            "tools": [
                client_tool("mcp__leanctx__ctx_shell"),
                *[client_tool(f"tool_{index}") for index in range(11)],
            ],
        }
        with RecordingUpstream([(200, b"ok")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, _ = proxy.post_document(document)
        self.assertEqual(status, 200)
        self.assertEqual(upstream.documents, [document])

    def test_400_from_transformed_request_retries_original_once(self) -> None:
        document = {
            "model": self.primary,
            "messages": [],
            "tools": [
                client_tool("mcp__leanctx__ctx_shell"),
                *[client_tool(f"tool_{index}") for index in range(11)],
            ],
        }
        with RecordingUpstream(
            [(400, b"unsupported"), (200, b"ok")]
        ) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.post_document(document)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(len(upstream.documents), 2)
        self.assertIn("defer_loading", upstream.documents[0]["tools"][1])
        self.assertEqual(upstream.documents[1], document)

    def test_422_from_transformed_request_retries_original_once(self) -> None:
        document = {
            "model": self.primary,
            "messages": [],
            "tools": [
                client_tool("mcp__leanctx__ctx_shell"),
                *[client_tool(f"tool_{index}") for index in range(11)],
            ],
        }
        with RecordingUpstream(
            [(422, b"unsupported"), (200, b"ok")]
        ) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.post_document(document)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(upstream.documents[1], document)

    def test_untransformed_400_is_returned_without_retry(self) -> None:
        document = {
            "model": self.primary,
            "messages": [],
            "tools": [client_tool("Bash")],
        }
        with RecordingUpstream([(400, b"invalid")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.post_document(document)
        self.assertEqual((status, body), (400, b"invalid"))
        self.assertEqual(upstream.documents, [document])

    def test_429_does_not_use_tool_compatibility_retry(self) -> None:
        document = {
            "model": self.primary,
            "messages": [],
            "tools": [
                client_tool("mcp__leanctx__ctx_shell"),
                *[client_tool(f"tool_{index}") for index in range(11)],
            ],
        }
        with RecordingUpstream([(429, b"quota")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, _ = proxy.post_document(document)
        self.assertEqual(status, 429)
        self.assertEqual(len(upstream.documents), 1)

    def test_cooldown_selected_fallback_uses_its_logical_model(self) -> None:
        primary = "oc-r-0000000000000001/future-model"
        fallback = "oc-r-0000000000000002/gpt-5.6-sol"
        document = {
            "model": primary,
            "messages": [],
            "tools": [
                client_tool("mcp__leanctx__ctx_shell"),
                *[client_tool(f"tool_{index}") for index in range(11)],
            ],
        }
        cooldowns = Cooldowns(60)
        cooldowns.trip(primary)
        with RecordingUpstream([(200, b"ok")]) as upstream:
            with ProxyHarness(
                upstream.port,
                {primary: fallback},
                cooldowns=cooldowns,
            ) as proxy:
                status, _ = proxy.post_document(document)
        self.assertEqual(status, 200)
        self.assertEqual(upstream.models, [fallback])
        self.assertEqual(
            upstream.documents[0]["tools"][-1]["type"],
            "tool_search_tool_regex_20251119",
        )

    def test_account_failover_changes_only_model(self) -> None:
        document = {
            "model": self.primary,
            "messages": [{"role": "user", "content": "test"}],
            "tools": [
                client_tool("mcp__leanctx__ctx_shell"),
                *[client_tool(f"tool_{index}") for index in range(11)],
            ],
        }
        with RecordingUpstream(
            [(429, b"limited"), (200, b"fallback")]
        ) as upstream:
            with ProxyHarness(
                upstream.port, {self.primary: self.fallback}
            ) as proxy:
                status, _ = proxy.post_document(document)
        self.assertEqual(status, 200)
        primary_document, fallback_document = upstream.documents
        self.assertEqual(primary_document["model"], self.primary)
        self.assertEqual(fallback_document["model"], self.fallback)
        self.assertEqual(
            {**primary_document, "model": self.fallback},
            fallback_document,
        )

    def test_400_compatibility_retry_does_not_trip_cooldown(self) -> None:
        document = {
            "model": self.primary,
            "messages": [],
            "tools": [
                client_tool("mcp__leanctx__ctx_shell"),
                *[client_tool(f"tool_{index}") for index in range(11)],
            ],
        }
        with RecordingUpstream(
            [
                (400, b"unsupported"),
                (200, b"original"),
                (200, b"next"),
            ]
        ) as upstream:
            with ProxyHarness(
                upstream.port, {self.primary: self.fallback}
            ) as proxy:
                first = proxy.post_document(document)
                second = proxy.post_document(document)
        self.assertEqual(first, (200, b"original"))
        self.assertEqual(second, (200, b"next"))
        self.assertEqual(
            upstream.models,
            [self.primary, self.primary, self.primary],
        )

    def test_retryable_status_uses_one_fallback_and_preserves_result(self) -> None:
        with RecordingUpstream(
            [(429, b"limited"), (200, b"fallback")]
        ) as upstream:
            with ProxyHarness(
                upstream.port, {self.primary: self.fallback}
            ) as proxy:
                status, body = proxy.post(self.primary)

        self.assertEqual((status, body), (200, b"fallback"))
        self.assertEqual(upstream.models, [self.primary, self.fallback])

    def test_status_endpoint_reports_the_active_fallback_account(self) -> None:
        with RecordingUpstream(
            [(429, b"limited"), (200, b"fallback")]
        ) as upstream:
            with ProxyHarness(
                upstream.port, {self.primary: self.fallback}
            ) as proxy:
                self.assertEqual(
                    proxy.post(self.primary),
                    (200, b"fallback"),
                )
                status, body = proxy.get(
                    "/status?session_id=oc-s-0000000000000001"
                )

        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["accountId"], "oc-a-0000000000000002")
        self.assertEqual(document["routeState"], "fallback")
        self.assertEqual(document["reason"], "retry")
        self.assertEqual(document["lastHttpStatus"], 200)
        self.assertRegex(document["requestId"], r"^oc-rq-[a-f0-9]{16}$")
        self.assertEqual(document["responseState"], "complete")
        self.assertEqual(document["bytesForwarded"], len(b"fallback"))
        self.assertIsNone(document["failureKind"])

    def test_stale_completion_does_not_overwrite_newer_request(self) -> None:
        store = RouteStatusStore()
        session_id = "oc-s-0000000000000001"
        first_request = "oc-rq-0000000000000001"
        second_request = "oc-rq-0000000000000002"
        route = StaticRouteIndex._route(
            self.primary, "0000000000000001"
        )
        store.select(
            session_id,
            first_request,
            route,
            route_state="primary",
            reason="primary",
        )
        store.select(
            session_id,
            second_request,
            route,
            route_state="primary",
            reason="primary",
        )
        store.complete(session_id, first_request, 200, 10)

        status = store.get(session_id)
        self.assertIsNotNone(status)
        self.assertEqual(status.request_id, second_request)
        self.assertEqual(status.response_state, "pending")

    def test_cooldown_skips_known_exhausted_primary(self) -> None:
        clock = [100.0]
        cooldowns = Cooldowns(60, clock=lambda: clock[0])
        with RecordingUpstream(
            [(429, b"limited"), (200, b"first"), (200, b"second")]
        ) as upstream:
            with ProxyHarness(
                upstream.port,
                {self.primary: self.fallback},
                cooldowns=cooldowns,
            ) as proxy:
                self.assertEqual(proxy.post(self.primary), (200, b"first"))
                self.assertEqual(proxy.post(self.primary), (200, b"second"))

        self.assertEqual(
            upstream.models,
            [self.primary, self.fallback, self.fallback],
        )

    def test_without_fallback_returns_the_original_failure(self) -> None:
        with RecordingUpstream([(429, b"limited")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.post(self.primary)

        self.assertEqual((status, body), (429, b"limited"))
        self.assertEqual(upstream.models, [self.primary])

    def test_connection_failure_does_not_attempt_a_fallback(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        unavailable_port = listener.getsockname()[1]
        listener.close()
        with ProxyHarness(
            unavailable_port, {self.primary: self.fallback}
        ) as proxy:
            status, _body = proxy.post(self.primary)

        self.assertEqual(status, 502)

    def test_sse_data_event_accepts_all_legal_line_endings(self) -> None:
        for separator in (b"\n", b"\r\n", b"\r"):
            for field in (b"data: value", b"data"):
                with self.subTest(separator=separator, field=field):
                    self.assertTrue(
                        _has_sse_data_event(
                            field + separator + separator
                        )
                    )

    def test_pre_output_truncated_stream_retries_fallback(self) -> None:
        fallback_body = b'fallback complete'
        with TruncatedUpstream(fallback_body=fallback_body) as upstream:
            with ProxyHarness(
                upstream.port, {self.primary: self.fallback}
            ) as proxy:
                status, body = proxy.post(self.primary)
                telemetry_status, telemetry_body = proxy.get(
                    "/status?session_id=oc-s-0000000000000001"
                )

        self.assertEqual((status, body), (200, fallback_body))
        self.assertEqual(upstream.models, [self.primary, self.fallback])
        self.assertEqual(telemetry_status, 200)
        telemetry = json.loads(telemetry_body)
        self.assertEqual(telemetry["routeState"], "fallback")
        self.assertEqual(telemetry["reason"], "retry")
        self.assertEqual(telemetry["responseState"], "complete")
        retry = next(
            event for event in proxy.events if event["event"] == "route-retry"
        )
        self.assertEqual(retry["cause"], "pre-output-stream")

    def test_empty_large_response_retries_before_output(self) -> None:
        fallback_body = b"fallback complete"
        with TruncatedUpstream(
            fallback_body=fallback_body,
            empty_large_response=True,
        ) as upstream:
            with ProxyHarness(
                upstream.port, {self.primary: self.fallback}
            ) as proxy:
                status, body = proxy.post(self.primary)

        self.assertEqual((status, body), (200, fallback_body))
        self.assertEqual(upstream.models, [self.primary, self.fallback])

    def test_pre_output_truncated_stream_without_fallback_returns_502(
        self,
    ) -> None:
        with TruncatedUpstream() as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.post(self.primary)

        self.assertEqual(status, 502)
        self.assertIn(b"Orichum upstream response failed", body)
        self.assertRegex(
            proxy.last_response_headers["X-Orichum-Request-ID"],
            r"^oc-rq-[a-f0-9]{16}$",
        )
        self.assertEqual(upstream.models, [self.primary])

    def test_truncated_stream_after_first_event_is_not_replayed(self) -> None:
        event = b'data: {"type":"content_block_delta"}\r\r'
        with TruncatedUpstream(
            fallback_body=b"must not be used",
            event_before_truncation=True,
            event_separator=b"\r",
        ) as upstream:
            with ProxyHarness(
                upstream.port, {self.primary: self.fallback}
            ) as proxy:
                status, body = proxy.post(self.primary)
                telemetry_status, telemetry_body = proxy.get(
                    "/status?session_id=oc-s-0000000000000001"
                )

        self.assertEqual((status, body), (200, event))
        self.assertEqual(upstream.models, [self.primary])
        self.assertEqual(telemetry_status, 200)
        telemetry = json.loads(telemetry_body)
        self.assertEqual(telemetry["routeState"], "primary")
        self.assertEqual(telemetry["responseState"], "failed")
        self.assertEqual(telemetry["failureKind"], "upstream")
        self.assertEqual(telemetry["bytesForwarded"], len(event))
        failure = next(
            event for event in proxy.events if event["event"] == "route-failed"
        )
        self.assertEqual(failure["stage"], "after-output")

    def test_non_loopback_listener_is_rejected(self) -> None:
        with self.assertRaisesRegex(RouteProxyError, "127.0.0.1"):
            RouteProxyServer(
                ("0.0.0.0", 0),
                ProxyConfig(8317, Path("/unused")),
                route_index=StaticRouteIndex({}),
            )


if __name__ == "__main__":
    unittest.main()
