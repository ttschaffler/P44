"""Password hashing and opaque-token handling. Tokens are stored only as keyed hashes."""
import base64
import hashlib
import hmac
import secrets

_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)

ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def token_hash(token: str, secret_key: str) -> str:
    """Keyed so that rotating the secret key invalidates every session, key and invite."""
    return hmac.new(secret_key.encode(), token.encode(), hashlib.sha256).hexdigest()


def sign_payload(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def password_ok(password: str) -> bool:
    return len(password) >= 10
