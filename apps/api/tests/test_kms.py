from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from api.config import Settings
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


def test_production_requires_managed_kms() -> None:
    with pytest.raises(ValidationError, match="APP_KMS_PROVIDER=managed"):
        Settings(environment="production", clerk_allow_unverified_jwt=False)


def test_production_accepts_managed_kms_configuration() -> None:
    settings = Settings(
        environment="production",
        app_kms_provider="managed",
        managed_kms_key_id="arn:aws:kms:us-east-1:123456789012:key/test",
        clerk_allow_unverified_jwt=False,
    )

    assert settings.app_kms_provider == "managed"
