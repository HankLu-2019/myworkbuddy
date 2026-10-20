import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, func, select
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


async def is_rate_limited(db: AsyncSession, key: str) -> bool:
    s = get_settings()
    since = datetime.now(UTC) - timedelta(minutes=s.login_window_minutes)
    count = await db.scalar(select(func.count()).where(LoginAttempt.key == key, LoginAttempt.at > since))
    return (count or 0) >= s.login_max_attempts


async def record_failed_login(db: AsyncSession, key: str) -> None:
    db.add(LoginAttempt(key=key))
    await db.commit()


async def authenticate(db: AsyncSession, username: str, password: str) -> User | None:
    user = await db.scalar(select(User).where(User.username == normalize_username(username)))
    if user is None:
        verify_password(_DUMMY_HASH, password)
        return None
    if not verify_password(user.password_hash, password) or not user.is_active:
        return None
    return user


async def create_session(db: AsyncSession, user: User, login_key: str) -> tuple[str, AuthSession]:
    token = secrets.token_urlsafe(32)
    session = AuthSession(
        token_hash=hash_token(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(24),
        expires_at=datetime.now(UTC) + timedelta(hours=get_settings().session_ttl_hours),
    )
    db.add(session)
    await db.execute(delete(LoginAttempt).where(LoginAttempt.key == login_key))
    await db.execute(delete(AuthSession).where(AuthSession.expires_at < datetime.now(UTC)))
    return token, session


async def resolve_session(db: AsyncSession, token: str) -> tuple[AuthSession, User] | None:
    row = (
        await db.execute(
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.token_hash == hash_token(token), AuthSession.expires_at > datetime.now(UTC))
        )
    ).first()
    if row is None or not row.User.is_active:
        return None
    return row.AuthSession, row.User


async def revoke_user_sessions(db: AsyncSession, user_id: int) -> None:
    await db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
