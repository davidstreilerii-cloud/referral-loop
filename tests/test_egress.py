"""The allowlist, and the four urllib defaults that are wrong for this client."""

from __future__ import annotations

import urllib.request

import pytest

from referral_loop.connect.connectors import ConnectorRegistry
from referral_loop.connect.egress import (
    MAX_RESPONSE_BYTES,
    EgressRefused,
    check_allowed,
    build_opener,
)


def _registry(**top) -> ConnectorRegistry:
    data = {
        "connectors": [
            {
                "connector_id": "example-med",
                "organization": "Example Medical Center",
                "vendor": "epic",
                "fhir_base_url": "https://fhir.example-med.example/api/FHIR/R4",
                "token_url": "https://auth.example-med.example/oauth2/token",
                "fhir_version": ["4.0.1"],
                "auth": {
                    "mode": "smart-backend-services",
                    "client_id": "abc",
                    "private_key_file": "/etc/k.pem",
                    "key_id": "k1",
                    "algorithm": "RS384",
                    "scopes": ["system/Patient.read"],
                },
                "authorities": [],
            }
        ]
    }
    data.update(top)
    return ConnectorRegistry.from_mapping(data)


def test_a_configured_host_is_allowed():
    check_allowed(_registry(), "https://fhir.example-med.example/api/FHIR/R4/metadata")


def test_the_token_host_is_allowed_too():
    check_allowed(_registry(), "https://auth.example-med.example/oauth2/token")


def test_an_unconfigured_host_is_refused():
    with pytest.raises(EgressRefused, match="not a configured connector endpoint"):
        check_allowed(_registry(), "https://evil.example/api")


def test_a_configured_host_on_another_port_is_refused():
    """The allowlist is (scheme, host, port). A host that is allowed on 443 is not thereby
    allowed on 8443 -- that is a different service."""
    with pytest.raises(EgressRefused):
        check_allowed(_registry(), "https://fhir.example-med.example:8443/api")


def test_an_explicit_default_port_is_the_same_destination():
    """https://h/a and https://h:443/b must not read as two different hosts."""
    check_allowed(_registry(), "https://fhir.example-med.example:443/api/FHIR/R4/metadata")


def test_plaintext_is_refused_even_to_a_configured_host():
    with pytest.raises(EgressRefused, match="https"):
        check_allowed(_registry(), "http://fhir.example-med.example/api")


def _plaintext_registry() -> ConnectorRegistry:
    data = {
        "allow_plaintext": True,
        "plaintext_hosts": ["fhir.local"],
        "connectors": [
            {
                "connector_id": "dev",
                "organization": "Local development",
                "vendor": "epic",
                "fhir_base_url": "http://fhir.local/api/FHIR/R4",
                "token_url": "http://fhir.local/oauth2/token",
                "fhir_version": ["4.0.1"],
                "auth": {
                    "mode": "smart-backend-services",
                    "client_id": "abc",
                    "private_key_file": "/etc/k.pem",
                    "key_id": "k1",
                    "algorithm": "RS384",
                    "scopes": ["system/Patient.read"],
                },
                "authorities": [],
            }
        ],
    }
    return ConnectorRegistry.from_mapping(data)


def test_plaintext_is_allowed_when_both_statements_are_present():
    check_allowed(_plaintext_registry(), "http://fhir.local/api/FHIR/R4/metadata")


def test_plaintext_to_a_host_not_named_in_plaintext_hosts_is_refused():
    """allow_plaintext is not a global switch. It permits the named hosts and nothing else, so
    turning it on for a dev server does not open every configured connector to plaintext."""
    registry = _plaintext_registry()
    with pytest.raises(EgressRefused, match="https"):
        check_allowed(registry, "http://other.local/api")


def test_the_opener_ignores_proxy_environment_variables(monkeypatch):
    """urllib reads http_proxy/https_proxy from the environment by default. On a hospital
    network that is frequently set, and honouring it routes PHI and credentials through a host
    nobody put in the registry.

    The assertion is that **no** ProxyHandler survives in the chain, which is subtler than it
    looks and is worth stating. Passing `ProxyHandler({})` to build_opener does two things:
    build_opener sees an instance of ProxyHandler among the handlers and therefore skips
    installing its own environment-reading default, and then add_handler discards the empty one
    because a ProxyHandler built from an empty mapping registers no *_open methods and
    add_handler only keeps handlers that register at least one. Both steps have to happen for
    the environment to be ignored.

    Which is exactly why this test asserts zero rather than one: with the environment set, if
    someone deletes the `ProxyHandler({})` argument as apparently useless, build_opener installs
    its default, that default reads http_proxy, it registers http_open/https_open, add_handler
    keeps it -- and this test goes from zero to one and fails. The empty handler looks inert and
    is load-bearing."""
    monkeypatch.setenv("https_proxy", "http://proxy.internal:3128")
    monkeypatch.setenv("http_proxy", "http://proxy.internal:3128")
    opener = build_opener(_registry().get("example-med"))
    proxies = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert proxies == [], (
        "a ProxyHandler in the chain means the environment was consulted: "
        f"{[p.proxies for p in proxies]}"
    )


def test_the_opener_refuses_redirects():
    """A 302 from the token endpoint hands our client assertion -- or a live bearer token -- to
    whoever answered."""
    from referral_loop.connect.egress import _RefuseRedirects

    handler = _RefuseRedirects()
    with pytest.raises(EgressRefused, match="redirect"):
        handler.redirect_request(
            req=None, fp=None, code=302, msg="Found",
            headers={}, newurl="https://fhir.example-med.example/elsewhere",
        )


def test_a_redirect_to_an_allowed_host_is_still_refused():
    """Not re-resolved against the allowlist: a redirect to a listed host is still a server we
    did not intend to talk to for this request."""
    from referral_loop.connect.egress import _RefuseRedirects

    with pytest.raises(EgressRefused, match="redirect"):
        _RefuseRedirects().redirect_request(
            req=None, fp=None, code=301, msg="Moved",
            headers={}, newurl="https://auth.example-med.example/oauth2/token",
        )


def test_the_response_cap_is_declared_and_bounded():
    assert 0 < MAX_RESPONSE_BYTES <= 64 * 1024 * 1024
