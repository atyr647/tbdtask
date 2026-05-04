"""Server-side operator key vault (Phase 8b.1).

The *operator key* (also called ``KEK_admin_recovery`` in
``docs/security-architecture.md`` §8) wraps every org's
``KEK_org_master``. Plaintext of the operator key never leaves the
vault; callers see only ``wrap`` / ``unwrap`` / ``operator_key_id``.

Two providers in 8b.1:

* ``LocalKeyVault`` — stores the operator key in a single file with
  mode 0600. Suitable for the AppImage / dev / single-node deploys
  where there is no KMS available.
* ``AWSKMSKeyVault`` and ``GCPKMSKeyVault`` — placeholder stubs that
  raise ``NotImplementedError``. Wired up in 8b.3 once the local path
  is exercised.

All providers implement the same wire format as §6 of the security
spec: ``[version:1][algo:1][key_id:16][iv:12][ciphertext:N][tag:16]``.
The ``key_id`` field embeds an operator-key fingerprint so a future
rotation can verify the vault matches.

AAD (Additional Authenticated Data) is required on every wrap/unwrap.
Callers pass it explicitly so the GCM tag binds the wrapped material
to its row context — for org-master wraps, the AAD is
``b"org_master_key:" + org_id_bytes + b":v" + key_version_bytes``.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import struct
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------

WIRE_VERSION = 0x01
ALGO_AES_256_GCM = 0x01

KEY_ID_LEN = 16
IV_LEN = 12
TAG_LEN = 16
HEADER_LEN = 1 + 1 + KEY_ID_LEN + IV_LEN  # 30 bytes


class WireFormatError(ValueError):
    """Raised when a wrapped blob has an unrecognized header."""


def _pack(*, key_id: bytes, iv: bytes, ciphertext_with_tag: bytes) -> bytes:
    if len(key_id) != KEY_ID_LEN:
        raise ValueError("key_id must be 16 bytes")
    if len(iv) != IV_LEN:
        raise ValueError("iv must be 12 bytes")
    return bytes([WIRE_VERSION, ALGO_AES_256_GCM]) + key_id + iv + ciphertext_with_tag


def _unpack(blob: bytes) -> tuple[bytes, bytes, bytes]:
    if len(blob) < HEADER_LEN + TAG_LEN:
        raise WireFormatError("wrapped blob too short")
    if blob[0] != WIRE_VERSION:
        raise WireFormatError(f"unsupported wire version {blob[0]:#x}")
    if blob[1] != ALGO_AES_256_GCM:
        raise WireFormatError(f"unsupported algo {blob[1]:#x}")
    key_id = blob[2 : 2 + KEY_ID_LEN]
    iv = blob[2 + KEY_ID_LEN : 2 + KEY_ID_LEN + IV_LEN]
    ct = blob[HEADER_LEN:]
    return key_id, iv, ct


# ---------------------------------------------------------------------------
# Abstract vault
# ---------------------------------------------------------------------------


class KeyVault(ABC):
    """Symmetric key vault interface.

    Implementations expose the operator key as an opaque service:
    callers can wrap/unwrap blobs but never see the operator key
    itself. ``operator_key_id`` is a stable identifier for the current
    key; it changes after ``rotate``.
    """

    @abstractmethod
    def operator_key_id(self) -> str: ...

    @abstractmethod
    def wrap(self, plaintext: bytes, *, aad: bytes) -> bytes: ...

    @abstractmethod
    def unwrap(self, ciphertext: bytes, *, aad: bytes) -> bytes: ...

    @abstractmethod
    def rotate(self) -> "KeyVault":
        """Return a new vault with a freshly generated operator key.

        The caller is responsible for re-wrapping every blob currently
        protected by the old vault.
        """


# ---------------------------------------------------------------------------
# LocalKeyVault — file on disk
# ---------------------------------------------------------------------------


class LocalKeyVault(KeyVault):
    """Operator key stored as 32 raw bytes in a single file.

    File mode is enforced to 0600 (owner read/write only) at every
    access. If the file does not exist when ``LocalKeyVault`` is
    instantiated, ``ensure=True`` will generate one; ``ensure=False``
    raises so deploys that expect a pre-provisioned key fail loudly.
    """

    KEY_LEN = 32

    def __init__(self, path: Path, *, ensure: bool = True) -> None:
        self._path = Path(path)
        self._key: Optional[bytes] = None
        self._key_id: Optional[str] = None
        if ensure:
            self._ensure_key()
        self._load_key()

    @classmethod
    def from_env(cls) -> "LocalKeyVault":
        path = os.environ.get(
            "TBDTASK_OPERATOR_KEY_PATH",
            str(Path("data") / "operator_key"),
        )
        return cls(Path(path), ensure=True)

    # --- KeyVault contract -----------------------------------------------

    def operator_key_id(self) -> str:
        # ``local:<sha-256-prefix>`` — stable per-key but does not leak
        # the key bytes. Prefix to 16 hex chars (64 bits) keeps the
        # column short and is enough for collision-free rotation
        # within an org's lifetime.
        if self._key_id is None:
            assert self._key is not None
            digest = hashlib.sha256(self._key).hexdigest()[:16]
            self._key_id = f"local:{digest}"
        return self._key_id

    def wrap(self, plaintext: bytes, *, aad: bytes) -> bytes:
        assert self._key is not None
        iv = secrets.token_bytes(IV_LEN)
        ct_with_tag = AESGCM(self._key).encrypt(iv, plaintext, aad)
        return _pack(
            key_id=self._key_id_bytes(), iv=iv, ciphertext_with_tag=ct_with_tag
        )

    def unwrap(self, ciphertext: bytes, *, aad: bytes) -> bytes:
        assert self._key is not None
        key_id, iv, ct = _unpack(ciphertext)
        if key_id != self._key_id_bytes():
            raise WireFormatError(
                "operator-key id mismatch — blob was wrapped under a different key"
            )
        # cryptography raises InvalidTag on auth-tag failure; let it
        # bubble so callers can distinguish wrong-key from wrong-aad.
        return AESGCM(self._key).decrypt(iv, ct, aad)

    def rotate(self) -> "LocalKeyVault":
        # Atomic-ish swap: write a new file alongside, then rename
        # over. Keeps the old key file backed up under .prev so a
        # rotation that crashes mid-rewrap can be undone.
        new_key = secrets.token_bytes(self.KEY_LEN)
        new_path = self._path.with_suffix(self._path.suffix + ".new")
        backup = self._path.with_suffix(self._path.suffix + ".prev")
        new_path.write_bytes(new_key)
        os.chmod(new_path, 0o600)
        if self._path.exists():
            # Preserve the old key for one rotation cycle so a partial
            # rewrap can fall back. Future rotation overwrites this.
            backup.write_bytes(self._path.read_bytes())
            os.chmod(backup, 0o600)
        os.replace(new_path, self._path)
        return LocalKeyVault(self._path, ensure=False)

    # --- internals --------------------------------------------------------

    def _ensure_key(self) -> None:
        if self._path.exists():
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Use os.open + O_EXCL so two concurrent boots don't race-create.
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secrets.token_bytes(self.KEY_LEN))
        finally:
            os.close(fd)

    def _load_key(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(
                f"Operator key file missing: {self._path}. "
                f"Set TBDTASK_OPERATOR_KEY_PATH or initialise LocalKeyVault "
                f"with ensure=True."
            )
        # Defence in depth: enforce 0600 even if the file pre-existed
        # with looser permissions.
        try:
            os.chmod(self._path, 0o600)
        except PermissionError:
            # Some filesystems (e.g. tmpfs in CI) reject chmod; skip.
            pass
        data = self._path.read_bytes()
        if len(data) != self.KEY_LEN:
            raise ValueError(
                f"Operator key at {self._path} has wrong length "
                f"({len(data)} bytes, expected {self.KEY_LEN})"
            )
        self._key = data
        self._key_id = None  # recompute on next access

    def _key_id_bytes(self) -> bytes:
        # 16-byte deterministic id derived from the key. Embedded into
        # the wire format header so unwrap can verify the blob was
        # wrapped under *this* key.
        assert self._key is not None
        return hashlib.sha256(self._key).digest()[:KEY_ID_LEN]


# ---------------------------------------------------------------------------
# Cloud KMS placeholders (real implementations land in 8b.3)
# ---------------------------------------------------------------------------


class AWSKMSKeyVault(KeyVault):
    """Placeholder. Real implementation in 8b.3."""

    def __init__(self, key_arn: str) -> None:
        self._key_arn = key_arn

    def operator_key_id(self) -> str:
        return self._key_arn

    def wrap(self, plaintext: bytes, *, aad: bytes) -> bytes:  # noqa: ARG002
        raise NotImplementedError("AWSKMSKeyVault not implemented yet (Phase 8b.3)")

    def unwrap(self, ciphertext: bytes, *, aad: bytes) -> bytes:  # noqa: ARG002
        raise NotImplementedError("AWSKMSKeyVault not implemented yet (Phase 8b.3)")

    def rotate(self) -> "AWSKMSKeyVault":
        raise NotImplementedError("AWSKMSKeyVault not implemented yet (Phase 8b.3)")


class GCPKMSKeyVault(KeyVault):
    """Placeholder. Real implementation in 8b.3."""

    def __init__(self, key_resource: str) -> None:
        self._key_resource = key_resource

    def operator_key_id(self) -> str:
        return self._key_resource

    def wrap(self, plaintext: bytes, *, aad: bytes) -> bytes:  # noqa: ARG002
        raise NotImplementedError("GCPKMSKeyVault not implemented yet (Phase 8b.3)")

    def unwrap(self, ciphertext: bytes, *, aad: bytes) -> bytes:  # noqa: ARG002
        raise NotImplementedError("GCPKMSKeyVault not implemented yet (Phase 8b.3)")

    def rotate(self) -> "GCPKMSKeyVault":
        raise NotImplementedError("GCPKMSKeyVault not implemented yet (Phase 8b.3)")


# ---------------------------------------------------------------------------
# Provider selector
# ---------------------------------------------------------------------------


def get_vault() -> KeyVault:
    """Return the configured operator key vault.

    Reads ``TBDTASK_KEY_VAULT`` (default ``local``). Cloud providers
    are placeholders in 8b.1 and raise from their methods.
    """
    provider = os.environ.get("TBDTASK_KEY_VAULT", "local").lower()
    if provider == "local":
        return LocalKeyVault.from_env()
    if provider == "aws_kms":
        arn = os.environ.get("TBDTASK_OPERATOR_KMS_KEY_ARN")
        if not arn:
            raise RuntimeError(
                "TBDTASK_KEY_VAULT=aws_kms requires TBDTASK_OPERATOR_KMS_KEY_ARN"
            )
        return AWSKMSKeyVault(arn)
    if provider == "gcp_kms":
        res = os.environ.get("TBDTASK_OPERATOR_KMS_KEY_RESOURCE")
        if not res:
            raise RuntimeError(
                "TBDTASK_KEY_VAULT=gcp_kms requires TBDTASK_OPERATOR_KMS_KEY_RESOURCE"
            )
        return GCPKMSKeyVault(res)
    raise RuntimeError(f"Unknown TBDTASK_KEY_VAULT provider: {provider!r}")


# ---------------------------------------------------------------------------
# AAD helpers — keep the construction in one place so callers can't
# accidentally produce mismatched binding strings.
# ---------------------------------------------------------------------------


def aad_for_org_master_key(*, org_id: int, key_version: int) -> bytes:
    """AAD for org-master-key wraps. See §6 of the security spec."""
    return b"org_master_key:" + struct.pack(">qI", org_id, key_version)


def aad_for_credential_key(
    *,
    credential_uuid: str,
    org_id: int,
    key_version: int,
) -> bytes:
    """AAD for per-credential KEK_org_master wraps."""
    return (
        b"credential_key:"
        + credential_uuid.encode("ascii")
        + b":"
        + struct.pack(">qI", org_id, key_version)
    )
