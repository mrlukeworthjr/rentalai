import os
import secrets
from datetime import datetime

from sqlalchemy import (JSON, Column, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text,
                        create_engine)
from sqlalchemy.orm import declarative_base, deferred, relationship, sessionmaker

URL = os.environ.get("DATABASE_URL", "sqlite:///./rentalai.db")
for prefix in ("postgres://", "postgresql://"):      # Railway hands out either form; pin the driver we ship
    if URL.startswith(prefix):
        URL = "postgresql+psycopg2://" + URL[len(prefix):]
engine = create_engine(URL, pool_pre_ping=True,
                       connect_args={"check_same_thread": False} if URL.startswith("sqlite") else {})
Session = sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()
now = datetime.utcnow


class Org(Base):
    __tablename__ = "orgs"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    income_multiple = Column(Float, default=3.0)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("orgs.id"), nullable=False, index=True)
    email = Column(String(320), unique=True, nullable=False)
    name = Column(String(200))
    pw = Column(String(300), nullable=False)
    org = relationship(Org)


class Application(Base):
    __tablename__ = "applications"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("orgs.id"), nullable=False, index=True)
    token = Column(String(64), unique=True, default=lambda: secrets.token_urlsafe(24))
    applicant_name = Column(String(200), nullable=False)
    applicant_email = Column(String(320))
    property = Column(String(200))
    monthly_rent = Column(Float)
    verdict = Column(String(20))
    summary = Column(JSON, default=dict)
    created_at = Column(DateTime, default=now)
    documents = relationship("Document", back_populates="application", order_by="Document.id",
                             cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False, index=True)
    filename = Column(String(300))
    sha256 = Column(String(64), index=True)
    size = Column(Integer)
    source = Column(String(20), default="staff")      # staff | applicant
    status = Column(String(20), default="queued")     # queued | processing | done | error
    doc_type = Column(String(30))
    verdict = Column(String(20))
    risk = Column(Integer)
    report = Column(JSON)
    review = Column(JSON(none_as_null=True))                              # analyst decision, the training label
    data = deferred(Column(LargeBinary))               # original bytes, never modified
    created_at = Column(DateTime, default=now)
    application = relationship(Application, back_populates="documents")


class Audit(Base):
    __tablename__ = "audit_events"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, index=True)
    user_id = Column(Integer)
    action = Column(String(60))
    target = Column(String(120))
    detail = Column(Text)
    at = Column(DateTime, default=now)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(60), primary_key=True)
    blob = Column(LargeBinary)


def init():
    Base.metadata.create_all(engine)
