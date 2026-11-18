import hashlib
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, LargeBinary, String, Text, UniqueConstraint, create_engine, event, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./.local/maintenance.db"
    demo_mode: bool = False
    admin_password: str = ""
    admin_org: str = Field(default="pilot", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    model_url: str = ""
    model_key: str = ""
    chat_model: str = ""
    embedding_model: str = ""
    embedding_dimensions: int = 0
    embedding_min_similarity: float = 0.48
    rrf_k: int = 60
    allow_model_data: bool = False
    id_userinfo_url: str = ""
    registry_url: str = ""
    integration_token: str = ""
    workorder_url: str = ""
    workorder_enabled: bool = False
    secure_cookie: bool = False
    ocr_lang: str = "chi_sim+eng"
    clamav_command: str = ""
    max_upload_mib: int = 20
    allowed_ticket_pattern: str = r"[A-Za-z0-9][A-Za-z0-9_./-]{2,63}"
    retention_days: int = 90
    expert_sla_days: int = 2
    org_timezone: str = "Asia/Shanghai"
    org_holidays: str = ""


cfg = Settings()
Path(".local").mkdir(exist_ok=True)
engine = create_engine(cfg.database_url, pool_pre_ping=True)
DB = sessionmaker(engine, expire_on_commit=False)


if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _):
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")


def now():
    return datetime.now(timezone.utc).isoformat()


def timestamp():
    return datetime.now(timezone.utc).timestamp()


def uid():
    return uuid4().hex


def password_hash(value, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(value.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + ":" + digest.hex()


class Base(DeclarativeBase):
    pass


class Record(Base):
    __tablename__ = "records"
    __table_args__ = (UniqueConstraint("org", "kind", "key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    org: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    key: Mapped[str] = mapped_column(String(200), default=uid)
    owner: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(40), default="")
    rev: Mapped[int] = mapped_column(Integer, default=1)
    created: Mapped[str] = mapped_column(String(40), default=now)
    updated: Mapped[str] = mapped_column(String(40), default=now)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    blob: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)

    def view(self, *exclude):
        return {**{k: v for k, v in self.data.items() if k not in exclude},
                "id": self.id, "key": self.key, "status": self.status, "revision": self.rev,
                "created": self.created, "updated": self.updated}

    def change(self, status=None, **data):
        self.data = {**self.data, **data}
        self.updated = now()
        self.rev += 1
        if status is not None:
            self.status = status


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = {"postgresql_partition_by": "LIST (org)"}
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    org: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    position: Mapped[str] = mapped_column(String(180))
    body: Mapped[str] = mapped_column(Text)
    ocr: Mapped[bool] = mapped_column(Boolean, default=False)
    embedding: Mapped[list | None] = mapped_column(JSON(none_as_null=True).with_variant(Vector(), "postgresql"))
    embedding_model: Mapped[str] = mapped_column(String(100), default="")

    def view(self):
        return dict(id=self.id, position=self.position, text=self.body, ocr=self.ocr)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(24))
    org: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available: Mapped[float] = mapped_column(Float, default=timestamp)
    lease: Mapped[str] = mapped_column(String(32), default="")
    leased_until: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str] = mapped_column(String(400), default="")


class Scope(Base):
    __tablename__ = "effective_scopes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    org: Mapped[str] = mapped_column(String(80), index=True)
    number: Mapped[str] = mapped_column(String(90))
    model: Mapped[str] = mapped_column(String(80))
    batch: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(12))
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def view(self):
        def iso(value):
            return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat() if value else ""
        return dict(id=self.id, number=self.number, model=self.model, batch=self.batch, sourceId=self.source,
                    status=self.status, start=iso(self.start), end=iso(self.end))


def scopes(org):
    return select(Scope).where(Scope.org == org)


def current_scopes(org, instant=None):
    instant = instant or datetime.now(timezone.utc)
    return scopes(org).where(Scope.start <= instant, (Scope.end.is_(None) | (instant < Scope.end)))


def records(org, kind):
    return select(Record).where(Record.org == org, Record.kind == kind)


def find(db, org, kind, *, key=None, id=None, lock=False):
    query = records(org, kind).where(Record.id == id if id is not None else Record.key == key)
    return db.scalar(query.with_for_update() if lock else query)


def add(db, org, kind, *, key=None, owner="", status="", blob=None, **data):
    instant = now()
    row = Record(id=uid(), org=org, kind=kind, key=key or uid(), owner=owner, status=status,
                 rev=1, created=instant, updated=instant, data=data, blob=blob)
    db.add(row)
    return row


def audit(db, org, actor, action, target, request_id=""):
    add(db, org, "audit", owner=actor, action=action, target=target, actor=actor, requestId=request_id)


def enqueue(db, org, kind, target):
    db.add(Job(org=org, kind=kind, target=target))


def set_org(db, org):
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT set_config('maintenance.org', :org, true)"), {"org": org})


def digest(data):
    return hashlib.sha256(data).hexdigest()


def init_db():
    from alembic import command
    from alembic.config import Config

    path = Path(__file__).resolve().parents[1]
    config = Config()
    config.set_main_option("script_location", path.joinpath("migrations").as_posix().replace("%", "%%"))
    command.upgrade(config, "head")
    with DB.begin() as db:
        if cfg.admin_password and len(cfg.admin_password) < 12:
            raise ValueError("ADMIN_PASSWORD must contain at least 12 characters")
        if cfg.admin_password and not find(db, cfg.admin_org, "user", key="admin"):
            add(db, cfg.admin_org, "user", key="admin", status="active", name="平台管理员",
                role="platform", password=password_hash(cfg.admin_password))
        if cfg.demo_mode:
            from .seed import seed_demo
            seed_demo(db)

