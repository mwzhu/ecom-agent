from __future__ import annotations

from uuid import uuid4

import pytest

from api.security import CredentialCipher

LOCAL_KMS_MASTER_KEY = "1zM4W6j5Pvp3HmJVh97vD-zSwMgg0pBwFK8Z28k7d8Q="


def test_credential_cipher_round_trips_for_same_merchant() -> None:
    merchant_id = uuid4()
    cipher = CredentialCipher(
        master_key=LOCAL_KMS_MASTER_KEY,
        kms_key_id="local-test-cmk",
    )

    encrypted = cipher.encrypt(merchant_id, "shopify-offline-token")

    assert encrypted.kms_key_id == "local-test-cmk"
    assert cipher.decrypt(merchant_id, encrypted.encrypted_value) == "shopify-offline-token"


def test_credential_cipher_rejects_other_merchant() -> None:
    cipher = CredentialCipher(
        master_key=LOCAL_KMS_MASTER_KEY,
        kms_key_id="local-test-cmk",
    )
    encrypted = cipher.encrypt(uuid4(), "stripe-secret")

    with pytest.raises(ValueError, match="Unable to decrypt credential envelope"):
        cipher.decrypt(uuid4(), encrypted.encrypted_value)
