"""Safe request and worker correlation metadata.

Only opaque identifiers belong in this context. Request bodies, query strings,
headers, user names, e-mail addresses, file names, and filesystem paths must
never be added here because the context is consumed by operational logging.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Iterator
from uuid import uuid4

from fastapi import Request

REQUEST_ID_HEADER = "X-Request-ID"
CORRELATION_ID_HEADER = "X-Correlation-ID"
MAX_EXTERNAL_ID_LENGTH = 64
MAX_EXTERNAL_CORRELATION_LENGTH = 1024
# Forwarded client metadata is optional and remains deliberately small.  These
# limits apply before parsing so a proxy cannot turn audit-context creation into
# an unbounded header-processing operation.
MAX_FORWARDED_FOR_BYTES = 4096
MAX_FORWARDED_FOR_HOPS = 16

_EXTERNAL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_INTERNAL_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,31}")
_JWT_PATTERN = re.compile(r"[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}")
_DIGESTED_CORRELATION_PATTERN = re.compile(r"corr:[a-f0-9]{32}")
_CREDENTIAL_PREFIXES = (
    "akia",
    "api-key",
    "apikey",
    "authorization",
    "basic",
    "bearer",
    "credential",
    "ghp_",
    "github_pat_",
    "glpat-",
    "password",
    "passwd",
    "secret",
    "sk-",
    "token",
    "xox",
)
_REQUEST_CONTEXT_STATE_KEY = "docvault_request_context"

_PRIVATE_CLIENT_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


@dataclass(frozen=True)
class RequestContext:
    """Opaque correlation metadata shared by API and worker-style operations."""

    request_id: str = ""
    correlation_id: str = ""
    actor_id: str = ""
    document_id: int | None = None
    job_id: str = ""
    ip: str = ""
    user_agent: str = ""


_current_context: ContextVar[RequestContext | None] = ContextVar(
    "docvault_request_context",
    default=None,
)


def normalize_external_id(value: object) -> str | None:
    """Accept a bounded, header-safe opaque ID or return ``None``."""

    if not isinstance(value, str) or len(value) > MAX_EXTERNAL_ID_LENGTH:
        return None
    if _EXTERNAL_ID_PATTERN.fullmatch(value) is None:
        return None
    lowered = value.lower()
    if lowered.startswith(_CREDENTIAL_PREFIXES):
        return None
    if _JWT_PATTERN.fullmatch(value) is not None:
        return None
    return value


def new_request_id() -> str:
    """Generate an opaque request/correlation identifier."""

    return uuid4().hex


def digest_external_correlation_id(value: object) -> str | None:
    """Convert an untrusted correlation header into a non-reversible token.

    An already-sanitized DocVault token is stable across service hops. All
    other bounded values are keyed with the deployment signing material before
    they may enter logs, audit rows, worker contexts, or response headers.
    """

    if not isinstance(value, str) or not value:
        return None
    if _DIGESTED_CORRELATION_PATTERN.fullmatch(value) is not None:
        return value
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_EXTERNAL_CORRELATION_LENGTH:
        return None

    # Imported lazily to keep this pure context module independent from
    # settings initialization and to avoid copying key material into globals.
    from ..config import settings

    secret = settings.secret_key.encode("utf-8")
    if not secret:
        return None
    digest = hmac.new(
        secret,
        b"docvault-correlation-v1\0" + encoded,
        hashlib.sha256,
    ).hexdigest()
    return f"corr:{digest[:32]}"


def safe_actor_identifier(user_id: object) -> str:
    """Return an opaque actor reference without accepting names or e-mail."""

    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        return ""
    return f"user:{user_id}"


def safe_document_identifier(document_id: object) -> int | None:
    """Return a positive database identifier, never an arbitrary string."""

    if (
        isinstance(document_id, bool)
        or not isinstance(document_id, int)
        or document_id < 1
    ):
        return None
    return document_id


def _safe_ip(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return ""


def _configured_trusted_proxy_networks() -> tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network,
    ...,
]:
    """Read the operator's proxy allowlist, failing closed on any anomaly."""

    # Import lazily to keep this utility usable by worker/test contexts without
    # forcing settings initialization at module import time.
    try:
        from ..config import settings

        raw_networks = settings.trusted_proxy_cidrs
        parsed: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for raw_network in raw_networks:
            network = ipaddress.ip_network(raw_network, strict=True)
            if network.with_prefixlen != raw_network:
                return ()
            parsed.append(network)
        return tuple(parsed)
    except (AttributeError, TypeError, ValueError):
        return ()


