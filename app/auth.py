"""Local password gate for the app.

The password is never stored. What is stored is a salted **scrypt** hash (with a
PBKDF2-HMAC-SHA256 fallback for a Python built without scrypt), in its own file
beside the settings so a settings reset cannot silently unlock the app.

Be clear about what this is and is not:

* It stops someone who sits down at this machine from opening the app and using
  your API key or your question banks.
* It is **not** encryption. The documents on disk are untouched, and anyone with
  file access can read them, run the CLI, or delete this file to clear the
  password. Treat it as a lock on the front door, not a safe.

Nothing here logs, returns or echoes the password.
"""

from __future__ import annotations

import binascii
import hashlib
import hmac
import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)

#: Short enough not to be a nuisance for a single-user desk tool, long enough
#: that a shoulder-surfer cannot guess it in three tries.
MIN_PASSWORD_LENGTH = 6

#: scrypt work factors. n=2**15 with r=8 costs 128*n*r = 32 MiB and a few tenths
#: of a second -- unnoticeable on a login screen, expensive in bulk.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64

#: OpenSSL caps scrypt's memory at 32 MiB by default, which the parameters above
#: land exactly on -- so it refuses with "memory limit exceeded" and we would
#: quietly drop to PBKDF2 every time. Raise the ceiling explicitly.
_SCRYPT_MAXMEM = 96 * 1024 * 1024

#: Fallback iterations, sized to take a comparable amount of time.
_PBKDF2_ROUNDS = 600_000

_SALT_BYTES = 16

ALGO_SCRYPT = "scrypt"
ALGO_PBKDF2 = "pbkdf2_sha256"


class AuthError(Exception):
    """Raised when a password cannot be accepted or stored."""


@dataclass(slots=True)
class AuthRecord:
    """What actually lands on disk. No reversible material in here."""

    algo: str
    salt: str
    hash: str
    n: int = 0
    r: int = 0
    p: int = 0
    rounds: int = 0
    version: int = 1


# --------------------------------------------------------------------------- #
# Location
# --------------------------------------------------------------------------- #


def auth_path() -> Path:
    """Where the hash lives -- next to settings.json, in its own file.

    Honours the same environment override the settings use, so a test run never
    touches the real credential.
    """
    override = os.environ.get("KRISHNAS_SUPERBOT_AUTH", "").strip()
    if override:
        return Path(override)
    try:
        from app.config import settings_path
    except ImportError:  # pragma: no cover - config is always present in the app
        base = Path(os.environ.get("APPDATA") or Path.home()) / "KrishnasSuperBot"
        return base / "auth.json"
    return settings_path().parent / "auth.json"


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #


def _scrypt_available() -> bool:
    """True when scrypt works at *the parameters we actually use*.

    Probing with toy parameters would report success and then fail on the real
    call, which is exactly how the PBKDF2 fallback got taken silently.
    """
    try:
        hashlib.scrypt(
            b"probe", salt=b"probe", n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
            dklen=16, maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, MemoryError, AttributeError, OSError):
        return False
    return True


