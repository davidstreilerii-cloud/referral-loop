"""The assertion we sign, and what a verifier gets when they check it."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from referral_loop.connect.auth import ASSERTION_LIFETIME, REFRESH_MARGIN, Token, TokenCache, build_assertion
from referral_loop.connect.connectors import ConnectorRegistry

from ._certs import rsa_keypair

_NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


def _b64u_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _profile(key_path, algorithm: str = "RS384"):
    return ConnectorRegistry.from_mapping(
        {
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
                        "client_id": "client-abc",
                        "private_key_file": str(key_path),
                        "key_id": "example-med-2026",
                        "algorithm": algorithm,
                        "scopes": ["system/Patient.read"],
                    },
                    "authorities": [],
                }
            ]
        }
    ).get("example-med")


def test_the_assertion_has_three_segments(tmp_path):
    key_path, _ = rsa_keypair(tmp_path)
    assert build_assertion(_profile(key_path), now=_NOW).count(".") == 2


def test_the_header_names_the_algorithm_and_the_key(tmp_path):
    key_path, _ = rsa_keypair(tmp_path)
    header = json.loads(_b64u_decode(build_assertion(_profile(key_path), now=_NOW).split(".")[0]))
    assert header == {"alg": "RS384", "typ": "JWT", "kid": "example-med-2026"}


def test_the_claims_are_what_smart_backend_services_requires(tmp_path):
    key_path, _ = rsa_keypair(tmp_path)
    claims = json.loads(_b64u_decode(build_assertion(_profile(key_path), now=_NOW).split(".")[1]))
    assert claims["iss"] == "client-abc"
    assert claims["sub"] == "client-abc"
    assert claims["aud"] == "https://auth.example-med.example/oauth2/token"
    assert claims["exp"] == int((_NOW + ASSERTION_LIFETIME).timestamp())
    assert claims["jti"]


def test_the_assertion_expires_within_five_minutes(tmp_path):
    key_path, _ = rsa_keypair(tmp_path)
    claims = json.loads(_b64u_decode(build_assertion(_profile(key_path), now=_NOW).split(".")[1]))
    assert claims["exp"] - int(_NOW.timestamp()) <= 300


def test_the_signature_verifies_against_the_public_key(tmp_path):
    key_path, public = rsa_keypair(tmp_path)
    token = build_assertion(_profile(key_path), now=_NOW)
    header_b64, claims_b64, sig_b64 = token.split(".")
    public.verify(
        _b64u_decode(sig_b64),
        f"{header_b64}.{claims_b64}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA384(),
    )


def test_rs256_signs_with_sha256(tmp_path):
    key_path, public = rsa_keypair(tmp_path)
    token = build_assertion(_profile(key_path, algorithm="RS256"), now=_NOW)
    header_b64, claims_b64, sig_b64 = token.split(".")
    public.verify(
        _b64u_decode(sig_b64),
        f"{header_b64}.{claims_b64}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_two_assertions_differ_under_a_frozen_clock(tmp_path):
    """jti comes from secrets, not from the clock. A clock-derived jti collides under a frozen
    clock -- which is exactly when a replay test would stop catching anything."""
    key_path, _ = rsa_keypair(tmp_path)
    profile = _profile(key_path)
    first = json.loads(_b64u_decode(build_assertion(profile, now=_NOW).split(".")[1]))
    second = json.loads(_b64u_decode(build_assertion(profile, now=_NOW).split(".")[1]))
    assert first["jti"] != second["jti"]


def test_a_missing_key_file_is_a_typed_failure(tmp_path):
    from referral_loop.connect.auth import AuthFailure

    with pytest.raises(AuthFailure, match="private key"):
        build_assertion(_profile(tmp_path / "absent.pem"), now=_NOW)


def test_a_token_knows_whether_it_is_still_usable():
    token = Token(value="abc", expires_at=_NOW + timedelta(seconds=300))
    assert token.usable_at(_NOW)
    assert not token.usable_at(_NOW + timedelta(seconds=300) - REFRESH_MARGIN + timedelta(seconds=1))


def test_the_cache_returns_the_same_token_until_the_refresh_margin(tmp_path):
    calls = []

    def acquire():
        calls.append(1)
        return Token(value=f"t{len(calls)}", expires_at=_NOW + timedelta(seconds=300))

    cache = TokenCache()
    first = cache.get("example-med", acquire, now=_NOW)
    second = cache.get("example-med", acquire, now=_NOW + timedelta(seconds=60))
    assert first.value == second.value == "t1"
    assert len(calls) == 1


def test_the_cache_reacquires_inside_the_refresh_margin(tmp_path):
    calls = []

    def acquire():
        calls.append(1)
        return Token(value=f"t{len(calls)}", expires_at=_NOW + timedelta(seconds=300))

    cache = TokenCache()
    cache.get("example-med", acquire, now=_NOW)
    later = cache.get("example-med", acquire, now=_NOW + timedelta(seconds=299))
    assert later.value == "t2"
    assert len(calls) == 2


def test_two_connectors_do_not_share_a_cache_entry():
    cache = TokenCache()
    a = cache.get("example-med", lambda: Token("a", _NOW + timedelta(seconds=300)), now=_NOW)
    b = cache.get("other", lambda: Token("b", _NOW + timedelta(seconds=300)), now=_NOW)
    assert a.value == "a" and b.value == "b"
