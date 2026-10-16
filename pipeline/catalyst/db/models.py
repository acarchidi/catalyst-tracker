"""SQLAlchemy tables. Each mirrors a Pydantic model in `catalyst.schemas` field for field."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CHAR, DateTime, MetaData, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all tables, with stable constraint names for migrations."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class SourceDocumentRow(Base):
    """Table for `catalyst.schemas.SourceDocument`. One row per distinct copy of a document."""

    __tablename__ = "source_document"
    __table_args__ = (UniqueConstraint("url", "sha256", name="uq_source_document_url_sha256"),)

    id: Mapped[UUID] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    publisher: Mapped[str] = mapped_column(String(200))
    document_type: Mapped[str] = mapped_column(String(100))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sha256: Mapped[str] = mapped_column(CHAR(64))
    snapshot_key: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(200))