def _is_private_client_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Reject private client claims while permitting documentation test nets."""

    return (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or any(address in network for network in _PRIVATE_CLIENT_NETWORKS)
    )


def _request_client_ip(request: Request) -> str:
    """Return the safe client address for audit/request context.

    Without explicit trusted proxy CIDRs, only the ASGI immediate peer is
    usable.  If that peer is trusted, a single bounded X-Forwarded-For header
    is scanned from nearest to farthest; trusted hops are skipped and the first
    valid untrusted address is selected.  Forwarded and X-Real-IP are never
    consulted.  Any malformed, overlong, private, duplicate, or otherwise
    ambiguous chain falls back to the immediate peer.
    """

    peer = _safe_ip(request.client.host if request.client else None)
    if not peer:
        return ""

    networks = _configured_trusted_proxy_networks()
    if not networks:
        return peer
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_address in network for network in networks):
        return peer

    # Starlette exposes duplicate header values through getlist(); accepting
    # more than one value would make comma/line folding ambiguous.
    try:
        header_values = request.headers.getlist("x-forwarded-for")
    except AttributeError:
        header_value = request.headers.get("x-forwarded-for")
        header_values = [header_value] if header_value is not None else []
    if len(header_values) != 1:
        return peer
    header_value = header_values[0]
    try:
        encoded_header = header_value.encode("utf-8")
    except UnicodeError:
        return peer
    if len(encoded_header) > MAX_FORWARDED_FOR_BYTES:
        return peer

    raw_hops = header_value.split(",")
    if not raw_hops or len(raw_hops) > MAX_FORWARDED_FOR_HOPS:
        return peer
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for raw_hop in raw_hops:
        hop = raw_hop.strip()
        if not hop or "%" in hop or len(hop) > 128:
            return peer
        try:
            parsed_address: ipaddress.IPv4Address | ipaddress.IPv6Address = (
                ipaddress.ip_address(hop)
            )
        except ValueError:
            return peer
        addresses.append(parsed_address)

    for address in reversed(addresses):
        if any(address in network for network in networks):
            continue
        if _is_private_client_address(address):
            # A private nearest-untrusted claim can be supplied by an attacker
            # and is not a reliable external client identity.  Private hops
            # that are themselves in the configured proxy networks are fine.
            return peer
        return str(address)
    return peer


def _context_from_request(request: Request) -> RequestContext:
    # Request IDs are always server generated. An external value may contain
    # credentials or PII even when its syntax looks harmless.
    request_id = new_request_id()
    correlation_id = (
        digest_external_correlation_id(
            request.headers.get(CORRELATION_ID_HEADER),
        )
        or request_id
    )
    return RequestContext(
        request_id=request_id,
        correlation_id=correlation_id,
        ip=_request_client_ip(request),
        # User-Agent is intentionally not copied. It is untrusted free-form text
        # and has historically been abused to smuggle credentials into logs.
        user_agent="",
    )


def install_request_context(request: Request) -> RequestContext:
    """Create and attach the initial context for one HTTP request."""

    context = _context_from_request(request)
    setattr(request.state, _REQUEST_CONTEXT_STATE_KEY, context)
    return context


def get_request_context(request: Request | None) -> RequestContext:
    """Read request state, falling back to the currently bound worker context."""

    if request is not None:
        context = getattr(request.state, _REQUEST_CONTEXT_STATE_KEY, None)
        if isinstance(context, RequestContext):
            return context
        return install_request_context(request)
    return _current_context.get() or RequestContext()


def bind_actor_to_request(request: Request, user_id: object) -> RequestContext:
    """Attach only the opaque database actor ID to request state."""

    context = get_request_context(request)
    updated = replace(context, actor_id=safe_actor_identifier(user_id))
    setattr(request.state, _REQUEST_CONTEXT_STATE_KEY, updated)
    return updated


def context_with_actor(
    context: RequestContext | None,
    user_id: object,
) -> RequestContext:
    """Return a copy with a safe actor reference."""

    return replace(
        context or RequestContext(),
        actor_id=safe_actor_identifier(user_id),
    )


def context_with_document(
    context: RequestContext | None,
    document_id: object,
) -> RequestContext:
    """Return a copy with a safe document reference."""

    return replace(
        context or RequestContext(),
        document_id=safe_document_identifier(document_id),
    )


def worker_context(
    kind: str,
    *,
    parent: RequestContext | None = None,
) -> RequestContext:
    """Create a correlated context for a bounded background operation."""

    inherited = parent or _current_context.get() or RequestContext()
    safe_kind = kind if _INTERNAL_TOKEN_PATTERN.fullmatch(kind) else "worker"
    correlation_id = inherited.correlation_id or new_request_id()
    return replace(
        inherited,
        correlation_id=correlation_id,
        job_id=f"job:{safe_kind}:{uuid4().hex[:16]}",
    )


@contextmanager
def bound_request_context(
    context: RequestContext,
) -> Iterator[RequestContext]:
    """Bind context for the current async task/thread and always restore it."""

    token: Token[RequestContext | None] = _current_context.set(context)
    try:
        yield context
    finally:
        _current_context.reset(token)
