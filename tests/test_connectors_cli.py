"""The connectors mode, and where it sits relative to the boot gates."""

from __future__ import annotations

import json

from referral_loop.cli import MODES, main
from tests._certs import localhost_cert


def _connector_file(tmp_path, connector_id: str = "example-med"):
    key = tmp_path / "k.pem"
    key.write_bytes(b"-----BEGIN PRIVATE KEY-----\nnot a key\n-----END PRIVATE KEY-----\n")
    path = tmp_path / "connectors.json"
    path.write_text(
        json.dumps(
            {
                "connectors": [
                    {
                        "connector_id": connector_id,
                        "organization": "Example Medical Center",
                        "vendor": "epic",
                        "fhir_base_url": "https://unreachable.invalid/api/FHIR/R4",
                        "token_url": "https://unreachable.invalid/oauth2/token",
                        "fhir_version": ["4.0.1"],
                        "auth": {
                            "mode": "smart-backend-services",
                            "client_id": "abc",
                            "private_key_file": str(key),
                            "key_id": "k1",
                            "algorithm": "RS384",
                            "scopes": ["system/Patient.read"],
                        },
                        "authorities": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_connectors_is_a_mode():
    assert "connectors" in MODES


def test_it_runs_without_the_pack_key(tmp_path, monkeypatch, capsys):
    """It joins purge and stats ahead of the pack lookup. An operator whose connector file is
    malformed needs to hear that, not a message about a signing key -- preflight touches no
    database, no pack and no PHI, so none of the three boot gates applies."""
    monkeypatch.delenv("REFERRAL_PACK_PUBKEY", raising=False)
    path = tmp_path / "connectors.json"
    path.write_text("{ not json", encoding="utf-8")

    code = main(["connectors", "--connectors", str(path)])
    assert code == 2
    err = capsys.readouterr().err
    assert "JSON" in err
    assert "REFERRAL_PACK_PUBKEY" not in err


def test_a_missing_file_refuses_with_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("REFERRAL_PACK_PUBKEY", raising=False)
    code = main(["connectors", "--connectors", str(tmp_path / "absent.json")])
    assert code == 2
    assert "not found" in capsys.readouterr().err


def test_a_valid_file_with_an_unreachable_host_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("REFERRAL_PACK_PUBKEY", raising=False)
    path = _connector_file(tmp_path)
    code = main(["connectors", "--connectors", str(path)])
    assert code == 1
    out = capsys.readouterr().out
    assert "example-med" in out
    assert "FAIL" in out


def test_the_peer_cross_check_says_when_it_did_not_run(tmp_path, monkeypatch, capsys):
    """A silent skip would read as a clean bill of health rather than an absence of evidence
    -- the same reasoning the README applies to the image tests that skip without Docker."""
    monkeypatch.delenv("REFERRAL_PACK_PUBKEY", raising=False)
    path = _connector_file(tmp_path)
    main(["connectors", "--connectors", str(path)])
    assert "peer id cross-check: skipped" in capsys.readouterr().out


def test_a_connector_id_shared_with_a_configured_peer_warns(tmp_path, monkeypatch, capsys, caplog):
    """This is the only caller of warn_on_peer_collisions. Without it the whole warning path
    is unreachable and an operator with a real collision never hears about it.

    load_peer_registry validates that tls.certfile/keyfile/client_ca_file are readable files on
    disk (referral_loop.peers.PeerRegistry._tls_files), so the peers.json fixture cannot name
    paths that were never written -- as the plan's literal fixture did. tests/_certs.py's
    localhost_cert helper (the same one test_peer_identity.py's Pki fixture is built from)
    produces a real self-signed cert/key pair on tmp_path, which is reused here rather than a
    second generator that could drift from it.
    """
    monkeypatch.delenv("REFERRAL_PACK_PUBKEY", raising=False)
    path = _connector_file(tmp_path, connector_id="example-ris")
    certfile, keyfile, ca_file = localhost_cert(tmp_path)
    peers = tmp_path / "peers.json"
    peers.write_text(
        json.dumps(
            {
                "transport": "mtls",
                "tls": {
                    "certfile": str(certfile),
                    "keyfile": str(keyfile),
                    "client_ca_file": str(ca_file),
                },
                "peers": [
                    {
                        "peer_id": "example-ris",
                        "organization": "Example Radiology",
                        "certificate_sha256": ["a" * 64],
                        "authorities": ["result"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        main(["connectors", "--connectors", str(path), "--peers", str(peers)])
    assert "example-ris" in caplog.text
    assert "peer id cross-check: skipped" not in capsys.readouterr().out
