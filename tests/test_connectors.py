"""The outbound registry, and the values it refuses to guess."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from referral_loop.connect.connectors import (
    ConnectorConfigError,
    ConnectorRegistry,
    endpoint_of,
    load_connector_registry,
)
from referral_loop.peers import RESERVED_PEER_IDS


def _profile(**overrides) -> dict:
    base = {
        "connector_id": "example-med",
        "organization": "Example Medical Center",
        "vendor": "epic",
        "fhir_base_url": "https://fhir.example-med.example/api/FHIR/R4",
        "token_url": "https://fhir.example-med.example/oauth2/token",
        "fhir_version": ["4.0.1"],
        "auth": {
            "mode": "smart-backend-services",
            "client_id": "abc-123",
            "private_key_file": "/etc/referral/example-med-signing.pem",
            "key_id": "example-med-2026",
            "algorithm": "RS384",
            "scopes": ["system/Patient.read"],
        },
        "authorities": [],
    }
    base.update(overrides)
    return base


def _registry(*profiles, **top) -> ConnectorRegistry:
    data = {"connectors": list(profiles) or [_profile()]}
    data.update(top)
    return ConnectorRegistry.from_mapping(data)


def test_a_well_formed_profile_loads():
    reg = _registry()
    assert reg.connector_ids() == ("example-med",)
    ku = reg.get("example-med")
    assert ku.organization == "Example Medical Center"
    assert ku.auth.algorithm == "RS384"
    assert ku.accepts_version("4.0.1")
    assert not ku.accepts_version("3.0.2")


@pytest.mark.parametrize(
    "field",
    [
        "connector_id",
        "organization",
        "vendor",
        "fhir_base_url",
        "token_url",
        "fhir_version",
        "auth",
    ],
)
def test_a_missing_required_field_refuses_rather_than_defaulting(field):
    """Every one of these is a security or clinical decision. A default would be us making it."""
    broken = _profile()
    del broken[field]
    with pytest.raises(ConnectorConfigError, match=field):
        _registry(broken)


@pytest.mark.parametrize("key", ["client_id", "private_key_file", "key_id", "algorithm", "scopes"])
def test_a_missing_auth_field_refuses(key):
    auth = dict(_profile()["auth"])
    del auth[key]
    with pytest.raises(ConnectorConfigError, match=key):
        _registry(_profile(auth=auth))


@pytest.mark.parametrize("bad", ["EXAMPLE-MED", "-ku", "ku med", "k" * 65, "", "ku/med"])
def test_a_malformed_connector_id_refuses(bad):
    """The id lands in audit rows and log lines, so it is constrained once here rather than
    sanitized at each site -- the same argument peers.py makes for a peer id."""
    with pytest.raises(ConnectorConfigError, match="connector_id"):
        _registry(_profile(connector_id=bad))


@pytest.mark.parametrize("url_field", ["fhir_base_url", "token_url"])
def test_a_plaintext_url_refuses_without_the_opt_out(url_field):
    with pytest.raises(ConnectorConfigError, match="https"):
        _registry(_profile(**{url_field: "http://fhir.example-med.example/api/FHIR/R4"}))


def test_an_unknown_authority_refuses():
    """Drawn from peers.AUTHORITIES so there is one vocabulary rather than two that drift."""
    with pytest.raises(ConnectorConfigError, match="authorit"):
        _registry(_profile(authorities=["admit"]))


def test_a_granted_authority_is_readable():
    reg = _registry(_profile(authorities=["result"]))
    assert reg.get("example-med").holds("result")
    assert not reg.get("example-med").holds("cancel")


def test_an_empty_fhir_version_list_refuses():
    with pytest.raises(ConnectorConfigError, match="fhir_version"):
        _registry(_profile(fhir_version=[]))


def test_an_empty_scope_list_refuses():
    with pytest.raises(ConnectorConfigError, match="scopes"):
        auth = dict(_profile()["auth"])
        auth["scopes"] = []
        _registry(_profile(auth=auth))


def test_an_unknown_auth_mode_refuses():
    auth = dict(_profile()["auth"])
    auth["mode"] = "client-secret"
    with pytest.raises(ConnectorConfigError, match="mode"):
        _registry(_profile(auth=auth))


def test_an_unknown_algorithm_refuses():
    auth = dict(_profile()["auth"])
    auth["algorithm"] = "HS256"
    with pytest.raises(ConnectorConfigError, match="algorithm"):
        _registry(_profile(auth=auth))


def test_pem_content_pasted_where_a_path_belongs_refuses():
    """A configuration file gets committed, pasted into tickets, and read by everyone with repo
    access. The signing key is the whole proof of our identity to the remote."""
    auth = dict(_profile()["auth"])
    auth["private_key_file"] = "-----BEGIN PRIVATE KEY-----\nMIIEvQ...\n-----END PRIVATE KEY-----"
    with pytest.raises(ConnectorConfigError, match="path"):
        _registry(_profile(auth=auth))


def test_two_connectors_may_not_share_an_id():
    with pytest.raises(ConnectorConfigError, match="duplicate"):
        _registry(_profile(), _profile())


def test_an_empty_connector_list_refuses():
    with pytest.raises(ConnectorConfigError, match="connectors"):
        ConnectorRegistry.from_mapping({"connectors": []})


def test_allow_plaintext_without_hosts_refuses():
    """Two independent statements, so neither is reachable by a typo in the other -- the same
    construction peers.py uses for its plaintext listener."""
    with pytest.raises(ConnectorConfigError, match="plaintext_hosts"):
        _registry(_profile(), allow_plaintext=True)


def test_plaintext_hosts_without_the_flag_refuses():
    with pytest.raises(ConnectorConfigError, match="allow_plaintext"):
        _registry(_profile(), plaintext_hosts=["fhir.local"])


def test_allow_plaintext_with_hosts_permits_an_http_url():
    reg = _registry(
        _profile(
            fhir_base_url="http://fhir.local/api/FHIR/R4",
            token_url="http://fhir.local/oauth2/token",
        ),
        allow_plaintext=True,
        plaintext_hosts=["fhir.local"],
    )
    assert reg.allow_plaintext
    assert reg.plaintext_hosts == frozenset({"fhir.local"})


def test_plaintext_hosts_are_lowercased():
    """A host is compared against a URL's parsed hostname, which urlsplit lowercases."""
    reg = _registry(
        _profile(
            fhir_base_url="http://fhir.local/api/FHIR/R4",
            token_url="http://fhir.local/oauth2/token",
        ),
        allow_plaintext=True,
        plaintext_hosts=["FHIR.LOCAL"],
    )
    assert reg.plaintext_hosts == frozenset({"fhir.local"})


