"""Sign a referral rule pack. Private key stays outside the repo, always.

Usage:
    python scripts/sign_pack.py --keygen        # once, writes to ~/.config
    python scripts/sign_pack.py --sign          # signs rules/pack.json
    python scripts/sign_pack.py --pubkey        # prints hex for REFERRAL_PACK_PUBKEY
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Named for the repository this package was extracted from, and left that way
# deliberately: this is where the signing key already sits on the machines that
# hold one. Renaming the directory would strand those keys, and a key that
# cannot be found is a pack that cannot be re-signed.
KEY_DIR = Path.home() / ".config" / "healthcare-rag"
PRIVATE_KEY = KEY_DIR / "referral_pack_ed25519.key"
# The extraction moved the pack to src/ and this path did not follow, so --sign
# exited "No pack at ..." rather than signing anything. It went unnoticed because
# signing happens by hand only when a pack changes, which was not for months.
PACK_DIR = Path(__file__).parent.parent / "src" / "referral_loop" / "rules"


def keygen() -> None:
    if PRIVATE_KEY.exists():
        sys.exit(f"Refusing to overwrite existing key at {PRIVATE_KEY}")
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    PRIVATE_KEY.write_bytes(key.private_bytes_raw())
    PRIVATE_KEY.chmod(0o600)
    print(f"Wrote {PRIVATE_KEY}")
    print(f"Public key (hex): {key.public_key().public_bytes_raw().hex()}")


def _load_private() -> Ed25519PrivateKey:
    if not PRIVATE_KEY.exists():
        sys.exit(f"No signing key at {PRIVATE_KEY}. Run --keygen first.")
    return Ed25519PrivateKey.from_private_bytes(PRIVATE_KEY.read_bytes())


def sign() -> None:
    key = _load_private()
    pack_path = PACK_DIR / "pack.json"
    if not pack_path.exists():
        sys.exit(f"No pack at {pack_path}. Nothing to sign.")
    pack_bytes = pack_path.read_bytes()
    (PACK_DIR / "pack.sig").write_bytes(key.sign(pack_bytes))
    print(f"Signed {len(pack_bytes)} bytes -> {PACK_DIR / 'pack.sig'}")


def pubkey() -> None:
    print(_load_private().public_key().public_bytes_raw().hex())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--keygen", action="store_true")
    group.add_argument("--sign", action="store_true")
    group.add_argument("--pubkey", action="store_true")
    args = parser.parse_args()
    if args.keygen:
        keygen()
    elif args.sign:
        sign()
    else:
        pubkey()
