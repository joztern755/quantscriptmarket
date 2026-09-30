"""Executor-side secret opening (SPEC §5.3, §5.9). Only the executor service can build these (``make_decryptor``
refuses any other ``service_role`` in prod).

``DbAgentKeyProvider`` (``KeyProvider`` port)
  Loads the ACTIVE agent key of (user_id, master_address) — ``agent_keys.key_ciphertext`` (app_executor has the
  column privilege, app_api does not) — and opens it with ``app.security.agent_keys.opened_agent_key``. The AAD is
  exactly what the API sealed with (``generate_sealed_agent_key(encryptor, user_id=)`` →
  ``agent_key_aad(user_id, agent_address)``), and the decrypted key must re-derive the stored agent address. The
  plaintext is a ``bytearray`` zeroized when the ``with`` block exits (after the orders are signed).

``CreatorCodeDecryptor``
  A DEDICATED decryptor for creator strategy code over the DEDICATED ``creator-code`` KMS key
  (``app.security.kms.make_code_decryptor``; REVIEW_TRADING_KEYS F2) — never the agent-key provider's decryptor or
  key. Record AAD ``app.security.kms.creator_code_aad(strategy_id, code_hash)`` exactly as
  ``app.api.routers.creator.upload_version`` sealed it, and the plaintext must hash to ``code_hash``. Code sealed under
  the old scheme (agent-keys KEK, AAD ``strategy_code:…``) does not open (DecryptionFailed) — re-upload it.
"""
from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from app.errors import NotFound
from app.logging import get_logger
from app.security.agent_keys import SealedKey, opened_agent_key
from app.security.kms import DecryptionFailed, zeroize

from .pg import PgDatabase, as_bytes

__all__ = ["DbAgentKeyProvider", "CreatorCodeDecryptor", "AgentKeyMissing", "creator_code_aad"]

log = get_logger("app.execution.keys")


class AgentKeyMissing(NotFound):
    """No active agent key for (user, master): the subscription cannot trade until the user reconnects."""


class _LazyDecryptor:
    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._dec: Any = None
        self._lock = threading.Lock()

    def get(self) -> Any:
        if self._dec is None:
            with self._lock:
                if self._dec is None:
                    self._dec = self._factory()
        return self._dec


def _default_factory(settings: Any) -> Callable[[], Any]:
    def build() -> Any:
        from app.security.kms import make_decryptor

        return make_decryptor(settings)
    return build


class DbAgentKeyProvider:
    def __init__(self, db: PgDatabase, *, settings: Any = None, decryptor_factory: Callable[[], Any] | None = None) -> None:
        self.db = db
        self._dec = _LazyDecryptor(decryptor_factory or _default_factory(settings))

    def _sealed(self, user_id: str, master_address: str) -> SealedKey:
        row = self.db.one("""
            SELECT key_ciphertext, kms_key_version, agent_address FROM agent_keys
             WHERE user_id = CAST(:u AS uuid) AND master_address = CAST(:m AS text) AND status = 'active'
             ORDER BY approved_at DESC NULLS LAST, created_at DESC
             LIMIT 1""", u=user_id, m=master_address.lower())
        if row is None:
            raise AgentKeyMissing("no active agent key for this wallet", master=master_address[:6] + "…")
        return SealedKey(ciphertext=as_bytes(row["key_ciphertext"]) or b"", key_version=str(row["kms_key_version"]),
                         address=str(row["agent_address"]).lower())

    @contextmanager
    def agent_key(self, user_id: str, master_address: str) -> Iterator[bytearray]:
        sealed = self._sealed(user_id, master_address)
        with opened_agent_key(sealed, self._dec.get(), user_id=user_id) as priv:
            yield priv


def creator_code_aad(strategy_id: str, code_hash: str) -> bytes:
    from app.security.kms import creator_code_aad as _aad

    return _aad(strategy_id, code_hash)


def _code_factory(settings: Any) -> Callable[[], Any]:
    def build() -> Any:
        from app.security.kms import make_code_decryptor

        return make_code_decryptor(settings)
    return build


class CreatorCodeDecryptor:
    def __init__(self, *, settings: Any = None, decryptor_factory: Callable[[], Any] | None = None) -> None:
        self._dec = _LazyDecryptor(decryptor_factory or _code_factory(settings))

    def open_source(self, *, strategy_id: str, code_hash: str, ciphertext: bytes) -> str:
        """Decrypt + verify sha256(plaintext) == code_hash; returns the source text (the bytearray is wiped)."""
        buf = self._dec.get().open(bytes(ciphertext), creator_code_aad(strategy_id, code_hash))
        try:
            if hashlib.sha256(bytes(buf)).hexdigest() != code_hash:
                raise DecryptionFailed("creator code does not match its code_hash")
            return bytes(buf).decode("utf-8")
        finally:
            zeroize(buf)
