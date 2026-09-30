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
* Two secret classes, two KMS keys (REVIEW_TRADING_KEYS F2 / SECURITY §3.4): agent private keys are wrapped under
  ``agent-keys`` (``settings.kms_key_name``, KMS-level AAD ``AGENT_KEY_KMS_AAD``) by ``make_encryptor`` /
  ``make_decryptor``; creator strategy code under the dedicated ``creator-code`` key
  (``settings.creator_code_kms_key_name``, KMS-level AAD ``CREATOR_CODE_KMS_AAD``) by ``make_code_encryptor`` /
  ``make_code_decryptor``. Each key has its own IAM (api encrypt-only, executor decrypt-only), rotation and blast
  radius; the per-class wrap AAD means a DEK wrapped for one class cannot be unwrapped as the other even if a key
  name were misconfigured, and in dev (one local KEK) the AAD alone separates them. Creator code record AAD =
  ``creator_code_aad(strategy_id, code_hash)`` (namespace ``aijalon/creator_code/v1``).
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
    "AGENT_KEY_KMS_AAD", "CREATOR_CODE_KMS_AAD", "creator_code_aad", "make_code_encryptor", "make_code_decryptor",
    "FORMAT_VERSION", "WRAPPER_KIND_CLOUD_KMS", "WRAPPER_KIND_LOCAL_DEV",
    "DecryptionFailed", "KeyWrapper", "CloudKmsKeyWrapper", "LocalAesKeyWrapper",
    "SealedBlob", "EnvelopeEncryptor", "EnvelopeDecryptor", "make_encryptor", "make_decryptor",
    "crc32c", "zeroize",
]

AGENT_KEY_KMS_AAD = b"aijalon-agent-key-v1"   # AAD on the KMS wrap/unwrap call (agent-keys)
CREATOR_CODE_KMS_AAD = b"aijalon-creator-code-v1"   # AAD on the KMS wrap/unwrap call (creator-code)
_CREATOR_CODE_RECORD_NS = "aijalon/creator_code/v1"
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
    def from_settings(cls, settings: Any, *, allow_unwrap: bool = False,
                      aad: bytes = AGENT_KEY_KMS_AAD) -> "LocalAesKeyWrapper":
        if settings.is_prod:
            raise RuntimeError("LocalAesKeyWrapper must never be used in prod")
        raw = getattr(settings, "local_dev_kek_b64", "") or ""
        if not raw:
            raise RuntimeError("LOCAL_DEV_KEK_B64 is not set (generate: python -c 'import os,base64;"
                               "print(base64.b64encode(os.urandom(32)).decode())')")
        kek = bytearray(base64.b64decode(raw, validate=True))
        try:
            return cls(kek, is_prod=settings.is_prod, allow_unwrap=allow_unwrap, aad=aad)
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


def creator_code_aad(strategy_id: str, code_hash: str) -> bytes:
    """Record AAD for creator strategy code (API seals, executor opens with exactly this)."""
    return f"{_CREATOR_CODE_RECORD_NS}\x00strategy_code:{strategy_id}:{code_hash}".encode()


def _build_wrapper(s: Any, *, allow_unwrap: bool, kms_client: Any, key_name: str | None = None,
                   aad: bytes = AGENT_KEY_KMS_AAD, env_name: str = "KMS_KEY_NAME") -> KeyWrapper:
    name = s.kms_key_name if key_name is None else key_name
    if name:
        return CloudKmsKeyWrapper(name, client=kms_client, allow_unwrap=allow_unwrap, require_hsm=s.is_prod, aad=aad)
    if s.is_prod:
        raise RuntimeError(f"{env_name} is required in prod")
    # one dev KEK, but a per-class wrap AAD: agent-key and code blobs never cross-open
    return LocalAesKeyWrapper.from_settings(s, allow_unwrap=allow_unwrap, aad=aad)


def _code_key_name(s: Any) -> str:
    name = getattr(s, "creator_code_kms_key_name", "") or ""
    if name and name == (s.kms_key_name or ""):
        raise RuntimeError("the creator-code KMS key must differ from the agent-keys KMS key")
    return name


def make_code_encryptor(settings: Any = None, *, kms_client: Any = None) -> EnvelopeEncryptor:
    """Creator strategy code (API upload): encrypt-only envelope under the dedicated creator-code KMS key."""
    s = _settings(settings)
    return EnvelopeEncryptor(_build_wrapper(s, allow_unwrap=False, kms_client=kms_client, key_name=_code_key_name(s),
                                            aad=CREATOR_CODE_KMS_AAD, env_name="CREATOR_CODE_KMS_KEY_NAME"))


def make_code_decryptor(settings: Any = None, *, kms_client: Any = None) -> EnvelopeDecryptor:
    """Creator strategy code (executor ONLY, same role rule as ``make_decryptor``)."""
    s = _settings(settings)
    _require_decrypt_role(s)
    return EnvelopeDecryptor(_build_wrapper(s, allow_unwrap=True, kms_client=kms_client, key_name=_code_key_name(s),
                                            aad=CREATOR_CODE_KMS_AAD, env_name="CREATOR_CODE_KMS_KEY_NAME"))