def _derive(password: str, salt: bytes, record: AuthRecord | None = None) -> tuple[str, dict[str, int]]:
    """Return (hex digest, parameters) for `password`.

    When `record` is given its parameters are reused, so an existing hash is
    verified with the settings it was created under rather than today's.
    """
    raw = password.encode("utf-8")
    algo = record.algo if record else (ALGO_SCRYPT if _scrypt_available() else ALGO_PBKDF2)

    if algo == ALGO_SCRYPT:
        n = record.n if record and record.n else _SCRYPT_N
        r = record.r if record and record.r else _SCRYPT_R
        p = record.p if record and record.p else _SCRYPT_P
        try:
            digest = hashlib.scrypt(
                raw, salt=salt, n=n, r=r, p=p, dklen=_SCRYPT_DKLEN,
                maxmem=_SCRYPT_MAXMEM,
            )
        except (ValueError, MemoryError, OSError) as exc:
            # A memory-constrained box can refuse the work factor. Fall back
            # rather than lock the user out of their own app.
            _LOG.warning("scrypt unavailable at these parameters (%s); using PBKDF2", exc)
            rounds = _PBKDF2_ROUNDS
            digest = hashlib.pbkdf2_hmac("sha256", raw, salt, rounds)
            return binascii.hexlify(digest).decode(), {"rounds": rounds}
        return binascii.hexlify(digest).decode(), {"n": n, "r": r, "p": p}

    rounds = record.rounds if record and record.rounds else _PBKDF2_ROUNDS
    digest = hashlib.pbkdf2_hmac("sha256", raw, salt, rounds)
    return binascii.hexlify(digest).decode(), {"rounds": rounds}


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def _read() -> AuthRecord | None:
    path = auth_path()
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        # A corrupt credential file must not brick the app. Treat it as "no
        # password set" and say so loudly in the log.
        _LOG.error("could not read %s (%s); treating the app as unlocked", path.name, exc)
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return AuthRecord(
            algo=str(raw.get("algo") or ""),
            salt=str(raw.get("salt") or ""),
            hash=str(raw.get("hash") or ""),
            n=int(raw.get("n") or 0),
            r=int(raw.get("r") or 0),
            p=int(raw.get("p") or 0),
            rounds=int(raw.get("rounds") or 0),
            version=int(raw.get("version") or 1),
        )
    except (TypeError, ValueError) as exc:
        _LOG.error("malformed credential file (%s); treating the app as unlocked", exc)
        return None


def _write(record: AuthRecord) -> None:
    path = auth_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        raise AuthError(f"could not save the password: {exc}") from exc
    _restrict(path)


def _restrict(path: Path) -> None:
    """Make the file owner-only where the OS allows it. Best effort."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name == "nt":
        # chmod is close to a no-op on Windows, so drop inherited ACLs and grant
        # only the current user. Failure is not fatal: the hash is not a secret
        # in the way the password is.
        import subprocess

        user = os.environ.get("USERNAME", "")
        if not user:
            return
        try:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True, check=False, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _LOG.debug("could not tighten ACLs on %s: %s", path.name, exc)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def is_password_set() -> bool:
    """True when a usable credential exists."""
    record = _read()
    return bool(record and record.salt and record.hash)


def set_password(password: str) -> None:
    """Store `password` as a fresh salted hash. Raises `AuthError` if too short."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"the password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    salt = os.urandom(_SALT_BYTES)
    digest, params = _derive(password, salt)
    _write(
        AuthRecord(
            algo=ALGO_SCRYPT if "n" in params else ALGO_PBKDF2,
            salt=binascii.hexlify(salt).decode(),
            hash=digest,
            n=int(params.get("n", 0)),
            r=int(params.get("r", 0)),
            p=int(params.get("p", 0)),
            rounds=int(params.get("rounds", 0)),
        )
    )


def verify_password(password: str) -> bool:
    """True when `password` matches the stored hash.

    Compared with `hmac.compare_digest` so the check does not leak how much of
    the password was right through its timing.
    """
    record = _read()
    if not record or not record.salt or not record.hash:
        return False
    if not isinstance(password, str) or not password:
        return False
    try:
        salt = binascii.unhexlify(record.salt)
    except (binascii.Error, ValueError):
        _LOG.error("credential salt is not valid hex; cannot verify")
        return False
    digest, _ = _derive(password, salt, record)
    return hmac.compare_digest(digest, record.hash)


def change_password(old: str, new: str) -> None:
    """Replace the password. Raises `AuthError` if `old` is wrong or `new` weak."""
    if is_password_set() and not verify_password(old):
        raise AuthError("the current password is not correct")
    set_password(new)


def clear_password() -> None:
    """Remove the credential entirely, leaving the app unlocked."""
    try:
        auth_path().unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AuthError(f"could not remove the password: {exc}") from exc


def describe() -> str:
    """One line about the stored credential, for the settings screen. No secrets."""
    record = _read()
    if not record:
        return "No password set — the app opens without asking."
    if record.algo == ALGO_SCRYPT:
        return f"Password set (scrypt, n={record.n}, r={record.r})."
    return f"Password set (PBKDF2-SHA256, {record.rounds:,} rounds)."
