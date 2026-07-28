"""Trusted-proxy client address policy and spoofing regressions."""

from __future__ import annotations

import ipaddress

import pytest
from starlette.requests import Request


def _request(
    peer: str,
    *,
    headers: tuple[tuple[str, str], ...] = (),
) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/live",
        "raw_path": b"/api/v1/live",
        "query_string": b"",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
        "client": (peer, 43120),
        "server": ("testserver", 80),
    }
    return Request(scope)


def _context_ip(
    monkeypatch: pytest.MonkeyPatch,
    peer: str,
    *,
    cidrs: list[str],
    headers: tuple[tuple[str, str], ...] = (),
) -> str:
    from app.config import settings
    from app.utils.request_context import install_request_context

    monkeypatch.setattr(settings, "trusted_proxy_cidrs", cidrs)
    return install_request_context(_request(peer, headers=headers)).ip


def test_unconfigured_proxy_list_ignores_xff_forwarded_and_real_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        _context_ip(
            monkeypatch,
            "10.0.0.2",
            cidrs=[],
            headers=(
                ("X-Forwarded-For", "8.8.8.8"),
                ("Forwarded", "for=8.8.8.8"),
                ("X-Real-IP", "8.8.8.8"),
            ),
        )
        == "10.0.0.2"
    )


def test_one_configured_proxy_hop_uses_valid_external_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        _context_ip(
            monkeypatch,
            "10.20.30.40",
            cidrs=["10.0.0.0/8"],
            headers=(("X-Forwarded-For", "8.8.8.8"),),
        )
        == "8.8.8.8"
    )


def test_multi_hop_chain_skips_configured_private_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        _context_ip(
            monkeypatch,
            "10.20.30.40",
            cidrs=["10.0.0.0/8", "192.168.0.0/16"],
            headers=(
                ("X-Forwarded-For", "8.8.8.8, 10.1.1.1, 192.168.1.4"),
            ),
        )
        == "8.8.8.8"
    )


@pytest.mark.parametrize(
    "header",
    [
        "8.8.8.8,not-an-ip",
        "8.8.8.8,",
        "[2001:db8::1]",
        "10.1.2.3",
        "127.0.0.1",
        "192.168.1.9",
        "fd00::9",
    ],
)
def test_malformed_invalid_or_private_chain_falls_back_to_peer(
    monkeypatch: pytest.MonkeyPatch,
    header: str,
) -> None:
    assert (
        _context_ip(
            monkeypatch,
            "10.20.30.40",
            cidrs=["10.0.0.0/8"],
            headers=(("X-Forwarded-For", header),),
        )
        == "10.20.30.40"
    )


def test_duplicate_xff_values_are_ambiguous_and_fall_back_to_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        _context_ip(
            monkeypatch,
            "10.20.30.40",
            cidrs=["10.0.0.0/8"],
            headers=(
                ("X-Forwarded-For", "8.8.8.8"),
                ("X-Forwarded-For", "9.9.9.9"),
            ),
        )
        == "10.20.30.40"
    )


def test_overlong_and_over_hop_limit_chains_fall_back_to_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.utils.request_context import MAX_FORWARDED_FOR_HOPS

    overlong = "8.8.8.8" * 700
    too_many_hops = ", ".join("8.8.8.8" for _ in range(MAX_FORWARDED_FOR_HOPS + 1))
    for header in (overlong, too_many_hops):
        assert (
            _context_ip(
                monkeypatch,
                "10.20.30.40",
                cidrs=["10.0.0.0/8"],
                headers=(("X-Forwarded-For", header),),
            )
            == "10.20.30.40"
        )


def test_cidr_boundary_and_non_proxy_peer_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = ["10.20.30.0/24"]
    assert (
        _context_ip(
            monkeypatch,
            "10.20.30.255",
            cidrs=configured,
            headers=(("X-Forwarded-For", "8.8.8.8"),),
        )
        == "8.8.8.8"
    )
    assert (
        _context_ip(
            monkeypatch,
            "10.20.31.1",
            cidrs=configured,
            headers=(("X-Forwarded-For", "8.8.8.8"),),
        )
        == "10.20.31.1"
    )


def test_settings_strictly_canonicalize_and_redact_invalid_proxy_cidr(
    settings_env: dict[str, str],
) -> None:
    from app.config import Settings

    configured = Settings(
        _env_file=None,  # type: ignore[call-arg]
        trusted_proxy_cidrs=["10.20.30.0/24", "2001:db8::/32"],
    )
    assert configured.trusted_proxy_cidrs == ["10.20.30.0/24", "2001:db8::/32"]
    assert all(
        isinstance(ipaddress.ip_network(value), (ipaddress.IPv4Network, ipaddress.IPv6Network))
        for value in configured.trusted_proxy_cidrs
    )

    canary = "10.20.30.7/24"
    with pytest.raises(ValueError) as error:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            trusted_proxy_cidrs=[canary],
        )
    assert canary not in str(error.value)
