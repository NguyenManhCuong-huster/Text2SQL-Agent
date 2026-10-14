"""Optional single-password login (APP_PASSWORD) with signed cookies, plus a simple per-IP rate limiter."""
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque

from . import config

COOKIE = "t2s_session"
SESSION_TTL_S = 7 * 24 * 3600

_secret: bytes | None = None


def enabled() -> bool:
    """Authentication is required as soon as a password or an API key is configured."""
    return bool(config.APP_PASSWORD or config.API_KEYS)


def password_enabled() -> bool:
    return bool(config.APP_PASSWORD)


def valid_api_key(key: str | None) -> bool:
    """Constant-time comparison against every configured key."""
    if not key or not config.API_KEYS:
        return False
    digest = hashlib.sha256(key.encode()).digest()
    ok = False
    for k in config.API_KEYS:
        ok |= hmac.compare_digest(digest, hashlib.sha256(k.encode()).digest())
    return ok


def request_api_key(headers) -> str | None:
    authz = headers.get("authorization", "")
    if authz[:7].lower() == "bearer ":
        return authz[7:].strip()
    return headers.get("x-api-key")


def authenticated(cookies, headers) -> bool:
    return valid_token(cookies.get(COOKIE)) or valid_api_key(request_api_key(headers))


def _get_secret() -> bytes:
    """SECRET_KEY env, else a random key persisted in DATA_DIR so sessions survive restarts and are shared by workers."""
    global _secret
    if _secret is None:
        if config.SECRET_KEY:
            _secret = config.SECRET_KEY.encode()
        else:
            path = config.DATA_DIR / ".secret"
            try:
                with open(path, "xb") as f:
                    f.write(secrets.token_bytes(32))
                os.chmod(path, 0o600)
            except FileExistsError:
                pass
            _secret = path.read_bytes()
    return _secret


def _sign(payload: str) -> str:
    return hmac.new(_get_secret(), payload.encode(), hashlib.sha256).hexdigest()


def check_password(password: str) -> bool:
    return password_enabled() and hmac.compare_digest(password.encode(), config.APP_PASSWORD.encode())


def issue_token() -> str:
    exp = str(int(time.time()) + SESSION_TTL_S)
    return f"{exp}.{_sign(exp)}"


def valid_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    if not hmac.compare_digest(sig, _sign(exp)):
        return False
    return exp.isdigit() and int(exp) > time.time()


class RateLimiter:
    """Sliding-window limiter, in memory and per process (so effectively limit x WORKERS)."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        if self.per_minute <= 0:
            return True, 0
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False, int(60 - (now - q[0])) + 1
            q.append(now)
            if len(self.hits) > 10000:  # bound memory under many distinct IPs
                for k in [k for k, v in self.hits.items() if not v][:5000]:
                    del self.hits[k]
            return True, 0


llm_limiter = RateLimiter(config.RATE_LIMIT_PER_MIN)
login_limiter = RateLimiter(10)
exec_limiter = RateLimiter(config.EXEC_RATE_LIMIT_PER_MIN)
