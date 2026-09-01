"""Small, typed Herdr command and socket transport.

This module owns only the provider protocol boundary.  It deliberately does
not render errors, choose a Beadhive operation, or import ``work``/Typer.
Every external failure is represented as a :class:`HerdrFailure` so callers
can decide whether and how to present it.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .identity import resolve_session
from .transport_types import FailureCode, HerdrFailure, HerdrResult

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
__all__ = [
    "FailureCode",
    "HerdrClient",
    "HerdrFailure",
    "HerdrResult",
    "HerdrTransport",
    "decode_protocol",
    "decode_response",
    "decode_legacy",
    "invoke_command",
]


def invoke_command(
    argv: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[Any] | None:
    """Run one raw command while retaining the legacy subprocess result shape.

    The compatibility plugin supplies its existing runner, so test monkeypatch
    points and capture semantics remain unchanged.  Typed callers should use
    :meth:`HerdrClient.command` instead.
    """

    kwargs: dict[str, Any] = {"check": False, "capture": True}
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        return runner(argv, **kwargs)
    except TypeError:
        # ``subprocess.run`` calls this argument ``capture_output``; Beadhive's
        # runner intentionally uses the shorter ``capture`` spelling.
        kwargs.pop("capture", None)
        kwargs["capture_output"] = True
        try:
            return runner(argv, **kwargs)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                argv,
                124,
                stdout="",
                stderr=f"timed out after {timeout:g}s" if timeout is not None else "timed out",
            )
        except Exception:  # noqa: BLE001 - optional provider failures are always fenced
            return None
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv,
            124,
            stdout="",
            stderr=f"timed out after {timeout:g}s" if timeout is not None else "timed out",
        )
    except Exception:  # noqa: BLE001 - optional provider failures are always fenced
        return None


def _failure_from_text(text: str, *, default: FailureCode = FailureCode.REFUSED) -> FailureCode:
    lowered = text.lower()
    if any(token in lowered for token in ("timeout", "timed out", "temporarily", "busy")):
        return FailureCode.RETRYABLE
    if any(token in lowered for token in ("malformed", "invalid json", "parse")):
        return FailureCode.MALFORMED
    if any(token in lowered for token in ("stale", "revision")):
        return FailureCode.STALE
    if any(token in lowered for token in ("conflict", "already exists", "duplicate")):
        return FailureCode.CONFLICT
    if any(
        token in lowered
        for token in ("unavailable", "not running", "not found", "connection refused")
    ):
        return FailureCode.UNAVAILABLE
    return default


def decode_protocol(payload: str | bytes | Mapping[str, Any]) -> HerdrResult[Any]:
    """Decode one Herdr JSON response, including its ``id/result/error`` envelope."""

    if isinstance(payload, (str, bytes)):
        try:
            text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
            if not text.strip():
                return HerdrResult.fail(FailureCode.MALFORMED, "Herdr returned an empty response")
            decoded = json.loads(text)
        except (UnicodeDecodeError, TypeError, ValueError):
            return HerdrResult.fail(
                FailureCode.MALFORMED,
                "Herdr returned malformed JSON",
                detail="invalid_json",
            )
    else:
        decoded = dict(payload)
    if not isinstance(decoded, dict):
        return HerdrResult.fail(FailureCode.MALFORMED, "Herdr response must be a JSON object")
    error = decoded.get("error")
    if error is not None:
        if isinstance(error, dict):
            provider_code = str(error.get("code") or "")
            message = "Herdr refused the request"
            code = _failure_from_text(provider_code or message)
            detail = "provider_refusal"
        else:
            message, detail = "Herdr refused the request", "provider_refusal"
            code = FailureCode.REFUSED
        return HerdrResult.fail(code, message, detail=detail)
    if "result" in decoded:
        result = decoded["result"]
        if not isinstance(result, (dict, list, str, int, float, bool)) and result is not None:
            return HerdrResult.fail(FailureCode.MALFORMED, "Herdr result has an invalid shape")
        return HerdrResult.ok(result)
    return HerdrResult.ok(decoded)


def decode_legacy(payload: str | bytes) -> Any:
    """Compatibility projection retaining envelopes and plain-text responses."""

    try:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        return json.loads(text)
    except (UnicodeDecodeError, TypeError, ValueError):
        return payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload


class HerdrClient:
    """Injectable command/socket client with exact session scoping."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        socket_factory: Callable[..., socket.socket] = socket.socket,
        session: str | None = None,
        environment: Mapping[str, str] | None = None,
        executable: str = "herdr",
    ) -> None:
        self._runner = runner or subprocess.run
        self._socket_factory = socket_factory
        self._selection = resolve_session(session, environment=environment)
        self.executable = executable

    @property
    def session(self) -> str:
        return self._selection.name

    def command(
        self,
        *args: str,
        timeout: float | None = None,
        check: bool = False,
    ) -> HerdrResult[Any]:
        argv = [self.executable, "--session", self.session, *args]
        try:
            completed = self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return HerdrResult.fail(
                FailureCode.RETRYABLE,
                "Herdr command timed out",
                detail="command_timeout",
                retryable=True,
            )
        except (OSError, RuntimeError):
            return HerdrResult.fail(
                FailureCode.UNAVAILABLE,
                "Herdr command is unavailable",
                detail="command_unavailable",
            )
        if completed.returncode:
            detail = f"exit_status={completed.returncode}"
            provider_text = str(completed.stderr or completed.stdout or "").strip()
            code = _failure_from_text(provider_text, default=FailureCode.REFUSED)
            return HerdrResult.fail(code, "Herdr command was refused", detail=detail)
        output = completed.stdout or ""
        decoded = decode_protocol(output)
        if check and not decoded.is_ok:
            return decoded
        return decoded

    execute = command

    def socket_request(
        self,
        socket_path: str | Path,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = 60.0,
        cancelled: threading.Event | None = None,
        request_id: str | None = None,
    ) -> HerdrResult[Any]:
        """Send one bounded NDJSON request and verify its correlation ID."""

        request_id = request_id or f"bh_{uuid.uuid4().hex}"
        request = {"id": request_id, "method": method, "params": dict(params or {})}
        if cancelled is not None and cancelled.is_set():
            return HerdrResult.fail(
                FailureCode.RETRYABLE,
                "Herdr request was cancelled",
                retryable=True,
            )
        try:
            client = self._socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
            with client:
                client.settimeout(timeout)
                client.connect(str(socket_path))
                encoded = (json.dumps(request, separators=(",", ":")) + "\n").encode()
                client.sendall(encoded)
                # Herdr's documented socket adapter is line-oriented.  Prefer
                # makefile when available (and keep a recv fallback for tiny
                # fake/real clients that expose only the socket primitives).
                if hasattr(client, "makefile"):
                    with client.makefile("rb") as stream:
                        response = stream.readline(1024 * 1024 + 1)
                    chunks = bytearray(response)
                else:
                    deadline = __import__("time").monotonic() + timeout
                    chunks = bytearray()
                while not hasattr(client, "makefile") and len(chunks) <= 1024 * 1024:
                    if cancelled is not None and cancelled.is_set():
                        return HerdrResult.fail(
                            FailureCode.RETRYABLE,
                            "Herdr request was cancelled",
                            retryable=True,
                        )
                    remaining = deadline - __import__("time").monotonic()
                    if remaining <= 0:
                        return HerdrResult.fail(
                            FailureCode.RETRYABLE,
                            "Herdr socket request timed out",
                            retryable=True,
                        )
                    client.settimeout(min(remaining, 0.25))
                    try:
                        chunk = client.recv(65536)
                    except TimeoutError:
                        continue
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if b"\n" in chunk:
                        break
                if not chunks or len(chunks) > 1024 * 1024:
                    return HerdrResult.fail(
                        FailureCode.MALFORMED,
                        "Herdr returned an empty or oversized response",
                    )
            try:
                response = json.loads(bytes(chunks).splitlines()[0])
            except (UnicodeDecodeError, TypeError, ValueError):
                return HerdrResult.fail(
                    FailureCode.MALFORMED,
                    "Herdr returned malformed socket JSON",
                    detail="invalid_json",
                )
        except TimeoutError:
            return HerdrResult.fail(
                FailureCode.RETRYABLE,
                "Herdr socket request timed out",
                detail="socket_timeout",
                retryable=True,
            )
        except (OSError, TypeError, ValueError):
            return HerdrResult.fail(
                FailureCode.UNAVAILABLE,
                "Herdr socket is unavailable",
                detail="socket_unavailable",
            )
        if not isinstance(response, dict) or response.get("id") != request_id:
            return HerdrResult.fail(FailureCode.CONFLICT, "Herdr socket response ID did not match")
        return decode_protocol(response)


HerdrTransport = HerdrClient
decode_response = decode_protocol
