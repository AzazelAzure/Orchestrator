"""WSGI HTTP service for the sole-writer state coordinator."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from flow_engine.control_plane.service_auth import (
    ServiceCallerKind,
    authenticate_service_credential,
    require_configured_service_credentials,
)
from flow_engine.coordinator.commands import (
    CommandContext,
    ResolvedTaskGrant,
    RuntimeCommand,
)
from flow_engine.coordinator.coordinator import StateCoordinator
from flow_engine.coordinator.mcp_enforce import (
    extract_mcp_context_claims,
    mcp_identity_present,
    strip_mcp_payload_audit_fields,
)
from flow_engine.coordinator.transport import command_from_dict
from flow_engine.domain.errors import AuthRequiredError, AuthzDeniedError, FlowError
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.persistence.connection import Kernel
from flow_engine.persistence.transactions import transaction

_coordinator: StateCoordinator | None = None
_kernel: Kernel | None = None


def get_coordinator() -> StateCoordinator:
    global _coordinator, _kernel
    if _coordinator is None:
        db_path = Path(os.environ.get("FLOW_DB_PATH", "/data/state.db"))
        _kernel = Kernel.init(db_path)
        # Default bootstrap OFF — inject secrets explicitly.
        if os.environ.get("ORCH_BOOTSTRAP_PRINCIPALS", "0") == "1":
            from flow_engine.control_plane.bootstrap import bootstrap_principals_from_env

            with transaction(_kernel.connection):
                bootstrap_principals_from_env(_kernel.connection)
        _coordinator = StateCoordinator(_kernel.connection)
    return _coordinator


def reset_coordinator() -> None:
    """Test helper to reset singleton."""
    global _coordinator, _kernel
    if _kernel is not None:
        _kernel.close()
    _coordinator = None
    _kernel = None


def _json_response(start_response: Callable, status: str, body: dict[str, Any]) -> list[bytes]:
    payload = json.dumps(body, default=str).encode("utf-8")
    start_response(
        status, [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))]
    )
    return [payload]


def _read_body(environ: dict[str, Any], max_bytes: int = 1_048_576) -> bytes:
    try:
        size = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError:
        size = 0
    if size > max_bytes:
        raise ValueError("request body too large")
    return environ["wsgi.input"].read(size)


def _service_headers(environ: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    return (
        environ.get("HTTP_X_ORCHESTRATOR_SERVICE_TOKEN"),
        environ.get("HTTP_X_ORCHESTRATOR_SERVICE_KIND"),
        environ.get("HTTP_X_ORCHESTRATOR_PRINCIPAL_TOKEN"),
    )


def _resolve_server_context(
    coord: StateCoordinator,
    *,
    caller: Any,
    principal_token: str | None,
    surface_hint: str | None,
    mcp_claims: dict[str, str | None] | None = None,
    command_type: str | None = None,
) -> CommandContext:
    """Resolve principal/role/grant server-side; reject caller-supplied authority.

    MCP service principal / lane / tool-snapshot identity may be preserved from
    authenticated API transport context, then independently verified in accept().
    """
    from flow_engine.control_plane import principal_registry as principals

    claims = mcp_claims or {}
    if caller.kind == ServiceCallerKind.WORKER:
        worker = principals.resolve_by_key(coord.connection, "worker")
        grant = principals.load_grant_for_principal(coord.connection, worker)
        return CommandContext(
            principal_id=worker.principal_id,
            role=worker.role,
            surface=Surface.WORKER,
            grant=grant,
        )

    if not principal_token:
        raise AuthRequiredError("principal token required for API service callers")
    principal = principals.resolve_by_token(coord.connection, principal_token)
    if principal.status == "revoked":
        raise AuthzDeniedError("principal revoked")
    grant = principals.load_grant_for_principal(coord.connection, principal)
    hierarchy_without_grant = bool(
        command_type and (command_type.startswith("delegation.") or command_type.startswith("org."))
    )
    if hierarchy_without_grant and not isinstance(grant, ResolvedTaskGrant):
        grant = None
    surface = Surface(surface_hint) if surface_hint else Surface.REST
    if mcp_identity_present(claims):
        surface = Surface.MCP
    principals.assert_surface_allowed(principal, surface)
    return CommandContext(
        principal_id=principal.principal_id,
        role=principal.role,
        surface=surface,
        grant=grant,
        mcp_service_principal_id=claims.get("mcp_service_principal_id"),
        mcp_lane_id=claims.get("mcp_lane_id"),
        mcp_tool_snapshot_digest=claims.get("mcp_tool_snapshot_digest"),
        mcp_tool_name=claims.get("mcp_tool_name"),
    )


def application(environ: dict[str, Any], start_response: Callable) -> list[bytes]:
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")

    if method == "GET" and path in {"/health", "/healthz", "/v1/health"}:
        try:
            coord = get_coordinator()
            version = coord.connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
            return _json_response(
                start_response,
                "200 OK",
                {"status": "ok", "service": "state-coordinator", "schema_version": version},
            )
        except Exception as exc:
            return _json_response(
                start_response,
                "503 Service Unavailable",
                {"status": "error", "detail": str(exc)},
            )

    if method == "POST" and path in {"/v1/commands", "/commands"}:
        try:
            service_token, service_kind, principal_token = _service_headers(environ)
            caller = authenticate_service_credential(
                credential=service_token,
                claimed_kind=service_kind,
            )
            raw = _read_body(environ)
            data = json.loads(raw.decode("utf-8") or "{}")
            # Reject caller-supplied authority: strip context before rebuild.
            # Preserve MCP identity claims for independent coordinator verification.
            incoming_ctx = data.get("context") if isinstance(data.get("context"), dict) else {}
            surface_hint = incoming_ctx.get("surface") if isinstance(incoming_ctx, dict) else None
            mcp_claims = extract_mcp_context_claims(
                incoming_ctx if isinstance(incoming_ctx, dict) else None
            )
            payload = strip_mcp_payload_audit_fields(data.get("payload") or {})
            # Strip authority smuggling from payload.
            for banned in (
                "principal_id",
                "role",
                "grant",
                "step_up",
                "capabilities",
                "worker_principal_id",
                "founder_authorized",
                "allow_registration",
            ):
                payload.pop(banned, None)

            data = {
                "command_type": data["command_type"],
                "target_id": data.get("target_id"),
                "payload": payload,
                "idempotency_key": data.get("idempotency_key"),
                "context": {
                    "principal_id": "unresolved",
                    "role": "worker",
                    "surface": surface_hint or "rest",
                },
            }

            command = command_from_dict(data)
            coord = get_coordinator()

            PUBLIC_AUTH_COMMANDS = frozenset(
                {
                    "control_plane.resolve_token",
                    "auth.login",
                    "auth.refresh",
                    "auth.throttle_check",
                    "auth.logout",
                }
            )
            if command.command_type == "auth.register_user":
                if caller.kind != ServiceCallerKind.API:
                    raise AuthzDeniedError("auth/token commands require API service credential")
                if principal_token:
                    resolved_ctx = _resolve_server_context(
                        coord,
                        caller=caller,
                        principal_token=principal_token,
                        surface_hint=surface_hint,
                        mcp_claims=mcp_claims,
                        command_type=command.command_type,
                    )
                    command = RuntimeCommand(
                        command_type=command.command_type,
                        target_id=command.target_id,
                        payload=command.payload,
                        idempotency_key=command.idempotency_key,
                        context=resolved_ctx,
                    )
                else:
                    command = RuntimeCommand(
                        command_type=command.command_type,
                        target_id=command.target_id,
                        payload=command.payload,
                        idempotency_key=command.idempotency_key,
                        context=CommandContext(
                            principal_id="auth-resolver",
                            role=PrincipalRole.SYSTEM,
                            surface=Surface.REST,
                        ),
                    )
                with transaction(coord.connection):
                    envelope = coord.accept(command)
            elif command.command_type in PUBLIC_AUTH_COMMANDS:
                if caller.kind != ServiceCallerKind.API:
                    raise AuthzDeniedError("auth/token commands require API service credential")
                command = RuntimeCommand(
                    command_type=command.command_type,
                    target_id=command.target_id,
                    payload=command.payload,
                    idempotency_key=command.idempotency_key,
                    context=CommandContext(
                        principal_id="auth-resolver",
                        role=PrincipalRole.SYSTEM,
                        surface=Surface.REST,
                    ),
                )
                with transaction(coord.connection):
                    envelope = coord.accept(command)
            else:
                resolved_ctx = _resolve_server_context(
                    coord,
                    caller=caller,
                    principal_token=principal_token,
                    surface_hint=surface_hint,
                    mcp_claims=mcp_claims,
                    command_type=command.command_type,
                )
                command = RuntimeCommand(
                    command_type=command.command_type,
                    target_id=command.target_id,
                    payload=command.payload,
                    idempotency_key=command.idempotency_key,
                    context=resolved_ctx,
                )

                if command.command_type == "runtime.worker_deliver":
                    from flow_engine.application.worker_delivery import accept_worker_deliver

                    if caller.kind != ServiceCallerKind.WORKER:
                        raise AuthzDeniedError("worker_deliver requires worker service credential")
                    envelope = accept_worker_deliver(coord, command)
                elif command.command_type == "script.execute":
                    from flow_engine.application.script_delivery import accept_script_execute

                    if caller.kind != ServiceCallerKind.WORKER:
                        raise AuthzDeniedError("script.execute requires worker service credential")
                    envelope = accept_script_execute(coord, command)
                else:
                    with transaction(coord.connection):
                        envelope = coord.accept(command)

            http_status = "202 Accepted"
            if envelope.get("from_cache"):
                http_status = "200 OK"
            if envelope.get("status") == "rejected":
                code = envelope.get("error_code", "FLOW_ERROR")
                if code == "AUTH_REQUIRED":
                    http_status = "401 Unauthorized"
                elif code in {"AUTHZ_DENIED", "UNSUPPORTED_SURFACE"}:
                    http_status = "403 Forbidden"
                elif code == "NOT_FOUND":
                    http_status = "404 Not Found"
                elif code in {
                    "CONFLICT_CAS",
                    "IDEMPOTENCY_REPLAY",
                    "GATE_OPEN",
                    "OUTCOME_UNKNOWN",
                    "STALE_ASSET",
                }:
                    http_status = "409 Conflict"
                elif code == "VALIDATION_FAILED":
                    http_status = "400 Bad Request"
                else:
                    http_status = "409 Conflict"
            return _json_response(start_response, http_status, envelope)
        except (AuthRequiredError, AuthzDeniedError) as exc:
            code = getattr(exc, "code", "AUTHZ_DENIED")
            status = "401 Unauthorized" if code == "AUTH_REQUIRED" else "403 Forbidden"
            return _json_response(
                start_response,
                status,
                {"error_code": code, "error": str(exc), "status": "rejected"},
            )
        except json.JSONDecodeError:
            return _json_response(
                start_response,
                "400 Bad Request",
                {"error_code": "VALIDATION_FAILED", "error": "invalid JSON"},
            )
        except FlowError as exc:
            return _json_response(
                start_response,
                "409 Conflict",
                {"error_code": getattr(exc, "code", "FLOW_ERROR"), "error": str(exc)},
            )
        except Exception as exc:
            return _json_response(
                start_response,
                "500 Internal Server Error",
                {"error_code": "A0", "error": str(exc)},
            )

    return _json_response(start_response, "404 Not Found", {"error_code": "NOT_FOUND"})


def main() -> None:
    from wsgiref.simple_server import make_server

    require_configured_service_credentials()
    # Never bind publicly by default; Compose uses an internal network.
    host = os.environ.get("COORDINATOR_HOST", "127.0.0.1")
    port = int(os.environ.get("COORDINATOR_PORT", "9001"))
    with make_server(host, port, application) as httpd:
        print(f"state-coordinator listening on {host}:{port}", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
