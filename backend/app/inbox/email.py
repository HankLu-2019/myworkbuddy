"""The email outbox (design 02 §5). Callers queue a row inside their own transaction; `send_emails` (S5) sends
pending rows with retries and applies the daily cap. Bodies are plain text built by us, never model HTML."""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import OutboundEmail, User

SUBJECT_CHARS = 150


def app_url(path: str) -> str:
    """An absolute link into MyWorkBuddy: the only kind of link an email may contain."""
    return f"{get_settings().hostname.rstrip('/')}{path}"


async def queue_email(db: AsyncSession, user: User, *, subject: str, body: str, dedupe_key: str) -> bool:
    """Queue unless the user has no address or turned email off. Returns whether a row was added (the dedupe
    key makes a retried caller harmless)."""
    if not user.email or not user.email_notifications:
        return False
    stmt = (
        insert(OutboundEmail)
        .values(
            owner_id=user.id,
            to_addr=user.email,
            subject=" ".join(subject.split())[:SUBJECT_CHARS],
            body_text=body,
            dedupe_key=dedupe_key,
        )
        .on_conflict_do_nothing(index_elements=[OutboundEmail.dedupe_key])
        .returning(OutboundEmail.id)
    )
    return (await db.execute(stmt)).scalar() is not None
