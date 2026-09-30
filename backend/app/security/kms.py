"""Envelope encryption for secrets at rest (agent private keys; reusable for other small secrets).

Design (docs/SPEC.md §2, §2.1, §5.3)
------------------------------------
* Every record gets a fresh random 256-bit data-encryption key (DEK). The record plaintext is sealed with
  AES-256-GCM under the DEK, with the record binding (e.g. user id + agent address) as additional authenticated
  data, so a ciphertext copied onto another row fails to decrypt.
* The DEK is wrapped by a key-encryption key (KEK):
    - prod: Cloud KMS key ``agent-keys`` (HSM protection) via ``CloudKmsKeyWrapper``;
    - dev/test only: a local AES-256-GCM KEK from ``settings.local_dev_kek_b64`` via ``LocalAesKeyWrapper``,
      which refuses to construct when ``settings.is_prod``.
* Segregation mirrors IAM (§2.1): the ``api`` service holds only ``cloudkms.cryptoKeyVersions.useToEncrypt``
  and the ``executor`` only ``useToDecrypt``. In code, the API builds an ``EnvelopeEncryptor`` (no decrypt
  method at all) over a wrapper constructed with ``allow_unwrap=False`` (``unwrap`` raises ``Forbidden``).
  ``EnvelopeDecryptor`` requires a wrapper built with ``allow_unwrap=True``, and ``make_decryptor`` refuses
  unless the process runs as the executor. IAM is the real control; the code split stops accidental use.

Blob format v1 (all integers big-endian)
----------------------------------------
    offset   size  field
    0        1     format version = 0x01
    1        1     wrapper kind: 0x01 = Cloud KMS, 0x02 = local dev AES (refused by a KMS-backed decryptor)
    2        2     L = length of wrapped DEK (1..1024)
    4        L     wrapped DEK (Cloud KMS ciphertext, or local: nonce(12) || AES-GCM(KEK, DEK) || tag(16))
    4+L      12    data nonce (random 96-bit)
    16+L     N     AES-256-GCM(DEK, nonce, plaintext, aad=DATA_AAD) ciphertext || 16-byte tag  (N >= 16)

    DATA_AAD = b"aijalon-envelope-v1\\x00" || u32(len(H)) || H || u32(len(R)) || R
      where H = bytes[0 : 4+L] (version, kind, L, wrapped DEK) and R = caller's record binding.

The KMS key *version* used for wrapping is returned separately (``SealedBlob.key_version``) and stored in its
own column (``agent_keys.kms_key_version``); Cloud KMS ciphertext identifies its version internally, so it is
informational (rotation tracking), not needed to decrypt.

Memory hygiene: Python cannot guarantee secrets are wiped (immutable ``bytes`` copies are made by the KMS
client and by ``cryptography``). We minimise lifetime: DEKs and plaintexts we own are ``bytearray`` and are
zeroised with ``zeroize`` after use.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.errors import AppError, ExternalServiceError, Forbidden

try:  # prod only; import-guarded so dev/test run without Google libraries
    from google.cloud import kms as _gkms  # type: ignore
except Exception:  # noqa: BLE001
    _gkms = None

__all__ = [
    "AGENT_KEY_KMS_AAD", "FORMAT_VERSION", "WRAPPER_KIND_CLOUD_KMS", "WRAPPER_KIND_LOCAL_DEV",
    "DecryptionFailed", "KeyWrapper", "CloudKmsKeyWrapper", "LocalAesKeyWrapper",
    "SealedBlob", "EnvelopeEncryptor", "EnvelopeDecryptor", "make_encryptor", "make_decryptor",
    "crc32c", "zeroize",
]

AGENT_KEY_KMS_AAD = b"aijalon-agent-key-v1"   # AAD on the KMS wrap/unwrap call
FORMAT_VERSION = 0x01
WRAPPER_KIND_CLOUD_KMS = 0x01
WRAPPER_KIND_LOCAL_DEV = 0x02
_DATA_AAD_PREFIX = b"aijalon-envelope-v1\x00"
_NONCE_LEN = 12
_TAG_LEN = 16
_DEK_LEN = 32
_MAX_WRAPPED_LEN = 1024
_MAX_PLAINTEXT_LEN = 1 << 20
_HSM = 2  # google.cloud.kms.ProtectionLevel.HSM
_KMS_KEY_NAME_RE = re.compile(r"^projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/]+$")
# Roles allowed to build a decryptor. "all" = single-process local dev (refused in prod).
_DECRYPT_ROLES_PROD = frozenset({"executor"})
_DECRYPT_ROLES_NONPROD = frozenset({"executor", "all"})


class DecryptionFailed(AppError):
    """Ciphertext malformed, tampered, bound to another record, or wrapped by the wrong KEK."""
    http_status, code = 500, "decryption_failed"


def zeroize(buf: bytearray | memoryview | None) -> None:
    """Overwrite a mutable buffer in place (same-length slice assignment does not reallocate)."""
    if buf is None:
        return
    if isinstance(buf, memoryview):
        buf[:] = b"\x00" * buf.nbytes
    else:
        buf[:] = b"\x00" * len(buf)


# ---------------------------------------------------------------- CRC32C (Castagnoli), as Google KMS requires
def _make_crc32c_table() -> tuple[int, ...]:
    poly = 0x82F63B78
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ poly if c & 1 else c >> 1
        table.append(c)
    return tuple(table)


_CRC_TABLE = _make_crc32c_table()


def _crc32c_pure(data: bytes | bytearray) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = _CRC_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def crc32c(data: bytes | bytearray) -> int:
    """CRC32C as an int, as used for Cloud KMS request/response integrity fields."""
    try:
        import google_crc32c  # type: ignore
        return int(google_crc32c.value(bytes(data)))
    except Exception:  # noqa: BLE001 - fall back to pure Python
        return _crc32c_pure(data)


# ---------------------------------------------------------------- key wrappers (KEK)
class KeyWrapper(ABC):
    """Wraps/unwraps a DEK under a KEK. ``unwrap`` must raise ``Forbidden`` unless built with allow_unwrap=True."""

    kind: int

    def __init__(self, *, allow_unwrap: bool) -> None:
        self._allow_unwrap = bool(allow_unwrap)

    @property
    def can_unwrap(self) -> bool:
        return self._allow_unwrap

    @abstractmethod
    def wrap(self, dek: bytes | bytearray) -> tuple[bytes, str]:
        """Return (wrapped_dek, key_version)."""

    @abstractmethod
    def unwrap(self, wrapped: bytes) -> bytearray:
        """Return the DEK as a bytearray (caller zeroises)."""

    def _check_unwrap_allowed(self) -> None:
        if not self._allow_unwrap:
            raise Forbidden("key unwrap is not permitted in this process")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(allow_unwrap={self._allow_unwrap})"


class CloudKmsKeyWrapper(KeyWrapper):
    """Cloud KMS symmetric key (HSM). Follows Google's data-integrity guidance: CRC32C on every request field
    and verification of every response checksum; also verifies the key used and its protection level."""

    kind = WRAPPER_KIND_CLOUD_KMS

    def __init__(self, key_name: str, *, client: Any = None, allow_unwrap: bool = False,
                 require_hsm: bool = True, timeout_s: float = 10.0, aad: bytes = AGENT_KEY_KMS_AAD) -> None:
        super().__init__(allow_unwrap=allow_unwrap)
        if not _KMS_KEY_NAME_RE.match(key_name or ""):
            raise ValueError("kms key name must be projects/*/locations/*/keyRings/*/cryptoKeys/* (no version)")
        if client is None:
            if _gkms is None:
                raise RuntimeError("google-cloud-kms is not installed")
            client = _gkms.KeyManagementServiceClient()
        self._client = client
        self._key_name = key_name
        self._require_hsm = require_hsm
        self._timeout = timeout_s
        self._aad = bytes(aad)
        self._aad_crc = crc32c(self._aad)

    def wrap(self, dek: bytes | bytearray) -> tuple[bytes, str]:
        if len(dek) != _DEK_LEN:
            raise ValueError("DEK must be 32 bytes")
        pt = bytes(dek)
        try:
            resp = self._client.encrypt(
                request={
                    "name": self._key_name,
                    "plaintext": pt,
                    "plaintext_crc32c": crc32c(pt),
                    "additional_authenticated_data": self._aad,
                    "additional_authenticated_data_crc32c": self._aad_crc,
                },
                timeout=self._timeout,
            )
        except Exception as e:  # noqa: BLE001 - google.api_core errors; message has no secrets
            raise ExternalServiceError("kms encrypt failed", service="kms", error=type(e).__name__) from e
        if not getattr(resp, "verified_plaintext_crc32c", False):
            raise ExternalServiceError("kms encrypt: plaintext crc32c not verified by server", service="kms")
        if not getattr(resp, "verified_additional_authenticated_data_crc32c", False):
            raise ExternalServiceError("kms encrypt: aad crc32c not verified by server", service="kms")
        ciphertext = bytes(resp.ciphertext)
        if int(resp.ciphertext_crc32c) != crc32c(ciphertext):
            raise ExternalServiceError("kms encrypt: response ciphertext corrupted in transit", service="kms")
        version = str(resp.name)
        if not version.startswith(self._key_name + "/cryptoKeyVersions/"):
            raise ExternalServiceError("kms encrypt: response from unexpected key", service="kms")
        if self._require_hsm and int(getattr(resp, "protection_level", 0)) != _HSM:
            raise ExternalServiceError("kms encrypt: key is not HSM-protected", service="kms")
        if not 0 < len(ciphertext) <= _MAX_WRAPPED_LEN:
            raise ExternalServiceError("kms encrypt: unexpected ciphertext size", service="kms")
        return ciphertext, version

    def unwrap(self, wrapped: bytes) -> bytearray:
        self._check_unwrap_allowed()
        wrapped = bytes(wrapped)
        try:
            resp = self._client.decrypt(
                request={
                    "name": self._key_name,
                    "ciphertext": wrapped,
                    "ciphertext_crc32c": crc32c(wrapped),
                    "additional_authenticated_data": self._aad,
                    "additional_authenticated_data_crc32c": self._aad_crc,
                },
                timeout=self._timeout,
            )
        except Exception as e:  # noqa: BLE001
            raise ExternalServiceError("kms decrypt failed", service="kms", error=type(e).__name__) from e
        dek = bytearray(resp.plaintext)
        if int(resp.plaintext_crc32c) != crc32c(dek):
            zeroize(dek)
            raise ExternalServiceError("kms decrypt: response plaintext corrupted in transit", service="kms")
        if self._require_hsm and int(getattr(resp, "protection_level", 0)) != _HSM:
            zeroize(dek)
            raise ExternalServiceError("kms decrypt: key is not HSM-protected", service="kms")
        if len(dek) != _DEK_LEN:
            zeroize(dek)
            raise DecryptionFailed("unwrapped DEK has wrong length")
        return dek

    def __repr__(self) -> str:
        return f"CloudKmsKeyWrapper(key={self._key_name!r}, allow_unwrap={self._allow_unwrap})"


class LocalAesKeyWrapper(KeyWrapper):
    """DEV/TEST ONLY. AES-256-GCM KEK held in process memory. Refuses to construct in prod."""

    kind = WRAPPER_KIND_LOCAL_DEV

    def __init__(self, kek: bytes | bytearray, *, is_prod: bool, allow_unwrap: bool = False,
                 aad: bytes = AGENT_KEY_KMS_AAD) -> None:
        if is_prod:
            raise RuntimeError("LocalAesKeyWrapper must never be used in prod")
        super().__init__(allow_unwrap=allow_unwrap)
        if len(kek) != 32:
            raise ValueError("local dev KEK must be 32 bytes (base64 of 32 random bytes)")
        self._aead = AESGCM(bytes(kek))
        self._aad = bytes(aad)
        self._version = "local-dev:" + hashlib.sha256(b"aijalon-kek-fingerprint\x00" + bytes(kek)).hexdigest()[:16]

    @classmethod
    def from_settings(cls, settings: Any, *, allow_unwrap: bool = False) -> "LocalAesKeyWrapper":
        if settings.is_prod:
            raise RuntimeError("LocalAesKeyWrapper must never be used in prod")
        raw = getattr(settings, "local_dev_kek_b64", "") or ""
        if not raw:
            raise RuntimeError("LOCAL_DEV_KEK_B64 is not set (generate: python -c 'import os,base64;"
                               "print(base64.b64encode(os.urandom(32)).decode())')")
        kek = bytearray(base64.b64decode(raw, validate=True))
        try:
            return cls(kek, is_prod=settings.is_prod, allow_unwrap=allow_unwrap)
        finally:
            zeroize(kek)

    def wrap(self, dek: bytes | bytearray) -> tuple[bytes, str]:
        if len(dek) != _DEK_LEN:
            raise ValueError("DEK must be 32 bytes")
        nonce = secrets.token_bytes(_NONCE_LEN)
        return nonce + self._aead.encrypt(nonce, bytes(dek), self._aad), self._version

    def unwrap(self, wrapped: bytes) -> bytearray:
        self._check_unwrap_allowed()
        if len(wrapped) != _NONCE_LEN + _DEK_LEN + _TAG_LEN:
            raise DecryptionFailed("wrapped DEK has wrong length")
        try:
            return bytearray(self._aead.decrypt(wrapped[:_NONCE_LEN], wrapped[_NONCE_LEN:], self._aad))
        except InvalidTag as e:
            raise DecryptionFailed("wrapped DEK failed authentication (wrong KEK?)") from e

    def __repr__(self) -> str:
        return f"LocalAesKeyWrapper(version={self._version!r}, allow_unwrap={self._allow_unwrap})"


# ---------------------------------------------------------------- envelope
@dataclass(frozen=True)
class SealedBlob:
    blob: bytes
    key_version: str

    def __repr__(self) -> str:
        return f"SealedBlob(len={len(self.blob)}, key_version={self.key_version!r})"


def _data_aad(header: bytes, record_aad: bytes) -> bytes:
    return _DATA_AAD_PREFIX + struct.pack(">I", len(header)) + header + struct.pack(">I", len(record_aad)) + record_aad


class EnvelopeEncryptor:
    """Encrypt-only. Deliberately has no decrypt method (the API process builds only this)."""

    def __init__(self, wrapper: KeyWrapper) -> None:
        self._wrapper = wrapper

    def seal(self, plaintext: bytes | bytearray, record_aad: bytes) -> SealedBlob:
        if not record_aad:
            raise ValueError("record_aad (record binding) is required")
        if not 0 < len(plaintext) <= _MAX_PLAINTEXT_LEN:
            raise ValueError("plaintext size out of range")
        dek = bytearray(secrets.token_bytes(_DEK_LEN))
        try:
            wrapped, key_version = self._wrapper.wrap(dek)
            if not 0 < len(wrapped) <= _MAX_WRAPPED_LEN:
                raise ValueError("wrapped DEK size out of range")
            header = struct.pack(">BBH", FORMAT_VERSION, self._wrapper.kind, len(wrapped)) + wrapped
            nonce = secrets.token_bytes(_NONCE_LEN)
            ct = AESGCM(dek).encrypt(nonce, bytes(plaintext), _data_aad(header, bytes(record_aad)))
        finally:
            zeroize(dek)
        return SealedBlob(blob=header + nonce + ct, key_version=key_version)

    def __repr__(self) -> str:
        return f"EnvelopeEncryptor({self._wrapper!r})"


class EnvelopeDecryptor:
    """Decrypt-capable. Only the executor constructs this (see make_decryptor)."""

    def __init__(self, wrapper: KeyWrapper) -> None:
        if not wrapper.can_unwrap:
            raise Forbidden("EnvelopeDecryptor requires a wrapper built with allow_unwrap=True")
        self._wrapper = wrapper

    def open(self, blob: bytes, record_aad: bytes) -> bytearray:
        """Return plaintext as a bytearray; the caller must zeroize() it after use."""
        blob = bytes(blob)
        if len(blob) < 4:
            raise DecryptionFailed("blob too short")
        version, kind, wlen = struct.unpack(">BBH", blob[:4])
        if version != FORMAT_VERSION:
            raise DecryptionFailed("unsupported envelope format version", version=version)
        if kind != self._wrapper.kind:
            raise DecryptionFailed("blob was wrapped by a different KEK type", kind=kind)
        if not 0 < wlen <= _MAX_WRAPPED_LEN or len(blob) < 4 + wlen + _NONCE_LEN + _TAG_LEN:
            raise DecryptionFailed("blob truncated or malformed")
        header = blob[:4 + wlen]
        nonce = blob[4 + wlen:4 + wlen + _NONCE_LEN]
        ct = blob[4 + wlen + _NONCE_LEN:]
        dek = self._wrapper.unwrap(header[4:])
        try:
            return bytearray(AESGCM(dek).decrypt(nonce, ct, _data_aad(header, bytes(record_aad))))
        except InvalidTag as e:
            raise DecryptionFailed("ciphertext failed authentication (tampered or bound to another record)") from e
        finally:
            zeroize(dek)

    def __repr__(self) -> str:
        return f"EnvelopeDecryptor({self._wrapper!r})"


# ---------------------------------------------------------------- factories
def _settings(settings: Any) -> Any:
    if settings is not None:
        return settings
    from app.config import get_settings
    return get_settings()


def _build_wrapper(s: Any, *, allow_unwrap: bool, kms_client: Any) -> KeyWrapper:
    if s.kms_key_name:
        return CloudKmsKeyWrapper(s.kms_key_name, client=kms_client, allow_unwrap=allow_unwrap, require_hsm=s.is_prod)
    if s.is_prod:
        raise RuntimeError("KMS_KEY_NAME is required in prod")
    return LocalAesKeyWrapper.from_settings(s, allow_unwrap=allow_unwrap)


def make_encryptor(settings: Any = None, *, kms_client: Any = None) -> EnvelopeEncryptor:
    """For the API (and anything that only stores secrets)."""
    s = _settings(settings)
    return EnvelopeEncryptor(_build_wrapper(s, allow_unwrap=False, kms_client=kms_client))


def make_decryptor(settings: Any = None, *, kms_client: Any = None) -> EnvelopeDecryptor:
    """For the executor ONLY. Requires settings.service_role == "executor" ("all" allowed outside prod)."""
    s = _settings(settings)
    role = getattr(s, "service_role", None)
    allowed = _DECRYPT_ROLES_PROD if s.is_prod else _DECRYPT_ROLES_NONPROD
    if role not in allowed:
        raise Forbidden("secret decryption is only available to the executor service", service_role=role)
    return EnvelopeDecryptor(_build_wrapper(s, allow_unwrap=True, kms_client=kms_client))