def test_load_reads_a_file(tmp_path: Path):
    path = tmp_path / "connectors.json"
    path.write_text(json.dumps({"connectors": [_profile()]}), encoding="utf-8")
    assert load_connector_registry(path).connector_ids() == ("example-med",)


def test_load_refuses_a_missing_file(tmp_path: Path):
    with pytest.raises(ConnectorConfigError, match="not found"):
        load_connector_registry(tmp_path / "absent.json")


def test_load_refuses_malformed_json(tmp_path: Path):
    path = tmp_path / "connectors.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConnectorConfigError, match="JSON"):
        load_connector_registry(path)


def test_the_connector_id_pattern_has_not_drifted_from_the_peer_id_pattern():
    """Two modules, one constraint, and connectors.py says so in a comment. peers.py is not
    modified by this sub-project, so the pattern cannot be shared without touching it -- this
    asserts the equality instead, so tightening one side cannot silently leave the other behind."""
    from referral_loop.connect.connectors import _CONNECTOR_ID_RE
    from referral_loop.peers import _PEER_ID_RE

    assert _CONNECTOR_ID_RE.pattern == _PEER_ID_RE.pattern


def test_authorities_must_be_present_even_when_empty():
    broken = _profile()
    del broken["authorities"]
    with pytest.raises(ConnectorConfigError, match="authorities"):
        _registry(broken)


def test_a_ca_file_is_read_when_given():
    reg = _registry(_profile(tls={"ca_file": "/etc/referral/example-med-ca.crt"}))
    assert reg.get("example-med").ca_file == Path("/etc/referral/example-med-ca.crt")


def test_no_tls_block_means_the_system_trust_store():
    assert _registry().get("example-med").ca_file is None


def test_a_malformed_tls_block_refuses():
    with pytest.raises(ConnectorConfigError, match="tls"):
        _registry(_profile(tls="/etc/ca.crt"))


def test_an_endpoint_makes_the_default_port_explicit():
    assert endpoint_of("https://fhir.example-med.example/api") == ("https", "fhir.example-med.example", 443)