def _require_decrypt_role(s: Any) -> None:
    role = getattr(s, "service_role", None)
    allowed = _DECRYPT_ROLES_PROD if s.is_prod else _DECRYPT_ROLES_NONPROD
    if role not in allowed:
        raise Forbidden("secret decryption is only available to the executor service", service_role=role)


def make_encryptor(settings: Any = None, *, kms_client: Any = None) -> EnvelopeEncryptor:
    """For the API (and anything that only stores secrets)."""
    s = _settings(settings)
    return EnvelopeEncryptor(_build_wrapper(s, allow_unwrap=False, kms_client=kms_client))


def make_decryptor(settings: Any = None, *, kms_client: Any = None) -> EnvelopeDecryptor:
    """For the executor ONLY. Requires settings.service_role == "executor" ("all" allowed outside prod)."""
    s = _settings(settings)
    _require_decrypt_role(s)
    return EnvelopeDecryptor(_build_wrapper(s, allow_unwrap=True, kms_client=kms_client))


# ================================================================ agent attestation signer (SECURITY H1)
# The EXECUTOR (the only role that can decrypt agent keys) proves to the browser that an agent address is really the
# sealed key it will trade with: after opening the sealed key and re-deriving its address, it signs
#     aijalon-agent-v1|{user_id}|{agent_address}
# with a Cloud KMS ASYMMETRIC key (EC_SIGN_P256_SHA256, HSM) on which only the executor SA holds
# roles/cloudkms.signer. The api SA has NO role on it, so a compromised api (or edge) cannot mint attestations.
# The browser verifies with WebCrypto against the public key pinned in web/public/app-config.json.
import os as _os  # noqa: E402 - additive section

from cryptography.exceptions import InvalidSignature  # noqa: E402
from cryptography.hazmat.primitives import hashes as _hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec as _ec  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding as _Encoding,
    PublicFormat as _PublicFormat,
    load_der_public_key as _load_der_public_key,
    load_pem_public_key as _load_pem_public_key,
)

AGENT_ATTEST_PREFIX = "aijalon-agent-v1"
_EC_SIGN_P256_SHA256 = 12          # google.cloud.kms.CryptoKeyVersion.CryptoKeyVersionAlgorithm.EC_SIGN_P256_SHA256
_KMS_KEY_VERSION_RE = re.compile(r"^projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/]+/cryptoKeyVersions/\d+$")
_ATTEST_ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")
_ATTEST_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

__all__ += ["AGENT_ATTEST_PREFIX", "agent_attestation_message", "AttestationSigner", "CloudKmsAttestationSigner",
            "LocalAttestationSigner", "make_attestation_signer", "verify_p256_signature"]


def agent_attestation_message(user_id: str, agent_address: str) -> bytes:
    """The exact bytes the executor signs and the browser (web/src/core/attest.ts) re-builds."""
    uid, addr = str(user_id).lower(), str(agent_address).lower()
    if not _ATTEST_UUID_RE.match(uid):
        raise ValueError("user_id must be a uuid")
    if not _ATTEST_ADDR_RE.match(addr):
        raise ValueError("agent_address must be a 0x-prefixed 20-byte hex address")
    return f"{AGENT_ATTEST_PREFIX}|{uid}|{addr}".encode("ascii")


