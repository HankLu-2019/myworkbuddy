import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AuthSession, LoginAttempt, User

_hasher = PasswordHasher()
# Verified against when the username doesn't exist, so response time doesn't reveal valid usernames.
_DUMMY_HASH = _hasher.hash("not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_username(username: str) -> str:
    return username.strip().lower()


def login_keys(username: str, ip: str) -> dict[str, str]:
    """The three rate-limit keys for a login attempt (design 01 §H11)."""
    name = normalize_username(username)
    return {"user_ip": f"u:{name}|ip:{ip}", "ip": f"ip:{ip}", "user": f"u:{name}"}


def _limits() -> dict[str, int]:
    s = get_settings()
    return {"user_ip": s.login_max_attempts, "ip": s.login_max_attempts_ip, "user": s.login_max_attempts_user}


async def is_rate_limited(db: AsyncSession, keys: dict[str, str]) -> bool:
    """True when any key has reached its limit of failed attempts within the window."""
    since = datetime.now(UTC) - timedelta(minutes=get_settings().login_window_minutes)
    counts = dict(
        (
            await db.execute(
                select(LoginAttempt.key, func.count())
                .where(LoginAttempt.key.in_(keys.values()), LoginAttempt.at > since)
                .group_by(LoginAttempt.key)
            )
        ).all()
    )
    return any(counts.get(keys[name], 0) >= limit for name, limit in _limits().items())


async def record_failed_login(db: AsyncSession, keys: dict[str, str]) -> None:
    db.add_all(LoginAttempt(key=k) for k in keys.values())
    await db.commit()


async def authenticate(db: AsyncSession, username: str, password: str) -> User | None:
    user = await db.scalar(select(User).where(User.username == normalize_username(username)))
    if user is None:
        verify_password(_DUMMY_HASH, password)
        return None
    if not verify_password(user.password_hash, password) or not user.is_active:
        return None
    return user


async def create_session(
    db: AsyncSession, user: User, keys: dict[str, str], *, ip: str | None = None, user_agent: str | None = None
) -> tuple[str, AuthSession]:
    """Create a session. Clears only the username+IP failure count, so the IP and username limits still apply."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    session = AuthSession(
        token_hash=hash_token(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(24),
        expires_at=now + timedelta(hours=get_settings().session_ttl_hours),
        last_seen_at=now,
        ip=ip,
        user_agent=(user_agent or "")[:255] or None,
    )
    db.add(session)
    user.last_login_at = now
    await db.execute(delete(LoginAttempt).where(LoginAttempt.key == keys["user_ip"]))
    return token, session


LAST_SEEN_RESOLUTION = timedelta(minutes=5)


def session_alive_after(now: datetime) -> datetime:
    """Sessions last seen before this instant are idle-expired."""
    return now - timedelta(hours=get_settings().session_idle_hours)


async def resolve_session(db: AsyncSession, token: str) -> tuple[AuthSession, User] | None:
    """The live session for a cookie: not expired, not idle, user active. Refreshes `last_seen_at` at most every
    5 minutes (committed right away, so read-only requests keep the session alive too)."""
    now = datetime.now(UTC)
    row = (
        await db.execute(
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(
                AuthSession.token_hash == hash_token(token),
                AuthSession.expires_at > now,
                AuthSession.last_seen_at > session_alive_after(now),
            )
        )
    ).first()
    if row is None or not row.User.is_active:
        return None
    session = row.AuthSession
    if now - session.last_seen_at >= LAST_SEEN_RESOLUTION:
        await db.execute(update(AuthSession).where(AuthSession.id == session.id).values(last_seen_at=now))
        await db.commit()
        session.last_seen_at = now
    return session, row.User


async def revoke_user_sessions(db: AsyncSession, user_id: int, *, keep: int | None = None) -> int:
    """Delete the user's sessions (except `keep`). Returns how many were removed."""
    q = delete(AuthSession).where(AuthSession.user_id == user_id)
    if keep is not None:
        q = q.where(AuthSession.id != keep)
    return (await db.execute(q)).rowcount


def generate_password() -> str:
    """A one-time initial password: 16 characters from an unambiguous alphabet (about 92 bits)."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(16))