def test_an_explicit_default_port_normalises_to_the_same_endpoint():
    """The allowlist compares these triples. A string comparison would let a port-explicit
    spelling of an allowed host read as a different destination."""
    assert endpoint_of("https://h/a") == endpoint_of("https://h:443/b")


def test_a_nondefault_port_is_a_different_endpoint():
    assert endpoint_of("https://h:8443/a") != endpoint_of("https://h/a")


def test_a_host_is_lowercased_in_an_endpoint():
    assert endpoint_of("https://FHIR.EXAMPLE-MED.EXAMPLE/api")[1] == "fhir.example-med.example"


def test_the_allowlist_covers_both_the_api_and_the_token_host():
    reg = _registry(
        _profile(
            fhir_base_url="https://fhir.example-med.example/api/FHIR/R4",
            token_url="https://auth.example-med.example/oauth2/token",
        )
    )
    assert reg.endpoints() == frozenset(
        {
            ("https", "fhir.example-med.example", 443),
            ("https", "auth.example-med.example", 443),
        }
    )


@pytest.mark.parametrize("reserved", sorted(RESERVED_PEER_IDS))
def test_a_reserved_peer_id_is_refused_as_a_connector_id(reserved):
    """The two namespaces are separate files, which does not mean they may overlap. These five
    already carry meanings in audit rows; 'coordinator' means no message asserted the transition
    at all, so a connector able to claim it could attribute its own action to a human."""
    with pytest.raises(ConnectorConfigError, match="reserved"):
        _registry(_profile(connector_id=reserved))


def test_an_id_shared_with_a_configured_peer_warns_but_loads(caplog):
    """One organization on both ends of the relationship is the natural case, and the two files
    are read by different code paths -- so the ambiguity is only in the reader's head. But audit
    rows from the two directions will sit next to each other."""
    reg = _registry(_profile(connector_id="example-ris"))
    with caplog.at_level("WARNING"):
        collisions = reg.warn_on_peer_collisions(("example-ris", "example-lab"))
    assert collisions == ("example-ris",)
    assert "example-ris" in caplog.text


def test_no_collision_is_silent(caplog):
    reg = _registry()
    with caplog.at_level("WARNING"):
        assert reg.warn_on_peer_collisions(("example-ris",)) == ()
    assert caplog.text == ""


def test_an_unknown_scheme_is_a_typed_refusal_not_a_keyerror():
    """check_allowed calls endpoint_of on whatever URL it is handed. A KeyError there escapes
    every except clause in fetch, so a hostile redirect target would crash the process instead
    of being refused."""
    with pytest.raises(ConnectorConfigError, match="scheme"):
        endpoint_of("ftp://evil.example/pub")


def test_a_scheme_with_an_explicit_port_is_still_refused_if_unknown():
    """The explicit port would sidestep the dict lookup entirely, so the scheme has to be
    checked on its own rather than only where the default port is needed."""
    with pytest.raises(ConnectorConfigError, match="scheme"):
        endpoint_of("ftp://evil.example:21/pub")


def test_a_connector_without_identifier_systems_is_not_queryable():
    """Absent is allowed -- every connector A shipped is preflight-only -- but it must be
    visible as an incapability rather than discovered by a query that returns nothing."""
    assert not _registry().get("example-med").is_queryable


def test_a_declared_mrn_system_makes_a_connector_queryable():
    reg = _registry(_profile(identifier_systems={"mrn": "urn:oid:1.2.840.114350.1.13.99"}))
    ku = reg.get("example-med")
    assert ku.is_queryable
    assert ku.mrn_system == "urn:oid:1.2.840.114350.1.13.99"


def test_a_malformed_identifier_systems_block_refuses():
    with pytest.raises(ConnectorConfigError, match="identifier_systems"):
        _registry(_profile(identifier_systems="urn:oid:1.2.3"))


def test_an_empty_mrn_system_refuses_rather_than_reading_as_absent():
    """An empty string is a typo, not a declaration. Treating it as absent would turn a broken
    config into a silently preflight-only connector."""
    with pytest.raises(ConnectorConfigError, match="mrn"):
        _registry(_profile(identifier_systems={"mrn": "  "}))


def test_an_unknown_identifier_kind_refuses():
    """Only mrn is consumed today. An unrecognised key is a typo that would otherwise sit in
    the file looking like it did something."""
    with pytest.raises(ConnectorConfigError, match="ssn|unknown|identifier"):
        _registry(_profile(identifier_systems={"ssn": "urn:oid:2.16.840.1.113883.4.1"}))