def verify_p256_signature(public_key_spki_der: bytes, message: bytes, der_signature: bytes) -> bool:
    """ECDSA P-256 / SHA-256 verification (DER signature, SPKI DER public key). Never raises."""
    try:
        key = _load_der_public_key(bytes(public_key_spki_der))
        if not isinstance(key, _ec.EllipticCurvePublicKey) or not isinstance(key.curve, _ec.SECP256R1):
            return False
        key.verify(bytes(der_signature), bytes(message), _ec.ECDSA(_hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


class AttestationSigner(ABC):
    """Signs attestation messages; ``key_version`` identifies the key (stored next to each signature)."""

    key_version: str

    @abstractmethod
    def sign(self, message: bytes) -> bytes:
        """DER-encoded ECDSA P-256/SHA-256 signature over ``message``."""

    @abstractmethod
    def public_key_spki_der(self) -> bytes:
        """The verifying key (pinned in web/public/app-config.json as base64)."""


class CloudKmsAttestationSigner(AttestationSigner):
    """Cloud KMS asymmetric signing with Google's integrity checks (CRC32C both ways), the algorithm/protection
    level/key-version checked on the public key, and every signature re-verified locally before it is returned."""

    def __init__(self, key_version_name: str, *, client: Any = None, require_hsm: bool = True,
                 timeout_s: float = 10.0) -> None:
        if not _KMS_KEY_VERSION_RE.match(key_version_name or ""):
            raise ValueError("attestation key must be projects/*/locations/*/keyRings/*/cryptoKeys/*/cryptoKeyVersions/N")
        if client is None:
            if _gkms is None:
                raise RuntimeError("google-cloud-kms is not installed")
            client = _gkms.KeyManagementServiceClient()
        self._client = client
        self.key_version = key_version_name
        self._require_hsm = require_hsm
        self._timeout = timeout_s
        self._spki: bytes | None = None

    def public_key_spki_der(self) -> bytes:
        if self._spki is None:
            try:
                resp = self._client.get_public_key(request={"name": self.key_version}, timeout=self._timeout)
            except Exception as e:  # noqa: BLE001
                raise ExternalServiceError("kms get_public_key failed", service="kms", error=type(e).__name__) from e
            pem = str(resp.pem)
            if int(getattr(resp, "pem_crc32c", crc32c(pem.encode()))) != crc32c(pem.encode()):
                raise ExternalServiceError("kms get_public_key: pem corrupted in transit", service="kms")
            if str(getattr(resp, "name", self.key_version)) != self.key_version:
                raise ExternalServiceError("kms get_public_key: response from unexpected key", service="kms")
            if int(getattr(resp, "algorithm", 0)) != _EC_SIGN_P256_SHA256:
                raise ExternalServiceError("kms attestation key must be EC_SIGN_P256_SHA256", service="kms")
            if self._require_hsm and int(getattr(resp, "protection_level", 0)) != _HSM:
                raise ExternalServiceError("kms attestation key is not HSM-protected", service="kms")
            key = _load_pem_public_key(pem.encode())
            self._spki = key.public_bytes(_Encoding.DER, _PublicFormat.SubjectPublicKeyInfo)
        return self._spki

    def sign(self, message: bytes) -> bytes:
        digest = hashlib.sha256(bytes(message)).digest()
        try:
            resp = self._client.asymmetric_sign(
                request={"name": self.key_version, "digest": {"sha256": digest}, "digest_crc32c": crc32c(digest)},
                timeout=self._timeout)
        except Exception as e:  # noqa: BLE001
            raise ExternalServiceError("kms asymmetric_sign failed", service="kms", error=type(e).__name__) from e
        if not getattr(resp, "verified_digest_crc32c", False):
            raise ExternalServiceError("kms sign: digest crc32c not verified by server", service="kms")
        sig = bytes(resp.signature)
        if int(resp.signature_crc32c) != crc32c(sig):
            raise ExternalServiceError("kms sign: signature corrupted in transit", service="kms")
        if str(getattr(resp, "name", self.key_version)) != self.key_version:
            raise ExternalServiceError("kms sign: response from unexpected key", service="kms")
        if not verify_p256_signature(self.public_key_spki_der(), message, sig):
            raise ExternalServiceError("kms sign: signature does not verify with the key's public key", service="kms")
        return sig

    def __repr__(self) -> str:
        return f"CloudKmsAttestationSigner(key_version={self.key_version!r})"


class LocalAttestationSigner(AttestationSigner):
    """DEV/TEST ONLY: P-256 key in process memory (from LOCAL_DEV_ATTEST_KEY_PEM, else ephemeral). Refused in prod."""

    def __init__(self, private_key: Any = None, *, is_prod: bool) -> None:
        if is_prod:
            raise RuntimeError("LocalAttestationSigner must never be used in prod")
        self._key = private_key or _ec.generate_private_key(_ec.SECP256R1())
        if not isinstance(self._key.curve, _ec.SECP256R1):
            raise ValueError("attestation key must be P-256")
        spki = self.public_key_spki_der()
        self.key_version = "local-dev:" + hashlib.sha256(spki).hexdigest()[:16]

    @classmethod
    def from_env(cls, *, is_prod: bool) -> "LocalAttestationSigner":
        pem = _os.environ.get("LOCAL_DEV_ATTEST_KEY_PEM", "")
        if pem:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            return cls(load_pem_private_key(pem.encode(), password=None), is_prod=is_prod)
        return cls(is_prod=is_prod)

    def public_key_spki_der(self) -> bytes:
        return self._key.public_key().public_bytes(_Encoding.DER, _PublicFormat.SubjectPublicKeyInfo)

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(bytes(message), _ec.ECDSA(_hashes.SHA256()))

    def __repr__(self) -> str:
        return f"LocalAttestationSigner(key_version={self.key_version!r})"


def make_attestation_signer(settings: Any = None, *, kms_client: Any = None) -> AttestationSigner:
    """For the executor ONLY (same role rule as make_decryptor). Key: AGENT_ATTEST_KEY_VERSION (full KMS
    cryptoKeyVersions name; infra/gcp/env.sh KMS_ATTEST_KEY_VERSION_NAME). Required in prod."""
    s = _settings(settings)
    role = getattr(s, "service_role", None)
    allowed = _DECRYPT_ROLES_PROD if s.is_prod else _DECRYPT_ROLES_NONPROD
    if role not in allowed:
        raise Forbidden("agent attestation is only available to the executor service", service_role=role)
    version = str(getattr(s, "agent_attest_key_version", "") or _os.environ.get("AGENT_ATTEST_KEY_VERSION", "") or "")
    if version:
        return CloudKmsAttestationSigner(version, client=kms_client, require_hsm=bool(s.is_prod))
    if s.is_prod:
        raise RuntimeError("AGENT_ATTEST_KEY_VERSION is required in prod")
    return LocalAttestationSigner.from_env(is_prod=False)
