"""OPA query helper — distinguishes missing policies from deny decisions.

When an OPA data path has no evaluated rule (policy package not loaded,
or rule missing without a `default`), OPA's response has **no** ``result``
field — just ``{"decision_id": "..."}``. Callers that do
``resp.json().get("result", {})`` silently treat this as "rule returned
empty dict", which then collapses to ``allowed=False``, ``min_score=0.0``,
etc. — a silent deny with misleading diagnostics.

This module surfaces that situation explicitly:

* :func:`opa_query` raises :class:`OpaPolicyMissingError` when ``result``
  is absent so callers can log the real cause and still fail-closed.
* :func:`verify_required_policies` is called once at service startup
  to refuse to boot if any required data path is unreachable.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class OpaPolicyMissingError(RuntimeError):
    """Raised when an OPA data path returns no ``result`` field.

    Attributes:
        package_path: The policy path that was queried (e.g.
            ``"pb/ingestion/quality_gate"``), so callers can include it
            in log messages and rejection reasons.
    """

    def __init__(self, package_path: str):
        self.package_path = package_path
        super().__init__(
            f"OPA policy {package_path!r} not loaded — response has no 'result' "
            f"field. Check that opa-policies/ is mounted and the package is "
            f"declared in a .rego file."
        )


async def opa_query(
    client: httpx.AsyncClient,
    opa_url: str,
    package_path: str,
    input_data: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
) -> Any:
    """POST an input to an OPA data path and return the evaluated ``result``.

    Args:
        client: An existing :class:`httpx.AsyncClient` (typically the
            service-wide client used elsewhere).
        opa_url: Base URL of the OPA server, e.g. ``http://opa:8181``.
        package_path: The rule/data path to evaluate, without leading
            ``/v1/data/`` — e.g. ``"pb/ingestion/quality_gate"`` or
            ``"pb/access/allow"``. Dots are also accepted and normalized
            to slashes so callers can use the Rego form
            (``"pb.ingestion.quality_gate"``) interchangeably.
        input_data: The OPA ``input`` payload. Defaults to ``{}``.
        timeout: Optional per-call override for the HTTP timeout. When
            ``None`` (default), the client's configured timeout applies.

    Returns:
        Whatever OPA evaluated the rule to. The exact shape depends on
        the rule — callers are responsible for validating it.

    Raises:
        OpaPolicyMissingError: The response body had no ``result`` field,
            indicating the policy package or rule is not loaded.
        httpx.HTTPError: Any transport-level failure. Callers typically
            wrap this in a fail-closed branch with a diagnostic log.
    """
    path = package_path.strip("/").replace(".", "/")
    kwargs: dict[str, Any] = {"json": {"input": input_data or {}}}
    if timeout is not None:
        kwargs["timeout"] = timeout

    resp = await client.post(f"{opa_url}/v1/data/{path}", **kwargs)
    resp.raise_for_status()
    body = resp.json()

    if "result" not in body:
        raise OpaPolicyMissingError(path)
    return body["result"]


async def verify_required_policies(
    client: httpx.AsyncClient,
    opa_url: str,
    required_paths: list[str],
    *,
    timeout: float | None = 5.0,
) -> None:
    """Probe each required OPA data path once; raise if any are unreachable.

    Intended for FastAPI startup hooks: if a service depends on specific
    OPA rules, missing them should be a loud boot-time failure rather
    than a confusing runtime deny.

    Implementation: POSTs ``{"input": {}}`` to each path. If the response
    has no ``result`` field or the request errors, the path is
    considered unavailable. Having a ``result`` that evaluates to
    ``false`` or ``{}`` is **fine** — that only means the rule
    legitimately says "no" for the empty input, which is a valid
    configuration.

    Args:
        client: Shared httpx client.
        opa_url: Base URL of the OPA server.
        required_paths: Data paths to probe, in Rego or slash form.
        timeout: Optional per-probe timeout. Short default (5s) so
            startup doesn't hang on an unreachable OPA.

    Raises:
        RuntimeError: One or more required paths are not loaded or
            the OPA server is unreachable. The message lists every
            failing path so operators see the full picture.
    """
    missing: list[str] = []
    for path in required_paths:
        try:
            await opa_query(client, opa_url, path, input_data={}, timeout=timeout)
        except OpaPolicyMissingError as exc:
            missing.append(exc.package_path)
        except Exception as exc:  # network, HTTP, parse — all treated as "not loaded"
            missing.append(f"{path} (probe error: {exc})")

    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            f"OPA startup check failed — required policies missing or unreachable: "
            f"{joined}. Check that opa-policies/ is mounted at OPA and that "
            f"the server at {opa_url} is healthy."
        )

    log.info("OPA startup check OK (%d required paths loaded)", len(required_paths))
