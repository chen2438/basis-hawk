from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class EncryptedPayload:
    ciphertext: str
    nonce: str
    key_version: int = 1


class SecretCipher:
    def __init__(self, encoded_key: str, *, key_version: int = 1) -> None:
        try:
            key = base64.urlsafe_b64decode(encoded_key.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("credential master key must be URL-safe base64") from exc
        if len(key) != 32:
            raise ValueError("credential master key must decode to exactly 32 bytes")
        self._cipher = AESGCM(key)
        self.key_version = key_version

    @staticmethod
    def generate_key() -> str:
        return base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode()

    def encrypt(self, value: str, *, associated_data: str) -> EncryptedPayload:
        nonce = secrets.token_bytes(12)
        encrypted = self._cipher.encrypt(
            nonce,
            value.encode(),
            associated_data.encode(),
        )
        return EncryptedPayload(
            ciphertext=base64.urlsafe_b64encode(encrypted).decode(),
            nonce=base64.urlsafe_b64encode(nonce).decode(),
            key_version=self.key_version,
        )

    def decrypt(self, payload: EncryptedPayload, *, associated_data: str) -> str:
        if payload.key_version != self.key_version:
            raise ValueError(f"unsupported credential key version: {payload.key_version}")
        return self._cipher.decrypt(
            base64.urlsafe_b64decode(payload.nonce.encode()),
            base64.urlsafe_b64decode(payload.ciphertext.encode()),
            associated_data.encode(),
        ).decode()

    def encrypt_json(self, value: dict[str, Any], *, associated_data: str) -> EncryptedPayload:
        return self.encrypt(
            json.dumps(value, separators=(",", ":"), sort_keys=True),
            associated_data=associated_data,
        )

    def decrypt_json(
        self, payload: EncryptedPayload, *, associated_data: str
    ) -> dict[str, Any]:
        value = json.loads(self.decrypt(payload, associated_data=associated_data))
        if not isinstance(value, dict):
            raise ValueError("encrypted credential payload must be an object")
        return value
