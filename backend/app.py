from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import smtplib
import time
import base64
import uuid
from threading import Lock
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, create_engine, func, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.exc import IntegrityError

from backend.payments import (
    PaymentStatus,
    PaymentProviderError,
    ProviderPaymentResult,
    get_mobile_money_provider,
    is_payment_provider_available,
    normalize_zambian_phone,
    register_mobile_money_provider,
)
from backend.lenco import (
    LENCO_COLLECTION_WEBHOOK_EVENTS,
    LENCO_PROVIDER_IDS,
    lenco_providers,
    verify_lenco_webhook_signature,
)
from backend.curriculum import (
    ADDITIONAL_LESSONS,
    FOUNDATION_CORE_LESSONS,
    LESSON_MODULES,
    MODULES,
    MODULE_BY_ID,
    NEW_CHALLENGE_DATA,
    NEW_CHALLENGE_META,
    NEW_QUIZ_DATA,
    NEW_QUIZ_META,
    LESSON_ONLY_QUIZ_DATA,
    LESSON_ONLY_QUIZ_LESSONS,
)


for lenco_provider in lenco_providers:
    register_mobile_money_provider(lenco_provider)


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("kite")
auth_attempts: dict[str, list[float]] = {}
auth_attempts_lock = Lock()


class Base(DeclarativeBase):
    pass


def is_hosted_environment() -> bool:
    return bool(
        os.getenv("RENDER") or os.getenv("VERCEL") or os.getenv("VERCEL_ENV")
        or os.getenv("KITE_ENV", "").strip().lower() == "production"
    )


def vercel_proxy_mode() -> bool:
    return os.getenv("KITE_VERCEL_PROXY", "").strip().lower() in {"1", "true", "yes"}


CONFIRMED_PRODUCTION_FRONTEND_ORIGIN = "https://learnpythonwithkite.vercel.app"
DEFAULT_DEVELOPMENT_CORS_ORIGINS = (
    "http://localhost",
    "https://localhost",
    "capacitor://localhost",
    CONFIRMED_PRODUCTION_FRONTEND_ORIGIN,
)


def configured_cors_origins() -> list[str]:
    raw = os.getenv("KITE_CORS_ORIGINS")
    if is_hosted_environment():
        if raw is not None and raw.strip():
            origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
        else:
            origins = [CONFIRMED_PRODUCTION_FRONTEND_ORIGIN]
        if CONFIRMED_PRODUCTION_FRONTEND_ORIGIN not in origins:
            origins.append(CONFIRMED_PRODUCTION_FRONTEND_ORIGIN)
        return origins

    if raw is not None and raw.strip():
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    return list(DEFAULT_DEVELOPMENT_CORS_ORIGINS)


def validate_postgres_ssl_url(value: str, setting_name: str) -> None:
    if not value.startswith("postgresql+psycopg://"):
        return
    ssl_modes = parse_qs(urlsplit(value).query).get("sslmode", [])
    if len(ssl_modes) != 1 or ssl_modes[0].lower() not in {"require", "verify-ca", "verify-full"}:
        parsed_url = urlsplit(value)
        if (
            len(ssl_modes) == 1
            and ssl_modes[0].lower() == "disable"
            and setting_name == "MIGRATION_DATABASE_URL"
            and not is_hosted_environment()
            and os.getenv("KITE_POSTGRES_INTEGRATION_TESTS") == "1"
            and os.getenv("KITE_TEST_SAFETY_MARKER") == "KITE_DISPOSABLE_POSTGRES_TEST_ONLY"
            and os.getenv("KITE_TEST_DATABASE_URL") == value
            and parsed_url.hostname == "127.0.0.1"
            and parsed_url.port == 55432
            and parsed_url.username == "kite_test_owner"
            and parsed_url.path == "/kite_integration_test"
        ):
            return
        raise RuntimeError(
            f"{setting_name} must specify sslmode=require, verify-ca, or verify-full."
        )


def database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        if is_hosted_environment() or os.getenv("KITE_ENV", "").strip().lower() != "development":
            raise RuntimeError(
                "DATABASE_URL is required; SQLite is allowed only when KITE_ENV=development."
            )
        return "sqlite:///./kite.db"
    if value.startswith("postgres://"):
        value = "postgresql+psycopg://" + value[len("postgres://") :]
    elif value.startswith("postgresql://"):
        value = "postgresql+psycopg://" + value[len("postgresql://") :]
    if is_hosted_environment() and not value.startswith("postgresql+psycopg://"):
        raise RuntimeError("Hosted environments require a PostgreSQL DATABASE_URL.")
    if is_hosted_environment():
        validate_postgres_ssl_url(value, "DATABASE_URL")
    return value


def validate_runtime_configuration() -> None:
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    supabase_key = os.getenv("SUPABASE_ANON_KEY", "").strip()
    if bool(supabase_url) != bool(supabase_key):
        raise RuntimeError("Set both SUPABASE_URL and SUPABASE_ANON_KEY, or leave both empty.")
    if not is_hosted_environment():
        return

    database_url()
    if len(os.getenv("KITE_SECRET_KEY", "").strip()) < 32:
        raise RuntimeError("KITE_SECRET_KEY must contain at least 32 characters in hosted environments.")
    if not supabase_url or not supabase_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY are required in hosted environments.")
    parsed_supabase_url = urlsplit(supabase_url)
    if (
        parsed_supabase_url.scheme != "https" or not parsed_supabase_url.netloc
        or parsed_supabase_url.path not in {"", "/"} or parsed_supabase_url.query
        or parsed_supabase_url.fragment
    ):
        raise RuntimeError("SUPABASE_URL must be an HTTPS project origin.")

    app_url = os.getenv("PUBLIC_APP_URL", "").strip()
    parsed_app_url = urlsplit(app_url)
    if (
        parsed_app_url.scheme != "https" or not parsed_app_url.netloc
        or parsed_app_url.path not in {"", "/"} or parsed_app_url.query
        or parsed_app_url.fragment
    ):
        raise RuntimeError("PUBLIC_APP_URL must be the HTTPS origin of the frontend.")
    app_origin = parsed_app_url.scheme + "://" + parsed_app_url.netloc
    api_url = os.getenv("PUBLIC_API_URL", "").strip()
    parsed_api_url = urlsplit(api_url)
    if (
        parsed_api_url.scheme != "https" or not parsed_api_url.netloc
        or parsed_api_url.path not in {"", "/"} or parsed_api_url.query
        or parsed_api_url.fragment
    ):
        raise RuntimeError("PUBLIC_API_URL must be the HTTPS origin of the API.")
    cors_origins = configured_cors_origins()
    if not cors_origins or "*" in cors_origins or app_origin not in cors_origins:
        raise RuntimeError("KITE_CORS_ORIGINS must list exact origins and include PUBLIC_APP_URL.")
    for origin in cors_origins:
        parsed_origin = urlsplit(origin)
        if (
            parsed_origin.scheme not in {"https", "capacitor"} or not parsed_origin.netloc
            or parsed_origin.path or parsed_origin.query or parsed_origin.fragment
        ):
            raise RuntimeError("Hosted KITE_CORS_ORIGINS entries must be exact HTTPS or Capacitor origins.")
        if parsed_origin.hostname in {"localhost", "127.0.0.1", "::1"} or (
            parsed_origin.hostname and parsed_origin.hostname.endswith(".localhost")
        ):
            raise RuntimeError("Localhost origins are restricted to development environments.")
    if vercel_proxy_mode():
        if app_origin != parsed_api_url.scheme + "://" + parsed_api_url.netloc:
            raise RuntimeError(
                "Vercel proxy mode requires PUBLIC_API_URL to match PUBLIC_APP_URL."
            )
        proxy_secret = os.getenv("KITE_PROXY_SECRET", "").strip()
        previous_proxy_secret = os.getenv("KITE_PROXY_SECRET_PREVIOUS", "").strip()
        if len(proxy_secret) < 32:
            raise RuntimeError(
                "KITE_PROXY_SECRET must contain at least 32 characters in Vercel proxy mode."
            )
        if previous_proxy_secret and (
            len(previous_proxy_secret) < 32
            or hmac.compare_digest(previous_proxy_secret, proxy_secret)
        ):
            raise RuntimeError(
                "KITE_PROXY_SECRET_PREVIOUS must be a different value of at least 32 characters."
            )
    else:
        cookie_site = os.getenv("KITE_COOKIE_SITE", "").strip().lower().rstrip(".")
        site_labels = cookie_site.split(".")
        if (
            len(site_labels) < 2
            or any(
                not label or not label[0].isalnum() or not label[-1].isalnum()
                or any(not (character.isalnum() or character == "-") for character in label)
                for label in site_labels
            )
            or cookie_site in {"vercel.app", "onrender.com", "supabase.co"}
        ):
            raise RuntimeError("KITE_COOKIE_SITE must be a registrable custom domain.")
        frontend_host = parsed_app_url.hostname or ""
        api_host = parsed_api_url.hostname or ""
        if not all(
            host == cookie_site or host.endswith("." + cookie_site)
            for host in (frontend_host, api_host)
        ):
            raise RuntimeError(
                "PUBLIC_APP_URL and PUBLIC_API_URL must use subdomains of KITE_COOKIE_SITE."
            )
    if os.getenv("KITE_COOKIE_SECURE", "").strip().lower() != "true":
        raise RuntimeError("KITE_COOKIE_SECURE=true is required in hosted environments.")
    if os.getenv("KITE_COOKIE_SAMESITE", "lax").strip().lower() != "lax":
        raise RuntimeError(
            "KITE_COOKIE_SAMESITE=lax is required for hosted cookie authentication."
        )
    if os.getenv("KITE_ALLOW_LOCAL_AUTH", "").strip().lower() in {"1", "true", "yes"}:
        raise RuntimeError("KITE_ALLOW_LOCAL_AUTH must be disabled in hosted environments.")


def database_engine_options(url: str, *, serverless: bool) -> dict[str, Any]:
    options: dict[str, Any] = {
        "connect_args": {"check_same_thread": False} if url.startswith("sqlite") else {},
        "pool_pre_ping": True,
    }
    if serverless and url.startswith("postgresql+psycopg://"):
        options["connect_args"] = {"prepare_threshold": None}
        options["pool_size"] = 1
        options["max_overflow"] = 0
    return options


DB_URL = database_url()
engine = create_engine(
    DB_URL,
    **database_engine_options(
        DB_URL,
        serverless=bool(os.getenv("VERCEL") or os.getenv("VERCEL_ENV")),
    ),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    auth_subject: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(160))
    xp: Mapped[int] = mapped_column(Integer, default=0)
    streak: Mapped[int] = mapped_column(Integer, default=0)
    best_streak: Mapped[int] = mapped_column(Integer, default=0)
    last_active: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settings: Mapped[str] = mapped_column(Text, default="{}")
    auth_session_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class RevokedToken(Base):
    __tablename__ = "revoked_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    remember_me: Mapped[bool] = mapped_column(Boolean, default=False)
    user_agent: Mapped[str] = mapped_column(String(200), default="Browser")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Lesson(Base):
    __tablename__ = "lessons"
    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    order_num: Mapped[int] = mapped_column(Integer)
    premium: Mapped[bool] = mapped_column(Boolean, default=False)
    xp_reward: Mapped[int] = mapped_column(Integer, default=20)


class LessonProgress(Base):
    __tablename__ = "lesson_progress"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"), primary_key=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[str] = mapped_column(String(120))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_ciphertext: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(40))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class MobileMoneyPayment(Base):
    __tablename__ = "mobile_money_payments"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_mobile_money_user_idempotency"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    plan_id: Mapped[str] = mapped_column(String(40))
    amount: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40))
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    display_price: Mapped[str] = mapped_column(String(40))
    amount: Mapped[str] = mapped_column(String(20))
    currency: Mapped[str] = mapped_column(String(3), default="ZMW")
    period: Mapped[str | None] = mapped_column(String(20), nullable=True)
    premium: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str] = mapped_column(String(300))
    features_json: Mapped[str] = mapped_column(Text, default="[]")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class SubscriptionRegistration(Base):
    __tablename__ = "subscription_registrations"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_subscription_registration_idempotency"),
        CheckConstraint(
            "status IN ('PENDING_PAYMENT', 'ACTIVE', 'EXPIRED', 'CANCELLED', 'FAILED')",
            name="ck_subscription_registration_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("subscription_plans.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    access_key_hash: Mapped[str] = mapped_column(String(64))
    phone_ciphertext: Mapped[str] = mapped_column(Text)
    phone_hash: Mapped[str] = mapped_column(String(64), index=True)
    phone_last4: Mapped[str] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(32), default="PENDING_PAYMENT", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('NOT_INITIATED', 'PENDING', 'PROCESSING', 'SUCCESSFUL', 'FAILED', 'CANCELLED', 'EXPIRED')",
            name="ck_payment_transaction_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    registration_id: Mapped[str] = mapped_column(
        ForeignKey("subscription_registrations.id", ondelete="CASCADE"), index=True,
    )
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    reference: Mapped[str] = mapped_column(String(64), unique=True)
    provider_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    provider_reference: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    amount: Mapped[str] = mapped_column(String(20))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(24), default=PaymentStatus.NOT_INITIATED.value, index=True)
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"
    __table_args__ = (
        UniqueConstraint("transaction_id", "attempt_number", name="uq_payment_attempt_number"),
        UniqueConstraint("idempotency_key", name="uq_payment_attempt_idempotency"),
        CheckConstraint("attempt_number > 0", name="ck_payment_attempt_number"),
        CheckConstraint(
            "status IN ('NOT_INITIATED', 'PENDING', 'PROCESSING', 'SUCCESSFUL', 'FAILED', 'CANCELLED', 'EXPIRED')",
            name="ck_payment_attempt_status",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("payment_transactions.id", ondelete="CASCADE"), index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(36))
    provider_id: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24))
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentStatusHistory(Base):
    __tablename__ = "payment_status_history"
    __table_args__ = (
        CheckConstraint(
            "status IN ('NOT_INITIATED', 'PENDING', 'PROCESSING', 'SUCCESSFUL', 'FAILED', 'CANCELLED', 'EXPIRED')",
            name="ck_payment_status_history_status",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("payment_transactions.id", ondelete="CASCADE"), index=True,
    )
    previous_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    status: Mapped[str] = mapped_column(String(24))
    source: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PaymentProviderConfiguration(Base):
    __tablename__ = "payment_provider_configurations"
    __table_args__ = (
        CheckConstraint("environment IN ('sandbox', 'production')", name="ck_payment_provider_environment"),
    )
    provider_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    environment: Mapped[str] = mapped_column(String(20), default="sandbox")
    options_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Activity(Base):
    __tablename__ = "activity"
    __table_args__ = (UniqueConstraint("user_id", "kind", "item_id", name="uq_activity_user_kind_item"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    item_id: Mapped[str] = mapped_column(String(80))
    xp: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PlaygroundError(Base):
    __tablename__ = "playground_errors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    fixed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlaygroundExecution(Base):
    __tablename__ = "playground_executions"
    execution_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class GuestPlaygroundExecution(Base):
    __tablename__ = "guest_playground_executions"
    execution_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class QuizResult(Base):
    __tablename__ = "quiz_results"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    quiz_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    best_score: Mapped[int] = mapped_column(Integer)
    total_questions: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"
    __table_args__ = (
        Index(
            "uq_quiz_attempts_submission", "user_id", "quiz_id", "submission_id",
            unique=True,
            sqlite_where=text("submission_id IS NOT NULL"),
            postgresql_where=text("submission_id IS NOT NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    quiz_id: Mapped[str] = mapped_column(String(80), index=True)
    score: Mapped[int] = mapped_column(Integer)
    total_questions: Mapped[int] = mapped_column(Integer)
    percentage: Mapped[int] = mapped_column(Integer)
    passed: Mapped[bool] = mapped_column(Boolean)
    feedback_json: Mapped[str] = mapped_column(Text)
    submission_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Notice(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("uq_notifications_user_event", "user_id", "event_key", unique=True),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(String(500))
    category: Mapped[str] = mapped_column(String(30), default="updates")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    event_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    target_route: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PasswordReset(Base):
    __tablename__ = "password_resets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)


LESSONS = list(FOUNDATION_CORE_LESSONS)

CHALLENGES = {
    "hello-world": {"tests": [("", "Hello, World!")], "xp": 100},
    "sum-variables": {"tests": [("a = 12\nb = 30", "42"), ("a = 7\nb = 5", "12")], "xp": 150},
    "count-to-five": {"tests": [("", "1\n2\n3\n4\n5")], "xp": 200},
}
QUIZZES = {
    "python-basics": [
        {"question": "What is the output?", "options": ["Hi", '"Hi"', "print", "Error"], "answer": 0,
         "code": 'print("Hi")', "explanation": "print displays the string without its quote characters."},
        {"question": "Which symbol assigns a value to a variable?", "options": ["==", "=", "->", "#"], "answer": 1,
         "explanation": "The single equals sign binds the value on its right to the name on its left; == compares values."},
        {"question": "What is the output?", "options": ["6", "8", "42", "Error"], "answer": 1,
         "code": "print(4 + 4)", "explanation": "The addition expression evaluates to 8."},
        {"question": "Which of these is a string?", "options": ["42", "3.14", '"42"', "True"], "answer": 2,
         "explanation": "Quotation marks make the characters 4 and 2 text; without quotes, 42 is an integer."},
        {"question": "What is the output?", "options": ["3", "1", "0", "3.33"], "answer": 1,
         "code": "print(4 // 3)", "explanation": "Floor division returns the integer quotient."},
        {"question": 'What does len("kite") return?', "options": ["3", "4", "5", "Error"], "answer": 1,
         "explanation": "len counts the four characters in the string."},
        {"question": "Which is a valid variable name?", "options": ["2name", "my-name", "my_name", "my name"], "answer": 2,
         "explanation": "Identifiers may contain letters, digits, and underscores, but cannot start with a digit or contain spaces and hyphens."},
        {"question": "What is the output?", "options": ["2", "3", "5", "Error"], "answer": 2,
         "code": "print(2 + 3)", "explanation": "The two integer values add to 5."},
        {"question": "What is the output?", "options": ["Py2", "PyPy", "Py Py", "Error"], "answer": 1,
         "code": 'print("Py" * 2)', "explanation": "Multiplying a string repeats its contents."},
        {"question": "What is the output?", "options": ["0", "1", "2", "3"], "answer": 1,
         "code": "for value in range(1, 2):\n    print(value)",
         "explanation": "range(1, 2) contains the single value 1."},
    ]
}
LESSONS.extend(ADDITIONAL_LESSONS)
CHALLENGES.update(NEW_CHALLENGE_DATA)
QUIZZES.update({
    quiz_id: data["questions"]
    for quiz_id, data in NEW_QUIZ_DATA.items()
})
for quiz_questions in QUIZZES.values():
    for question in quiz_questions:
        if question.get("type"):
            continue
        prompt = question["question"].casefold()
        if question.get("code"):
            question["type"] = (
                "output_prediction"
                if any(term in prompt for term in ("output", "print", "printed"))
                else "code_interpretation"
            )
        elif "error" in prompt or "exception" in prompt:
            question["type"] = "error_identification"
        else:
            question["type"] = "multiple_choice"
QUIZ_METADATA = [
    {"id": "python-basics", "module_id": "python-foundations",
     "title": "Python Basics", "question_count": len(QUIZZES["python-basics"])}
] + NEW_QUIZ_META
CHALLENGE_METADATA = [
    {"id": "hello-world", "module_id": "python-foundations", "title": "Hello, World!",
     "description": "Print the exact text Hello, World! from your program.",
     "starter_code": "# Write your print statement here.",
     "hint": "Use print with a quoted string.", "test_count": len(CHALLENGES["hello-world"]["tests"]),
     "xp": CHALLENGES["hello-world"]["xp"]},
    {"id": "sum-variables", "module_id": "python-foundations", "title": "Add two values",
     "description": "Print the sum of the provided variables a and b.",
     "starter_code": "# Print the sum of a and b.",
     "hint": "Use the + operator.", "test_count": len(CHALLENGES["sum-variables"]["tests"]),
     "xp": CHALLENGES["sum-variables"]["xp"]},
    {"id": "count-to-five", "module_id": "python-loops", "title": "Count to five",
     "description": "Print the integers from 1 through 5, one per line.",
     "starter_code": "# Use a loop to print 1 through 5.",
     "hint": "range excludes its stop value.", "test_count": len(CHALLENGES["count-to-five"]["tests"]),
     "xp": CHALLENGES["count-to-five"]["xp"]},
] + NEW_CHALLENGE_META
QUIZ_RELATED_LESSONS = {
    "python-basics": ["python-variables", "python-foundations-syntax-indent", "python-foundations-expressions-output"],
    "foundation-programming": ["python-foundations-algorithms", "python-foundations-runtime", "python-foundations-source-files"],
    "types-data": ["python-types", "python-types-integers-floats", "python-types-strings", "python-types-booleans-none"],
    "types-conversion": ["python-types-convert-types", "python-types-type-inspection", "python-types-mutability", "python-types-choose-data-type"],
    "operators-expressions": ["python-conditions", "python-conditions-comparisons", "python-conditions-logical-operators", "python-conditions-assignment-operators"],
    "input-output": ["python-conditions-input", "python-conditions-formatted-output", "python-conditions-mini-calculator"],
    "control-decisions": ["python-loops-branching", "python-loops-truthiness", "python-loops-nested-decisions"],
    "loops-iteration": ["python-loops", "python-loops-while-loops", "python-loops-break-continue", "python-loops-loop-summary"],
    "collections-lists": ["python-lists", "python-lists-list-slices", "python-lists-list-mutations"],
    "collections-processing": ["python-lists-list-iteration", "python-lists-list-comprehensions", "python-lists-copy-lists", "python-lists-sort-search"],
    "functions-basics": ["python-functions", "python-functions-parameters-arguments"],
    "functions-design": ["python-functions-defaults-keywords", "python-functions-scope", "python-functions-pure-functions", "python-functions-docstrings-types"],
    "strings-reading": ["python-strings-literals", "python-strings-index-slice", "python-strings-f-strings", "python-strings-methods"],
    "strings-transforming": ["python-strings-methods", "python-strings-split-join", "python-strings-search-count", "python-strings-validation"],
    "mappings-lookup": ["python-mappings-dict-create", "python-mappings-dict-get", "python-mappings-dict-update"],
    "mappings-collections": ["python-mappings-set-operations", "python-mappings-tuples", "python-mappings-dict-comprehension", "python-mappings-nested-data"],
    "files-io": ["python-files-paths", "python-files-open-context", "python-files-read-lines", "python-files-write-text", "python-files-safe-paths"],
    "files-formats": ["python-files-json", "python-files-csv", "python-files-in-memory-data"],
    "errors-handling": ["python-errors-try-except", "python-errors-specific-errors", "python-errors-else-finally", "python-errors-raise"],
    "errors-diagnostics": ["python-errors-tracebacks", "python-errors-assertions", "python-errors-debug-tests"],
    "oop-classes": ["python-oop-class-instance", "python-oop-init-self", "python-oop-methods", "python-oop-properties"],
    "oop-design": ["python-oop-inheritance", "python-oop-composition", "python-oop-dataclasses"],
    "advanced-iteration": ["python-advanced-iterators", "python-advanced-generators", "python-advanced-comprehensions"],
    "advanced-architecture": ["python-advanced-imports", "python-advanced-packages", "python-advanced-context-managers", "python-advanced-unit-tests"],
    "professional-practice": ["python-advanced-virtual-environments", "python-advanced-logging", "python-advanced-asyncio", "python-advanced-capstone"],
}
for quiz in QUIZ_METADATA:
    module = MODULE_BY_ID[quiz["module_id"]]
    quiz["module_title"] = module["title"]
    quiz["module_order"] = module["order"]
    quiz["topic"] = quiz["title"]
    quiz["difficulty"] = (
        "Beginner" if module["order"] <= 4 else
        "Intermediate" if module["order"] <= 9 else "Advanced"
    )
    quiz["pass_threshold"] = 80
    quiz["lesson_ids"] = QUIZ_RELATED_LESSONS.get(quiz["id"], [])
    module_challenges = [
        item for item in CHALLENGE_METADATA if item["module_id"] == quiz["module_id"]
    ]
    quiz["practice_challenge_id"] = module_challenges[0]["id"] if module_challenges else None
LESSON_ONLY_QUIZ_METADATA = []
for quiz_id, data in LESSON_ONLY_QUIZ_DATA.items():
    lesson_id = LESSON_ONLY_QUIZ_LESSONS[quiz_id]
    module_id = LESSON_MODULES.get(lesson_id, (None, 0))[0]
    if module_id != data["module_id"]:
        raise ValueError("Lesson-only quiz module does not match its lesson.")
    module = MODULE_BY_ID[module_id]
    module_challenges = [
        item for item in CHALLENGE_METADATA if item["module_id"] == module_id
    ]
    LESSON_ONLY_QUIZ_METADATA.append({
        "id": quiz_id,
        "module_id": module_id,
        "module_title": module["title"],
        "module_order": module["order"],
        "title": data["title"],
        "topic": data["title"],
        "difficulty": (
            "Beginner" if module["order"] <= 4 else
            "Intermediate" if module["order"] <= 9 else "Advanced"
        ),
        "pass_threshold": 80,
        "question_count": len(data["questions"]),
        "lesson_ids": [lesson_id],
        "practice_challenge_id": module_challenges[0]["id"] if module_challenges else None,
        "estimated_minutes": 5,
        "lesson_only": True,
    })
QUIZ_METADATA_BY_ID = {item["id"]: item for item in QUIZ_METADATA}
QUIZ_METADATA_BY_ID.update(
    {item["id"]: item for item in LESSON_ONLY_QUIZ_METADATA}
)
CHALLENGE_METADATA_BY_ID = {item["id"]: item for item in CHALLENGE_METADATA}


def quiz_questions(quiz_id: str) -> list[dict[str, Any]] | None:
    questions = QUIZZES.get(quiz_id)
    if questions is not None:
        return questions
    lesson_quiz = LESSON_ONLY_QUIZ_DATA.get(quiz_id)
    return lesson_quiz["questions"] if lesson_quiz else None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def same_instant(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    if right.tzinfo is None:
        right = right.replace(tzinfo=timezone.utc)
    return left.astimezone(timezone.utc) == right.astimezone(timezone.utc)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$", 2)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 310_000)
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def token_key() -> bytes:
    secret = os.getenv("KITE_SECRET_KEY")
    if not secret:
        raise HTTPException(status_code=503, detail="KITE_SECRET_KEY is not configured.")
    if len(secret) < 32:
        raise HTTPException(status_code=503, detail="KITE_SECRET_KEY must contain at least 32 characters.")
    return secret.encode()


def supabase_configured() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_ANON_KEY"))


def local_auth_enabled() -> bool:
    if os.getenv("VERCEL") or os.getenv("VERCEL_ENV") or os.getenv("RENDER"):
        return False
    return os.getenv("KITE_ALLOW_LOCAL_AUTH", "").strip().lower() in {"1", "true", "yes"}


def require_auth_provider() -> None:
    if not supabase_configured() and not local_auth_enabled():
        raise HTTPException(
            status_code=503,
            detail="Authentication is not configured. Configure Supabase Auth or enable local auth for development.",
        )


def enforce_auth_rate_limit(
    request: Request,
    action: str,
    identity: str,
    limit: int,
    window_seconds: int,
) -> None:
    client_host = request.client.host if request.client else "unknown"
    if getattr(request.state, "kite_proxy_authenticated", False):
        forwarded_ip = request.headers.get("x-real-ip", "").strip()
        try:
            client_host = str(ipaddress.ip_address(forwarded_ip))
        except ValueError:
            pass
    identity_hash = hashlib.sha256(
        (action + "\0" + client_host + "\0" + identity.strip().lower()).encode()
    ).hexdigest()
    now = time.monotonic()
    with auth_attempts_lock:
        recent = [attempt for attempt in auth_attempts.get(identity_hash, []) if now - attempt < window_seconds]
        if len(recent) >= limit:
            retry_after = max(1, int(window_seconds - (now - recent[0])))
            auth_attempts[identity_hash] = recent
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Please wait a little while and try again.",
                headers={"Retry-After": str(retry_after)},
            )
        recent.append(now)
        auth_attempts[identity_hash] = recent
        if len(auth_attempts) > 4096:
            expired = [
                key for key, attempts in auth_attempts.items()
                if not attempts or now - attempts[-1] >= window_seconds
            ]
            for key in expired:
                auth_attempts.pop(key, None)


def password_strength_error(password: str) -> str | None:
    if password.casefold() in {
        "password", "password1", "password123", "12345678", "123456789",
        "qwerty123", "letmein123", "admin1234",
    }:
        return "Choose a less common password."
    categories = sum((
        any(character.islower() for character in password),
        any(character.isupper() for character in password),
        any(character.isdigit() for character in password),
        any(not character.isalnum() for character in password),
    ))
    if categories < 2:
        return "Use a mix of letters, numbers, or symbols in your password."
    return None


def supabase_error(path: str, method: str, status: int, response: httpx.Response) -> HTTPException:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    provider_message = " ".join(
        str(body.get(key, "")) for key in ("code", "error_code", "error", "msg", "message")
    ).lower()
    if status == 429:
        return HTTPException(
            status_code=429,
            detail="Too many attempts. Please wait a little while and try again.",
            headers={"Retry-After": response.headers.get("Retry-After", "60")},
        )
    if path.startswith("/token?grant_type=password"):
        if "confirm" in provider_message and "email" in provider_message:
            return HTTPException(status_code=403, detail="Confirm your email address before signing in.")
        return HTTPException(status_code=401, detail="Email or password is incorrect.")
    if path.startswith("/signup"):
        if any(term in provider_message for term in ("password", "weak", "length")):
            return HTTPException(status_code=422, detail="Choose a stronger password and try again.")
        return HTTPException(
            status_code=400,
            detail="We couldn't complete registration. If you already have an account, try signing in.",
        )
    if path.startswith("/resend"):
        return HTTPException(status_code=400, detail="Verification email could not be sent. Please try again later.")
    if path.startswith("/recover"):
        return HTTPException(
            status_code=503,
            detail="Password recovery is temporarily unavailable. Please try again later.",
        )
    if path.startswith("/token?grant_type=pkce"):
        return HTTPException(
            status_code=401,
            detail="Google sign-in expired or could not be completed. Please try again.",
        )
    if path.startswith("/token?grant_type=refresh_token"):
        return HTTPException(status_code=401, detail="Your session has expired. Sign in again.")
    if path.startswith("/user") and method == "PUT":
        return HTTPException(
            status_code=400,
            detail="We couldn't update your account. The link may be expired; request a new one and try again.",
        )
    if path.startswith("/user") or path.startswith("/logout"):
        return HTTPException(status_code=401, detail="Your session has expired. Sign in again.")
    return HTTPException(status_code=503, detail="Authentication service could not complete the request.")


def supabase_request(path: str, *, method: str = "GET", data: dict[str, Any] | None = None,
                     access_token: str | None = None) -> dict[str, Any]:
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    headers = {"apikey": os.getenv("SUPABASE_ANON_KEY", ""), "Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = "Bearer " + access_token
    try:
        with httpx.Client(timeout=12) as client:
            response = client.request(method, base + "/auth/v1" + path, headers=headers, json=data)
    except httpx.HTTPError as error:
        log.warning("Supabase Auth request failed (%s).", type(error).__name__)
        raise HTTPException(status_code=503, detail="Authentication service is unavailable.")
    if response.status_code >= 400:
        raise supabase_error(path, method, response.status_code, response)
    return response.json() if response.content else {}


def email_is_verified(auth_user: dict[str, Any]) -> bool:
    return bool(
        auth_user.get("email_confirmed_at")
        or auth_user.get("confirmed_at")
        or auth_user.get("email_verified") is True
    )


def local_user_for_supabase(auth_user: dict[str, Any], db: Session) -> User:
    subject = str(auth_user["id"])
    email = str(auth_user.get("email", "")).strip().lower()
    if not email or not email_is_verified(auth_user):
        raise HTTPException(status_code=403, detail="Confirm your email address before signing in.")
    user = db.scalar(select(User).where(User.auth_subject == subject))
    if user is None and email:
        user = db.scalar(select(User).where(User.email == email))
    if user and user.auth_subject and user.auth_subject != subject:
        raise HTTPException(
            status_code=409,
            detail="This email is already connected to another Kite sign-in method.",
        )
    metadata = auth_user.get("user_metadata") or {}
    if user is None:
        user = User(email=email, auth_subject=subject,
                    name=str(metadata.get("name") or email.split("@")[0])[:80],
                    password_hash=hash_password(secrets.token_urlsafe(32)))
        db.add(user)
        db.flush()
    else:
        user.auth_subject = subject
        if email:
            user.email = email
        if metadata.get("name"):
            user.name = str(metadata["name"])[:80]
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This email is already connected to another Kite sign-in method.",
        ) from error
    return user


AUTH_ACCESS_COOKIE = "kite_access"
AUTH_REFRESH_COOKIE = "kite_refresh"
AUTH_SESSION_COOKIE = "kite_session"
SESSION_HOURS = 8
REMEMBER_DAYS = 30


def issue_token(user: User, remember_me: bool) -> tuple[str, datetime]:
    lifetime = timedelta(days=REMEMBER_DAYS) if remember_me else timedelta(hours=SESSION_HOURS)
    expires_at = utcnow() + lifetime
    payload = f"{user.id}.{int(expires_at.timestamp())}.{user.auth_session_version}.{secrets.token_hex(8)}"
    signature = hmac.new(token_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}", expires_at


def csrf_token(user_id: int) -> str:
    return hmac.new(
        token_key(), ("kite-csrf-v1:" + str(user_id)).encode(), hashlib.sha256,
    ).hexdigest()


def cookie_settings(request: Request, remember_me: bool) -> dict[str, Any]:
    same_site = os.getenv(
        "KITE_COOKIE_SAMESITE", "lax",
    ).strip().lower()
    if same_site not in {"lax", "none"}:
        raise RuntimeError("KITE_COOKIE_SAMESITE must be 'lax' or 'none'.")
    secure = request.url.scheme == "https" or os.getenv("KITE_COOKIE_SECURE", "").lower() == "true"
    max_age = REMEMBER_DAYS * 24 * 60 * 60 if remember_me else None
    if same_site == "none":
        secure = True
    return {"httponly": True, "secure": secure, "samesite": same_site, "path": "/", "max_age": max_age}


def set_auth_cookies(
    request: Request,
    response: Response,
    db: Session,
    user: User,
    access_token: str,
    refresh_token: str | None,
    remember_me: bool,
    expires_at: datetime,
    session_id: str | None = None,
) -> str:
    now = utcnow()
    duration = timedelta(days=REMEMBER_DAYS) if remember_me else timedelta(hours=SESSION_HOURS)
    session_id = session_id or secrets.token_hex(24)
    existing = db.get(AuthSession, session_id)
    session = existing or AuthSession(
        id=session_id,
        user_id=user.id,
        token_hash=hashlib.sha256(access_token.encode()).hexdigest(),
        created_at=now,
        last_seen_at=now,
        expires_at=now + duration,
        remember_me=remember_me,
        user_agent=(request.headers.get("user-agent") or "Browser")[:200],
    )
    if existing:
        session.user_id = user.id
        session.token_hash = hashlib.sha256(access_token.encode()).hexdigest()
        session.last_seen_at = now
        session.expires_at = now + duration
        session.remember_me = remember_me
    db.add(session)
    settings = cookie_settings(request, remember_me)
    response.set_cookie(AUTH_ACCESS_COOKIE, access_token, **settings)
    if refresh_token:
        response.set_cookie(AUTH_REFRESH_COOKIE, refresh_token, **settings)
    else:
        response.delete_cookie(AUTH_REFRESH_COOKIE, path="/", secure=settings["secure"],
                               httponly=True, samesite=settings["samesite"])
    response.set_cookie(AUTH_SESSION_COOKIE, session_id, **settings)
    return csrf_token(user.id)


def clear_auth_cookies(request: Request, response: Response) -> None:
    settings = cookie_settings(request, False)
    for name in (AUTH_ACCESS_COOKIE, AUTH_REFRESH_COOKIE, AUTH_SESSION_COOKIE):
        response.delete_cookie(
            name, path="/", secure=settings["secure"], httponly=True,
            samesite=settings["samesite"],
        )


def credential_token(request: Request, authorization: str | None) -> tuple[str | None, bool]:
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:], False
    cookie = request.cookies.get(AUTH_ACCESS_COOKIE)
    return (cookie, True) if cookie else (None, False)


def validate_csrf(request: Request, user: User, using_cookie: bool) -> None:
    if not using_cookie or request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    supplied = request.headers.get("x-kite-csrf", "")
    if not hmac.compare_digest(supplied, csrf_token(user.id)):
        raise HTTPException(status_code=403, detail="This request could not be verified. Refresh the page and try again.")


def validate_local_token(value: str, db: Session) -> User:
    if not local_auth_enabled():
        raise HTTPException(status_code=401, detail="Your session has expired. Sign in again.")
    try:
        parts = value.split(".")
        if len(parts) == 3:
            user_s, expiry_s, signature = parts
            version_s = "0"
            payload = f"{user_s}.{expiry_s}"
        elif len(parts) == 4:
            user_s, expiry_s, version_s, signature = parts
            payload = f"{user_s}.{expiry_s}.{version_s}"
        elif len(parts) == 5:
            user_s, expiry_s, version_s, nonce, signature = parts
            payload = f"{user_s}.{expiry_s}.{version_s}.{nonce}"
        else:
            raise ValueError
        expected = hmac.new(token_key(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected) or int(expiry_s) <= int(time.time()):
            raise ValueError
        user = db.get(User, int(user_s))
        if user is None or int(version_s) != user.auth_session_version:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Your session has expired. Sign in again.")
    token_hash = hashlib.sha256(value.encode()).hexdigest()
    if db.get(RevokedToken, token_hash):
        raise HTTPException(status_code=401, detail="Your session has been signed out.")
    return user


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> User:
    credential, using_cookie = credential_token(request, authorization)
    if not credential:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    session = None
    if using_cookie:
        session_id = request.cookies.get(AUTH_SESSION_COOKIE, "")
        session = db.get(AuthSession, session_id) if session_id else None
        if (
            session is None or session.revoked_at is not None
            or ensure_utc(session.expires_at) <= utcnow()
            or session.token_hash != hashlib.sha256(credential.encode()).hexdigest()
        ):
            clear_auth_cookies(request, response)
            raise HTTPException(status_code=401, detail="Your session has expired. Sign in again.")
    if supabase_configured():
        try:
            auth_user = supabase_request("/user", access_token=credential)
        except HTTPException as error:
            refresh_token = request.cookies.get(AUTH_REFRESH_COOKIE) if using_cookie else None
            if error.status_code != 401 or not refresh_token or session is None:
                if using_cookie:
                    clear_auth_cookies(request, response)
                raise
            refreshed = supabase_request(
                "/token?grant_type=refresh_token", method="POST",
                data={"refresh_token": refresh_token},
            )
            credential = refreshed.get("access_token", "")
            refresh_token = refreshed.get("refresh_token") or refresh_token
            if not credential:
                clear_auth_cookies(request, response)
                raise HTTPException(status_code=401, detail="Your session has expired. Sign in again.")
            expires_at = utcnow() + timedelta(seconds=int(refreshed.get("expires_in", 3600)))
            remember = session.remember_me
            refreshed_user = refreshed.get("user") or {}
            user = local_user_for_supabase(refreshed_user, db)
            session.token_hash = hashlib.sha256(credential.encode()).hexdigest()
            session.last_seen_at = utcnow()
            set_auth_cookies(
                request, response, db, user, credential, refresh_token,
                remember, expires_at, session.id,
            )
            auth_user = refreshed_user
        if not auth_user.get("id") or not auth_user.get("email"):
            raise HTTPException(status_code=401, detail="Your session has expired. Sign in again.")
        user = local_user_for_supabase(auth_user, db)
        if using_cookie and session:
            session.last_seen_at = utcnow()
        db.commit()
        validate_csrf(request, user, using_cookie)
        return user
    user = validate_local_token(credential, db)
    if using_cookie:
        session.last_seen_at = utcnow()
        db.commit()
    validate_csrf(request, user, using_cookie)
    return user


def optional_user(
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> User | None:
    credential, _ = credential_token(request, authorization)
    if not credential:
        return None
    return current_user(request, response, authorization, db)


def playground_user(
    request: Request,
    response: Response,
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> User | None:
    try:
        return optional_user(request, response, authorization, db)
    except HTTPException as error:
        if error.status_code == 401:
            return None
        raise


def premium_entitled(db: Session, user_id: int) -> bool:
    now = utcnow()
    subscriptions = db.scalars(select(Subscription).where(
        Subscription.user_id == user_id,
        Subscription.state.in_(["SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD", "SUBSCRIPTION_STATE_CANCELED"]),
    ))
    for subscription in subscriptions:
        expiry = subscription.expires_at
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry and expiry > now:
            return True
    return False


def encrypt_purchase_token(value: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = hashlib.sha256(token_key() + b"kite-google-play-token").digest()
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, value.encode(), b"kite-subscription-v1")
    return base64.urlsafe_b64encode(nonce + ciphertext).decode()


def decrypt_purchase_token(value: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.urlsafe_b64decode(value.encode())
    key = hashlib.sha256(token_key() + b"kite-google-play-token").digest()
    return AESGCM(key).decrypt(raw[:12], raw[12:], b"kite-subscription-v1").decode()


def user_payload(user: User, db: Session) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "xp": user.xp,
        "streak": current_streak(user),
        "premium": premium_entitled(db, user.id),
        "settings": json.loads(user.settings or "{}"),
    }


def current_streak(user: User) -> int:
    if user.last_active is None:
        return 0
    last_active = user.last_active
    if last_active.tzinfo is None:
        last_active = last_active.replace(tzinfo=timezone.utc)
    if last_active.astimezone(timezone.utc).date() < utcnow().date() - timedelta(days=1):
        return 0
    return user.streak


def create_notification(
    user: User,
    db: Session,
    *,
    event_key: str,
    title: str,
    body: str,
    category: str,
    target_route: str,
) -> Notice | None:
    settings = json.loads(user.settings or "{}")
    if settings.get("notifications") is False:
        return None
    existing = db.scalar(select(Notice.id).where(
        Notice.user_id == user.id,
        Notice.event_key == event_key,
    ))
    if existing is not None:
        return None

    notice = Notice(
        user_id=user.id,
        title=title[:160],
        body=body[:500],
        category=category[:30],
        event_key=event_key[:160],
        target_route=target_route[:40],
    )
    try:
        with db.begin_nested():
            db.add(notice)
            db.flush()
    except IntegrityError:
        return None
    return notice


def update_streak(user: User, db: Session) -> None:
    today = utcnow().date()
    previous = user.last_active.date() if user.last_active else None
    if previous != today:
        user.streak = (user.streak + 1) if previous == today - timedelta(days=1) else 1
        user.best_streak = max(user.best_streak or 0, user.streak)
        user.last_active = utcnow()
        if user.streak in {3, 7, 12, 30}:
            create_notification(
                user,
                db,
                event_key="streak-milestone:" + str(user.streak),
                title=str(user.streak) + "-day learning streak",
                body="You learned on " + str(user.streak) + " consecutive days. Keep it going!",
                category="achievements",
                target_route="progress",
            )


def send_reset_email(address: str, token: str) -> None:
    host = os.getenv("SMTP_HOST")
    if not host:
        raise RuntimeError("Password reset email is not configured (SMTP_HOST is missing).")
    message = EmailMessage()
    message["Subject"] = "Reset your Kite password"
    message["From"] = os.getenv("SMTP_FROM", "Kite <no-reply@example.com>")
    message["To"] = address
    base = os.getenv("PUBLIC_APP_URL", "http://localhost:8000")
    message.set_content(f"Use this one-time link to reset your Kite password:\n{base}/#reset?token={quote(token)}\n\nIt expires in 30 minutes.")
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=10) as smtp:
        smtp.starttls()
        if os.getenv("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(message)


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    remember_me: bool = False


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    remember_me: bool = False


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class EmailUpdateIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class PasswordUpdateIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class SettingsIn(BaseModel):
    theme: str | None = Field(default=None, pattern="^(light|dark|system)$")
    notifications: bool | None = None


class ResetRequestIn(BaseModel):
    email: EmailStr


class ResetCompleteIn(BaseModel):
    token: str = Field(min_length=16, max_length=4096)
    password: str = Field(min_length=8, max_length=128)


class RunIn(BaseModel):
    code: str = Field(min_length=1, max_length=20_000)
    execution_id: str | None = Field(default=None, min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    guest_token: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")


class CancelRunIn(BaseModel):
    execution_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    guest_token: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")


class ChallengeIn(BaseModel):
    code: str = Field(min_length=1, max_length=20_000)


class QuizIn(BaseModel):
    answers: list[int] = Field(max_length=30)
    submission_id: str | None = Field(
        default=None, min_length=16, max_length=64, pattern=r"^[a-f0-9-]+$",
    )


class GuestProgressImportIn(BaseModel):
    started_lessons: list[str] = Field(default_factory=list, max_length=100)
    completed_lessons: list[str] = Field(default_factory=list, max_length=100)


class GooglePurchaseIn(BaseModel):
    product_id: str = Field(min_length=1, max_length=120)
    purchase_token: str = Field(min_length=10, max_length=4096)


class MobileMoneyPaymentIn(BaseModel):
    plan_id: str = Field(pattern=r"^premium_(monthly|annual)$")
    provider: str = Field(pattern=r"^(AIRTEL_OAPI_ZMB|MTN_MOMO_ZMB|ZAMTEL_ZMB)$")
    phone_number: str | None = Field(default=None, min_length=7, max_length=40)
    idempotency_key: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )
    registration_id: str | None = Field(default=None, min_length=36, max_length=36)
    registration_key: str | None = Field(default=None, min_length=43, max_length=128)


class SubscriptionRegistrationIn(BaseModel):
    plan_id: str = Field(pattern=r"^premium_(monthly|annual)$")
    phone_number: str = Field(min_length=7, max_length=40)
    idempotency_key: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )
    access_key: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class PaymentInitiationIn(BaseModel):
    provider_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    idempotency_key: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    )


class SubscriptionRegistrationClaimIn(BaseModel):
    access_key: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class GoogleAuthCodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    code_verifier: str = Field(min_length=43, max_length=128)
    remember_me: bool = False


class SupabaseSessionIn(BaseModel):
    access_token: str | None = Field(default=None, min_length=1, max_length=4096)
    refresh_token: str | None = Field(default=None, max_length=4096)
    remember_me: bool = False


class VerificationResendIn(BaseModel):
    email: EmailStr


SUBSCRIPTION_PLAN_DEFINITIONS = (
    {
        "id": "free",
        "name": "Kite Free",
        "display_price": "Free",
        "amount": "0.00",
        "period": None,
        "premium": False,
        "summary": "Start learning Python at your own pace.",
        "features": ["Core learning path", "Guest quizzes and challenges", "Progress saved on this device"],
        "sort_order": 0,
    },
    {
        "id": "premium_monthly",
        "name": "Kite Premium · Monthly",
        "display_price": "K19.99",
        "amount": "19.99",
        "period": "month",
        "premium": True,
        "summary": "Full access for 30 days, prepaid with no automatic renewal.",
        "features": ["All 12 learning modules", "Premium lessons, quizzes, and challenges", "30 days of access"],
        "sort_order": 1,
    },
    {
        "id": "premium_annual",
        "name": "Kite Premium · Annual",
        "display_price": "K99.99",
        "amount": "99.99",
        "period": "year",
        "premium": True,
        "summary": "Full access for 365 days, prepaid with no automatic renewal.",
        "features": ["All 12 learning modules", "Premium lessons, quizzes, and challenges", "365 days of access"],
        "sort_order": 2,
    },
)


def seed_subscription_plans(db: Session) -> None:
    for definition in SUBSCRIPTION_PLAN_DEFINITIONS:
        if db.get(SubscriptionPlan, definition["id"]) is None:
            db.add(SubscriptionPlan(
                id=definition["id"],
                name=definition["name"],
                display_price=definition["display_price"],
                amount=definition["amount"],
                currency="ZMW",
                period=definition["period"],
                premium=definition["premium"],
                summary=definition["summary"],
                features_json=json.dumps(definition["features"]),
                active=True,
                sort_order=definition["sort_order"],
            ))


app = FastAPI(title="Kite API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=configured_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Kite-CSRF",
        "X-Kite-Registration-Key",
    ],
)
app.mount("/assets", StaticFiles(directory=Path(__file__).parent.parent / "web" / "assets"), name="assets")


@app.middleware("http")
async def enforce_proxy_and_browser_origin(request: Request, call_next):
    if vercel_proxy_mode() and request.url.path not in {"/health", "/health/ready"}:
        expected = os.getenv("KITE_PROXY_SECRET", "").strip()
        previous = os.getenv("KITE_PROXY_SECRET_PREVIOUS", "").strip()
        supplied = request.headers.get("x-kite-proxy-secret", "")
        valid_secret = bool(expected) and hmac.compare_digest(supplied, expected)
        if previous:
            valid_secret = hmac.compare_digest(supplied, previous) | valid_secret
        if not valid_secret:
            return JSONResponse(
                status_code=404,
                content={"detail": "Not found"},
                headers={"Cache-Control": "private, no-store"},
            )
        request.state.kite_proxy_authenticated = True

    if (
        is_hosted_environment()
        and request.url.path.startswith("/api/")
        and request.method not in {"GET", "HEAD", "OPTIONS"}
        and not request.url.path.startswith("/api/payment-webhooks/")
    ):
        origin = request.headers.get("origin")
        if not origin or origin not in configured_cors_origins():
            return JSONResponse(
                status_code=403,
                content={"detail": "This request origin is not allowed."},
                headers={"Cache-Control": "private, no-store"},
            )

    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "private, no-store"
    return response


RUNTIME_TABLE_OPERATIONS: dict[str, tuple[str, ...]] = {
    "activity": ("SELECT", "INSERT"),
    "auth_sessions": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "guest_playground_executions": ("SELECT", "INSERT", "DELETE"),
    "lesson_progress": ("SELECT", "INSERT", "UPDATE"),
    "lessons": ("SELECT",),
    "mobile_money_payments": ("SELECT", "INSERT", "UPDATE"),
    "notifications": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "password_resets": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    "payment_attempts": ("SELECT", "INSERT", "UPDATE"),
    "payment_provider_configurations": ("SELECT",),
    "payment_status_history": ("SELECT", "INSERT"),
    "payment_transactions": ("SELECT", "INSERT", "UPDATE"),
    "playground_errors": ("SELECT", "INSERT", "UPDATE"),
    "playground_executions": ("SELECT", "INSERT", "DELETE"),
    "quiz_attempts": ("SELECT", "INSERT"),
    "quiz_results": ("SELECT", "INSERT", "UPDATE"),
    "revoked_tokens": ("SELECT", "INSERT", "DELETE"),
    "subscription_plans": ("SELECT",),
    "subscription_registrations": ("SELECT", "INSERT", "UPDATE"),
    "subscriptions": ("SELECT", "INSERT", "UPDATE"),
    "users": ("SELECT", "INSERT", "UPDATE"),
}


def assert_database_migrations_applied() -> None:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    project_root = Path(__file__).parent.parent
    config = Config(str(project_root / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError("The database migration history must have exactly one head.")
    with engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            has_alembic_access = connection.execute(text(
                "SELECT has_table_privilege(current_user, 'public.alembic_version', 'SELECT')"
            )).scalar_one()
            if not has_alembic_access:
                # The restricted runtime role intentionally has no access to Alembic metadata
                return
        current = MigrationContext.configure(connection).get_current_revision()
    if current != heads[0]:
        raise RuntimeError(
            f"Database schema revision is {current or 'uninitialized'}; "
            f"run 'alembic upgrade head' (expected {heads[0]})."
        )


def assert_database_schema_matches_models() -> None:
    from backend.db_schema import compare_schema

    with engine.connect() as connection:
        differences = compare_schema(connection, Base.metadata)
    if differences:
        raise RuntimeError(
            "The database schema does not match the application models. "
            "Review the differences and create/apply a forward-only Alembic revision: "
            + "; ".join(map(str, differences))
        )


def validate_runtime_database_role() -> None:
    integration_test = os.getenv("KITE_POSTGRES_INTEGRATION_TESTS") == "1"
    if (
        (not is_hosted_environment() and not integration_test)
        or engine.dialect.name != "postgresql"
    ):
        return
    with engine.connect() as connection:
        role = connection.execute(text(
            "SELECT current_user AS role_name, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, "
            "has_database_privilege(current_user, current_database(), 'CREATE') AS can_create_database, "
            "has_schema_privilege(current_user, 'public', 'CREATE') AS can_create_schema "
            "FROM pg_roles WHERE rolname = current_user"
        )).mappings().one()
        elevated_access = connection.execute(text(
            "SELECT "
            "EXISTS (SELECT 1 FROM pg_auth_members "
            "WHERE member = (SELECT oid FROM pg_roles WHERE rolname = current_user)) "
            "AS has_role_memberships, "
            "EXISTS (SELECT 1 FROM pg_namespace AS namespace "
            "WHERE namespace.nspname NOT IN ('information_schema', 'pg_catalog') "
            "AND namespace.nspname NOT LIKE 'pg_%' "
            "AND has_schema_privilege(current_user, namespace.oid, 'CREATE')) "
            "AS can_create_in_schema, "
            "EXISTS (SELECT 1 FROM pg_class AS relation "
            "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = 'public' "
            "AND relation.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user) "
            "AND (relation.relname = ANY(:table_names) OR relation.relname = 'alembic_version' "
            "OR (relation.relkind = 'S' AND EXISTS ("
            "SELECT 1 FROM pg_depend AS dependency "
            "JOIN pg_class AS application_table ON application_table.oid = dependency.refobjid "
            "WHERE dependency.objid = relation.oid AND dependency.deptype IN ('a', 'i') "
            "AND application_table.relname = ANY(:table_names))))) "
            "AS owns_application_objects"
        ), {"table_names": list(Base.metadata.tables)}).mappings().one()
        if (
            role["role_name"] != "kite_runtime" or role["rolsuper"] or role["rolbypassrls"]
            or role["rolcreatedb"] or role["rolcreaterole"]
            or role["can_create_database"] or role["can_create_schema"]
            or elevated_access["has_role_memberships"]
            or elevated_access["can_create_in_schema"]
            or elevated_access["owns_application_objects"]
        ):
            raise RuntimeError(
                "Hosted DATABASE_URL must use the non-superuser kite_runtime role "
                "with RLS enforcement, no elevated role memberships, no schema CREATE privileges, "
                "and no ownership of application objects."
            )
        for table in Base.metadata.sorted_tables:
            required_operations = RUNTIME_TABLE_OPERATIONS.get(table.name, ("SELECT",))
            for operation in required_operations:
                has_perm = connection.execute(text(
                    "SELECT has_table_privilege(current_user, :table_name, :operation)"
                ), {"table_name": "public." + table.name, "operation": operation}).scalar_one()
                if not has_perm:
                    raise RuntimeError(
                        f"Hosted DATABASE_URL is missing required application table permission {operation} for {table.name}."
                    )
        has_alembic_access = connection.execute(text(
            "SELECT has_table_privilege(current_user, 'public.alembic_version', 'SELECT')"
        )).scalar_one()
        if has_alembic_access:
            raise RuntimeError(
                "Hosted DATABASE_URL must not have access to Alembic metadata table alembic_version."
            )
        sequences = connection.execute(text(
            "SELECT seq.relname FROM pg_class AS seq "
            "JOIN pg_namespace AS sequence_schema ON sequence_schema.oid = seq.relnamespace "
            "JOIN pg_depend AS dependency ON dependency.objid = seq.oid "
            "AND dependency.deptype IN ('a', 'i') "
            "JOIN pg_class AS application_table ON application_table.oid = dependency.refobjid "
            "WHERE seq.relkind = 'S' AND sequence_schema.nspname = 'public' "
            "AND application_table.relname = ANY(:table_names)"
        ), {"table_names": list(Base.metadata.tables)}).scalars().all()
        for sequence_name in sequences:
            has_sequence_access = connection.execute(text(
                "SELECT has_sequence_privilege(current_user, :sequence_name, 'USAGE') "
                "AND has_sequence_privilege(current_user, :sequence_name, 'SELECT')"
            ), {"sequence_name": "public." + sequence_name}).scalar_one()
            if not has_sequence_access:
                raise RuntimeError(
                    f"Hosted DATABASE_URL is missing required sequence permissions for {sequence_name}."
                )


@app.on_event("startup")
def startup() -> None:
    validate_runtime_configuration()
    assert_database_migrations_applied()
    assert_database_schema_matches_models()
    validate_runtime_database_role()
    if not os.getenv("KITE_SECRET_KEY"):
        log.warning("KITE_SECRET_KEY is unset; authenticated requests will be unavailable.")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ready", "database": "connected"}
    except Exception as exc:
        log.warning("Database readiness probe failed: %s", exc)
        raise HTTPException(status_code=503, detail="Database connection unavailable")


@app.get("/")
def home() -> FileResponse:
    return FileResponse(Path(__file__).parent.parent / "web" / "index.html")


@app.post("/api/auth/register", status_code=201)
def register(payload: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)):
    email = str(payload.email).strip().lower()
    enforce_auth_rate_limit(request, "register", email, 5, 3600)
    require_auth_provider()
    password_error = password_strength_error(payload.password)
    if password_error:
        raise HTTPException(status_code=422, detail=password_error)
    if supabase_configured():
        redirect = os.getenv("PUBLIC_APP_URL", "http://localhost:8000").rstrip("/") + "/"
        result = supabase_request("/signup?redirect_to=" + quote(redirect, safe=""), method="POST", data={
            "email": email,
            "password": payload.password,
            "data": {"name": payload.name.strip()},
        })
        auth_user = result.get("user") or {}
        if not result.get("access_token") or not auth_user.get("id") or not email_is_verified(auth_user):
            return {"message": "If this address can be registered, check your inbox for verification instructions."}
        user = local_user_for_supabase(auth_user, db)
        create_notification(
            user, db, event_key="welcome", title="Welcome to Kite!",
            body="Your learning journey starts here.", category="updates",
            target_route="learn",
        )
        csrf = set_auth_cookies(
            request, response, db, user, result["access_token"],
            result.get("refresh_token"), payload.remember_me,
            utcnow() + timedelta(seconds=int(result.get("expires_in", 3600))),
        )
        db.commit()
        return {"user": user_payload(user, db), "csrf_token": csrf}
    token_key()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(
            status_code=409,
            detail="We couldn't create this account. If you already have one, try signing in.",
        )
    user = User(email=email, name=payload.name.strip(), password_hash=hash_password(payload.password))
    db.add(user)
    db.flush()
    create_notification(
        user, db, event_key="welcome", title="Welcome to Kite!",
        body="Your learning journey starts here.", category="updates",
        target_route="learn",
    )
    access_token, expires_at = issue_token(user, payload.remember_me)
    csrf = set_auth_cookies(
        request, response, db, user, access_token, None, payload.remember_me, expires_at,
    )
    db.commit()
    return {"user": user_payload(user, db), "csrf_token": csrf}


@app.post("/api/auth/login")
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    email = str(payload.email).strip().lower()
    enforce_auth_rate_limit(request, "login", email, 8, 900)
    require_auth_provider()
    if supabase_configured():
        result = supabase_request("/token?grant_type=password", method="POST", data={
            "email": email,
            "password": payload.password,
        })
        auth_user = result.get("user") or {}
        if not auth_user.get("id"):
            raise HTTPException(status_code=401, detail="Email or password is incorrect.")
        user = local_user_for_supabase(auth_user, db)
        csrf = set_auth_cookies(
            request, response, db, user, result["access_token"],
            result.get("refresh_token"), payload.remember_me,
            utcnow() + timedelta(seconds=int(result.get("expires_in", 3600))),
        )
        db.commit()
        return {"user": user_payload(user, db), "csrf_token": csrf}
    token_key()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    access_token, expires_at = issue_token(user, payload.remember_me)
    csrf = set_auth_cookies(
        request, response, db, user, access_token, None, payload.remember_me, expires_at,
    )
    db.commit()
    return {"user": user_payload(user, db), "csrf_token": csrf}


@app.get("/api/auth/google/authorize")
def google_auth_authorize(
    request: Request,
    code_challenge: str = Query(min_length=43, max_length=128),
):
    enforce_auth_rate_limit(request, "google", "", 10, 900)
    if not supabase_configured():
        raise HTTPException(status_code=503, detail="Google sign-in requires configured Supabase Auth and Google OAuth credentials.")
    redirect_uri = os.getenv("PUBLIC_APP_URL", "http://localhost:8000").rstrip("/") + "/"
    parameters = urlencode({
        "provider": "google",
        "redirect_to": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "s256",
    })
    return {"authorization_url": os.getenv("SUPABASE_URL", "").rstrip("/") + "/auth/v1/authorize?" + parameters}


@app.post("/api/auth/google/exchange")
def google_auth_exchange(
    payload: GoogleAuthCodeIn, request: Request, response: Response, db: Session = Depends(get_db),
):
    enforce_auth_rate_limit(request, "google-exchange", "", 10, 900)
    if not supabase_configured():
        raise HTTPException(status_code=503, detail="Google sign-in requires configured Supabase Auth and Google OAuth credentials.")
    result = supabase_request("/token?grant_type=pkce", method="POST", data={
        "auth_code": payload.code,
        "code_verifier": payload.code_verifier,
    })
    auth_user = result.get("user") or {}
    if not auth_user.get("id") or not result.get("access_token"):
        raise HTTPException(status_code=401, detail="Google sign-in could not verify your account.")
    user = local_user_for_supabase(auth_user, db)
    csrf = set_auth_cookies(
        request, response, db, user, result["access_token"], result.get("refresh_token"),
        payload.remember_me,
        utcnow() + timedelta(seconds=int(result.get("expires_in", 3600))),
    )
    db.commit()
    return {"user": user_payload(user, db), "csrf_token": csrf}


@app.post("/api/auth/session")
def verify_supabase_session(
    payload: SupabaseSessionIn, request: Request, response: Response, db: Session = Depends(get_db),
):
    if supabase_configured():
        access_token = payload.access_token or ""
        refresh_token = payload.refresh_token
        try:
            auth_user = supabase_request("/user", access_token=access_token)
        except HTTPException as error:
            if error.status_code != 401 or not refresh_token:
                raise
            refreshed = supabase_request(
                "/token?grant_type=refresh_token", method="POST",
                data={"refresh_token": refresh_token},
            )
            access_token = refreshed.get("access_token", "")
            refresh_token = refreshed.get("refresh_token") or refresh_token
            auth_user = refreshed.get("user") or {}
        if not auth_user.get("id") or not auth_user.get("email"):
            raise HTTPException(status_code=401, detail="This verification link is invalid or expired.")
        user = local_user_for_supabase(auth_user, db)
        expires_at = utcnow() + timedelta(minutes=60)
    else:
        if not payload.access_token:
            raise HTTPException(status_code=401, detail="This saved session has expired. Sign in again.")
        access_token = payload.access_token
        refresh_token = None
        user = validate_local_token(access_token, db)
        expires_at = utcnow() + (
            timedelta(days=REMEMBER_DAYS) if payload.remember_me else timedelta(hours=SESSION_HOURS)
        )
    csrf = set_auth_cookies(
        request, response, db, user, access_token, refresh_token,
        payload.remember_me, expires_at,
    )
    db.commit()
    return {"user": user_payload(user, db), "csrf_token": csrf}


@app.get("/api/auth/session")
def get_auth_session(request: Request, user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    session_id = request.cookies.get(AUTH_SESSION_COOKIE)
    session = db.get(AuthSession, session_id) if session_id else None
    return {
        "user": user_payload(user, db),
        "csrf_token": csrf_token(user.id),
        "remember_me": bool(session and session.remember_me),
    }


@app.post("/api/auth/resend-verification")
def resend_verification(payload: VerificationResendIn, request: Request):
    email = str(payload.email).strip().lower()
    enforce_auth_rate_limit(request, "verification", email, 3, 3600)
    if not supabase_configured():
        raise HTTPException(status_code=503, detail="Email verification requires configured Supabase Auth.")
    redirect = os.getenv("PUBLIC_APP_URL", "http://localhost:8000").rstrip("/") + "/"
    supabase_request("/resend?redirect_to=" + quote(redirect, safe=""), method="POST", data={
        "type": "signup",
        "email": email,
    })
    return {"message": "If the address needs verification, an email will be sent shortly."}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response,
           authorization: Annotated[str | None, Header()] = None,
           user: User = Depends(current_user), db: Session = Depends(get_db)):
    credential, using_cookie = credential_token(request, authorization)
    session_id = request.cookies.get(AUTH_SESSION_COOKIE) if using_cookie else None
    if session_id:
        session = db.get(AuthSession, session_id)
        if session and session.user_id == user.id:
            session.revoked_at = utcnow()
    if supabase_configured() and credential:
        supabase_request("/logout", method="POST", access_token=credential)
    elif credential:
        token_hash = hashlib.sha256(credential.encode()).hexdigest()
        try:
            expiry_s = credential.split(".", 3)[1]
            expires_at = datetime.fromtimestamp(int(expiry_s), timezone.utc)
        except (IndexError, ValueError):
            expires_at = utcnow() + timedelta(days=1)
        db.query(RevokedToken).filter(RevokedToken.expires_at <= utcnow()).delete(synchronize_session=False)
        if not db.get(RevokedToken, token_hash):
            db.add(RevokedToken(token_hash=token_hash, expires_at=expires_at))
    db.commit()
    if using_cookie:
        clear_auth_cookies(request, response)
    return {"ok": True}


@app.get("/api/auth/sessions")
def list_auth_sessions(request: Request, user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    now = utcnow()
    sessions = db.scalars(select(AuthSession).where(
        AuthSession.user_id == user.id,
        AuthSession.revoked_at.is_(None),
        AuthSession.expires_at > now,
    ).order_by(AuthSession.created_at.desc()))
    current_id = request.cookies.get(AUTH_SESSION_COOKIE)
    return [{
        "id": session.id,
        "current": session.id == current_id,
        "created_at": session.created_at.isoformat(),
        "last_seen_at": session.last_seen_at.isoformat(),
        "expires_at": session.expires_at.isoformat(),
        "remember_me": session.remember_me,
        "device": session.user_agent,
    } for session in sessions]


@app.post("/api/auth/sessions/{session_id}/revoke")
def revoke_auth_session(
    session_id: str, request: Request, response: Response,
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    session = db.get(AuthSession, session_id)
    if session is None or session.user_id != user.id or session.revoked_at is not None:
        raise HTTPException(status_code=404, detail="This session could not be found.")
    session.revoked_at = utcnow()
    current = request.cookies.get(AUTH_SESSION_COOKIE) == session_id
    db.commit()
    if current:
        clear_auth_cookies(request, response)
    return {"ok": True, "current": current}


@app.post("/api/auth/logout-all")
def logout_all_sessions(
    request: Request, response: Response, authorization: Annotated[str | None, Header()] = None,
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    credential, _ = credential_token(request, authorization)
    for session in db.scalars(select(AuthSession).where(
        AuthSession.user_id == user.id,
        AuthSession.revoked_at.is_(None),
    )):
        session.revoked_at = utcnow()
    user.auth_session_version += 1
    if supabase_configured() and credential:
        supabase_request("/logout?scope=global", method="POST", access_token=credential)
    db.commit()
    clear_auth_cookies(request, response)
    return {"ok": True}


@app.post("/api/auth/refresh")
def refresh_auth(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"user": user_payload(user, db), "csrf_token": csrf_token(user.id)}


@app.post("/api/auth/password-reset")
def request_password_reset(payload: ResetRequestIn, request: Request, db: Session = Depends(get_db)):
    email = str(payload.email).strip().lower()
    enforce_auth_rate_limit(request, "password-reset", email, 4, 3600)
    if supabase_configured():
        redirect = os.getenv("PUBLIC_APP_URL", "http://localhost:8000").rstrip("/") + "/#reset"
        supabase_request("/recover?redirect_to=" + quote(redirect, safe=""), method="POST", data={"email": email})
        return {"message": "If that address has an account, password reset instructions will be sent."}
    require_auth_provider()
    if not os.getenv("SMTP_HOST"):
        raise HTTPException(status_code=503, detail="Password recovery is not configured. Please try again later.")
    user = db.scalar(select(User).where(User.email == email))
    if user:
        token = secrets.token_urlsafe(32)
        reset = PasswordReset(user_id=user.id, token_hash=hashlib.sha256(token.encode()).hexdigest(),
                              expires_at=utcnow() + timedelta(minutes=30))
        db.add(reset)
        db.commit()
        try:
            send_reset_email(user.email, token)
        except Exception as exc:
            log.exception("Unable to deliver password reset email")
            raise HTTPException(status_code=503, detail="Password reset email could not be sent. Try again later.")
    return {"message": "If that address has an account, password reset instructions will be sent."}


@app.post("/api/auth/password-reset/complete")
def complete_password_reset(payload: ResetCompleteIn, db: Session = Depends(get_db)):
    password_error = password_strength_error(payload.password)
    if password_error:
        raise HTTPException(status_code=422, detail=password_error)
    if supabase_configured():
        result = supabase_request("/user", method="PUT", data={"password": payload.password},
                                  access_token=payload.token)
        return {"ok": bool(result.get("id"))}
    require_auth_provider()
    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()
    reset = db.scalar(select(PasswordReset).where(PasswordReset.token_hash == token_hash, PasswordReset.used.is_(False)))
    reset_expiry = reset.expires_at if reset else None
    if reset_expiry and reset_expiry.tzinfo is None:
        reset_expiry = reset_expiry.replace(tzinfo=timezone.utc)
    if reset is None or reset_expiry <= utcnow():
        raise HTTPException(status_code=400, detail="This reset link is invalid or expired.")
    user = db.get(User, reset.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="This reset link is invalid or expired.")
    user.password_hash = hash_password(payload.password)
    user.auth_session_version += 1
    for session in db.scalars(select(AuthSession).where(
        AuthSession.user_id == user.id,
        AuthSession.revoked_at.is_(None),
    )):
        session.revoked_at = utcnow()
    reset.used = True
    db.commit()
    return {"ok": True}


@app.get("/api/me")
def get_me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return user_payload(user, db)


@app.patch("/api/me")
def update_me(payload: ProfileIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.name = payload.name.strip()
    db.commit()
    return user_payload(user, db)


@app.patch("/api/me/email")
def update_email(payload: EmailUpdateIn, request: Request,
                 authorization: Annotated[str | None, Header()] = None,
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    email = str(payload.email).strip().lower()
    existing = db.scalar(select(User.id).where(User.email == email, User.id != user.id))
    if existing:
        raise HTTPException(status_code=409, detail="An account with that email already exists.")
    if supabase_configured():
        credential, _ = credential_token(request, authorization)
        if not credential:
            raise HTTPException(status_code=401, detail="Sign in to continue.")
        supabase_request("/user", method="PUT", data={"email": email}, access_token=credential)
        return {"message": "Check your new email address to confirm the change."}
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    user.email = email
    db.commit()
    return {"email": user.email}


@app.patch("/api/me/password")
def update_password(payload: PasswordUpdateIn, request: Request,
                    authorization: Annotated[str | None, Header()] = None,
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    password_error = password_strength_error(payload.new_password)
    if password_error:
        raise HTTPException(status_code=422, detail=password_error)
    if supabase_configured():
        credential, _ = credential_token(request, authorization)
        if not credential:
            raise HTTPException(status_code=401, detail="Sign in to continue.")
        supabase_request("/user", method="PUT", data={"password": payload.new_password},
                         access_token=credential)
        return {"ok": True}
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}


@app.patch("/api/settings")
def update_settings(payload: SettingsIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    settings = json.loads(user.settings or "{}")
    settings.update(payload.model_dump(exclude_none=True))
    user.settings = json.dumps(settings)
    db.commit()
    return settings


def lesson_payload(lesson: Lesson, progress: dict[str, LessonProgress], premium: bool) -> dict[str, Any]:
    module_id, module_lesson_order = LESSON_MODULES.get(
        lesson.id, ("python-foundations", lesson.order_num)
    )
    module = MODULE_BY_ID[module_id]
    return {
        "id": lesson.id,
        "title": lesson.title,
        "description": lesson.description,
        "order": lesson.order_num,
        "module_id": module_id,
        "module_title": module["title"],
        "module_order": module["order"],
        "module_lesson_order": module_lesson_order,
        "premium": lesson.premium,
        "locked": lesson.premium and not premium,
        "completed": bool(progress.get(lesson.id) and progress[lesson.id].completed),
        "started": bool(progress.get(lesson.id) and not progress[lesson.id].completed),
    }


@app.get("/api/modules")
def list_modules(user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    premium = bool(user and premium_entitled(db, user.id))
    progress = {}
    quiz_results = {}
    quiz_attempt_counts = {}
    if user:
        progress = {
            item.lesson_id: item
            for item in db.scalars(select(LessonProgress).where(LessonProgress.user_id == user.id))
        }
        quiz_results = {
            result.quiz_id: result
            for result in db.scalars(select(QuizResult).where(QuizResult.user_id == user.id))
        }
        quiz_attempt_counts = dict(db.execute(
            select(QuizAttempt.quiz_id, func.count(QuizAttempt.id))
            .where(QuizAttempt.user_id == user.id)
            .group_by(QuizAttempt.quiz_id)
        ).all())
    lessons = [
        lesson_payload(lesson, progress, premium)
        for lesson in db.scalars(select(Lesson).order_by(Lesson.order_num))
    ]
    modules = []
    for module in MODULES:
        module_lessons = sorted(
            (lesson for lesson in lessons if lesson["module_id"] == module["id"]),
            key=lambda lesson: lesson["module_lesson_order"],
        )
        quizzes = [
            item for item in QUIZ_METADATA + LESSON_ONLY_QUIZ_METADATA
            if item["module_id"] == module["id"]
        ]
        challenges = [item for item in CHALLENGE_METADATA if item["module_id"] == module["id"]]
        locked = bool(module_lessons) and all(lesson["locked"] for lesson in module_lessons)
        available_quizzes = [] if locked else quizzes
        module_quiz_results = [
            quiz_results[quiz["id"]]
            for quiz in available_quizzes if quiz["id"] in quiz_results
        ]
        best_percentages = [
            round(result.best_score / result.total_questions * 100)
            for result in module_quiz_results if result.total_questions
        ]
        modules.append({
            **module,
            "locked": locked,
            "completed_lessons": sum(lesson["completed"] for lesson in module_lessons),
            "lesson_count": len(module_lessons),
            "lessons": module_lessons,
            "quiz_count": len(quizzes),
            "completed_quizzes": len(module_quiz_results),
            "quiz_attempt_count": sum(
                quiz_attempt_counts.get(quiz["id"], 0) for quiz in available_quizzes
            ),
            "average_quiz_score": (
                round(sum(best_percentages) / len(best_percentages))
                if best_percentages else None
            ),
            "quizzes": [
                {
                    **item,
                    "locked": locked,
                    "completed": not locked and item["id"] in quiz_results,
                    "best_percentage": (
                        round(
                            quiz_results[item["id"]].best_score
                            / quiz_results[item["id"]].total_questions * 100
                        )
                        if not locked and item["id"] in quiz_results
                        and quiz_results[item["id"]].total_questions else None
                    ),
                    "attempt_count": (
                        quiz_attempt_counts.get(item["id"], 0) if not locked else 0
                    ),
                }
                for item in quizzes
            ],
            "challenge_count": len(challenges),
            "challenges": [{**item, "locked": locked} for item in challenges],
        })
    return modules


@app.get("/api/lessons")
def list_lessons(user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    premium = bool(user and premium_entitled(db, user.id))
    progress = {}
    if user:
        progress = {p.lesson_id: p for p in db.scalars(select(LessonProgress).where(LessonProgress.user_id == user.id))}
    return [
        lesson_payload(l, progress, premium)
        for l in db.scalars(select(Lesson).order_by(Lesson.order_num))
    ]


@app.get("/api/lessons/{lesson_id}")
def get_lesson(lesson_id: str, user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    lesson = db.get(Lesson, lesson_id)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found.")
    if lesson.premium and (not user or not premium_entitled(db, user.id)):
        raise HTTPException(status_code=402, detail="This lesson is part of Kite Premium.", headers={"X-Kite-Entitlement": "premium"})
    module_id, module_lesson_order = LESSON_MODULES.get(
        lesson.id, ("python-foundations", lesson.order_num)
    )
    return {
        "id": lesson.id, "title": lesson.title, "description": lesson.description,
        "content": lesson.content, "module_id": module_id,
        "module_title": MODULE_BY_ID[module_id]["title"],
        "module_lesson_order": module_lesson_order, "xp_reward": lesson.xp_reward,
    }


@app.post("/api/lessons/{lesson_id}/complete")
def complete_lesson(lesson_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    lesson = db.get(Lesson, lesson_id)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found.")
    if lesson.premium and not premium_entitled(db, user.id):
        raise HTTPException(status_code=402, detail="This lesson is part of Kite Premium.")
    earned_before = {
        item["id"] for item in achievement_catalog(user, db) if item["earned"]
    }
    progress = db.get(LessonProgress, (user.id, lesson.id))
    if progress is None:
        progress = LessonProgress(user_id=user.id, lesson_id=lesson.id)
        db.add(progress)
    if not progress.completed:
        progress.completed = True
        progress.score = max(progress.score or 0, 100)
        user.xp += lesson.xp_reward
        update_streak(user, db)
        db.add(Activity(user_id=user.id, kind="lesson", item_id=lesson.id, xp=lesson.xp_reward))
        create_notification(
            user, db, event_key="lesson-completed:" + lesson.id,
            title="Lesson completed: " + lesson.title,
            body="You earned " + str(lesson.xp_reward) + " XP. Your learning progress has been updated.",
            category="learning", target_route="learn",
        )
        notify_new_achievements(user, db, earned_before)
    progress.updated_at = utcnow()
    db.commit()
    return {"completed": True, "xp": user.xp, "streak": current_streak(user)}


def leaderboard_scores(period: str, users: list[User], db: Session) -> dict[int, int]:
    if period == "all":
        return {user.id: user.xp for user in users}
    start = utcnow() - timedelta(days=7 if period == "week" else 30)
    scores: dict[int, int] = {}
    for activity in db.scalars(select(Activity).where(Activity.created_at >= start)):
        scores[activity.user_id] = scores.get(activity.user_id, 0) + activity.xp
    return {user.id: scores.get(user.id, 0) for user in users}


def achievement_catalog(user: User, db: Session) -> list[dict[str, Any]]:
    published_lessons = set(db.scalars(select(Lesson.id)))
    completed = set(db.scalars(select(LessonProgress.lesson_id).where(
        LessonProgress.user_id == user.id,
        LessonProgress.completed.is_(True),
    )))
    solved = set(db.scalars(select(Activity.item_id).where(
        Activity.user_id == user.id,
        Activity.kind == "challenge",
    )))
    quiz_results = list(db.scalars(select(QuizResult).where(QuizResult.user_id == user.id)))
    passed_quiz = any(
        result.total_questions and result.best_score / result.total_questions >= 0.8
        for result in quiz_results
    )
    fixed_errors = db.scalar(select(func.count(PlaygroundError.id)).where(
        PlaygroundError.user_id == user.id,
        PlaygroundError.fixed_at.is_not(None),
    )) or 0
    users = list(db.scalars(select(User)))
    weekly_scores = leaderboard_scores("week", users, db)
    ranked_users = sorted(users, key=lambda item: (-weekly_scores.get(item.id, 0), item.created_at, item.id))
    current_rank = next((rank for rank, item in enumerate(ranked_users, 1) if item.id == user.id), None)
    final_assessment = next(
        (result for result in quiz_results if result.quiz_id == "python-final-assessment"),
        None,
    )
    criteria = [
        ("first-lesson", "First Lesson", "Complete a lesson.", bool(completed)),
        ("quiz-master", "Quiz Master", "Score at least 80% on a quiz.", passed_quiz),
        ("coding-star", "Coding Star", "Pass a coding challenge.", bool(solved)),
        ("python-pro", "Python Pro", "Complete every published lesson and challenge.",
         bool(published_lessons) and published_lessons.issubset(completed)
         and bool(CHALLENGES) and set(CHALLENGES).issubset(solved)),
        ("12-day-streak", "12-Day Streak", "Maintain a 12-day learning streak.", current_streak(user) >= 12),
        ("bug-hunter", "Bug Hunter", "Fix ten Playground errors.", fixed_errors >= 10),
        ("top-10", "Top 10", "Rank in the top ten for weekly XP.",
         weekly_scores.get(user.id, 0) > 0 and current_rank is not None and current_rank <= 10),
        ("certified", "Certified", "Pass the comprehensive final assessment.",
         bool(final_assessment and final_assessment.total_questions
              and final_assessment.best_score / final_assessment.total_questions >= 0.8)),
    ]
    return [
        {"id": key, "name": name, "description": description, "earned": earned}
        for key, name, description, earned in criteria
    ]


def notify_new_achievements(user: User, db: Session, before: set[str]) -> None:
    for achievement in achievement_catalog(user, db):
        if achievement["earned"] and achievement["id"] not in before:
            create_notification(
                user,
                db,
                event_key="achievement:" + achievement["id"],
                title="Achievement unlocked: " + achievement["name"],
                body=achievement["description"],
                category="achievements",
                target_route="progress",
            )


@app.get("/api/progress")
def progress(user: User = Depends(current_user), db: Session = Depends(get_db)):
    done = list(db.scalars(select(LessonProgress.lesson_id).where(
        LessonProgress.user_id == user.id,
        LessonProgress.completed.is_(True),
    )))
    started = list(db.scalars(select(LessonProgress.lesson_id).where(
        LessonProgress.user_id == user.id, LessonProgress.completed.is_(False)
    )))
    activity = list(db.scalars(select(Activity).where(Activity.user_id == user.id).order_by(Activity.created_at.desc()).limit(30)))
    available_quizzes = [
        item for item in QUIZ_METADATA + LESSON_ONLY_QUIZ_METADATA
        if not module_is_locked(item["module_id"], user, db)
    ]
    recommendable_quizzes = [
        item for item in available_quizzes if not item.get("lesson_only")
    ]
    available_quiz_ids = {item["id"] for item in available_quizzes}
    quiz_results = {
        item.quiz_id: item
        for item in db.scalars(select(QuizResult).where(
            QuizResult.user_id == user.id,
            QuizResult.quiz_id.in_(available_quiz_ids),
        ))
    } if available_quiz_ids else {}
    quiz_count = len(quiz_results)
    quiz_attempt_count = 0
    if available_quiz_ids:
        quiz_attempt_count = db.scalar(select(func.count(QuizAttempt.id)).where(
            QuizAttempt.user_id == user.id,
            QuizAttempt.quiz_id.in_(available_quiz_ids),
        )) or 0
    best_percentages = {
        quiz_id: round(result.best_score / result.total_questions * 100)
        for quiz_id, result in quiz_results.items() if result.total_questions
    }
    average_quiz_score = (
        round(sum(best_percentages.values()) / len(best_percentages))
        if best_percentages else None
    )
    quiz_recommendations = []
    for item in recommendable_quizzes:
        result = quiz_results.get(item["id"])
        percentage = best_percentages.get(item["id"])
        if percentage is not None and percentage >= item["pass_threshold"]:
            continue
        quiz_recommendations.append({
            "id": item["id"],
            "title": item["title"],
            "module_id": item["module_id"],
            "module_title": item["module_title"],
            "percentage": percentage,
            "reason": (
                "Review this topic and retry your assessment."
                if result else "Take this assessment to check your understanding."
            ),
        })
    quiz_recommendations.sort(
        key=lambda item: (
            0 if item["percentage"] is not None else 1,
            item["percentage"] if item["percentage"] is not None else 101,
        )
    )
    challenge_count = db.scalar(select(func.count(func.distinct(Activity.item_id))).where(
        Activity.user_id == user.id, Activity.kind == "challenge"
    )) or 0
    total_lessons = db.scalar(select(func.count(Lesson.id))) or 0
    today = utcnow().date()
    week_start = today - timedelta(days=6)
    start_at = datetime.combine(week_start, datetime.min.time(), timezone.utc)
    weekly_activity = {
        (week_start + timedelta(days=offset)).isoformat(): {"xp": 0, "activities": 0}
        for offset in range(7)
    }
    recent_activity = db.scalars(select(Activity).where(
        Activity.user_id == user.id,
        Activity.created_at >= start_at,
    ))
    for item in recent_activity:
        created_at = item.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        day = created_at.astimezone(timezone.utc).date().isoformat()
        if day in weekly_activity:
            weekly_activity[day]["xp"] += item.xp
            weekly_activity[day]["activities"] += 1
    catalog = achievement_catalog(user, db)
    achievements = [item["name"] for item in catalog if item["earned"]]
    return {"xp": user.xp, "streak": current_streak(user), "completed_lessons": done,
            "started_lessons": started,
            "activities": [{"kind": a.kind, "item_id": a.item_id, "xp": a.xp, "at": a.created_at.isoformat()} for a in activity],
            "achievements": achievements, "achievement_catalog": catalog,
            "achievement_count": len(achievements), "total_achievements": len(catalog),
            "total_lessons": total_lessons, "completion_percent":
                round(len(done) / total_lessons * 100) if total_lessons else 0,
            "quiz_count": quiz_count, "total_quizzes": len(available_quizzes),
            "quiz_attempt_count": quiz_attempt_count,
            "average_quiz_score": average_quiz_score,
            "quiz_recommendations": quiz_recommendations[:5],
            "challenge_count": challenge_count, "total_challenges": len(CHALLENGES),
            "best_streak": user.best_streak,
            "active_days_7": sum(1 for item in weekly_activity.values() if item["activities"]),
            "weekly_activity": [
                {"date": day, "weekday": datetime.fromisoformat(day).strftime("%a"), **values}
                for day, values in weekly_activity.items()
            ]}


@app.post("/api/progress/import")
def import_guest_progress(payload: GuestProgressImportIn, user: User = Depends(current_user),
                          db: Session = Depends(get_db)):
    requested_completed = set(payload.completed_lessons)
    requested_started = set(payload.started_lessons) - requested_completed
    requested_ids = requested_completed | requested_started
    if any(not lesson_id or len(lesson_id) > 60 for lesson_id in requested_ids):
        raise HTTPException(status_code=400, detail="Guest progress contains an invalid lesson ID.")

    lessons = {lesson.id: lesson for lesson in db.scalars(select(Lesson).where(Lesson.id.in_(requested_ids)))}
    if requested_ids - lessons.keys():
        raise HTTPException(status_code=404, detail="Guest progress includes an unknown lesson.")
    if any(lessons[lesson_id].premium for lesson_id in requested_ids):
        raise HTTPException(status_code=402, detail="Only free lesson progress can be imported.")

    imported_started = 0
    imported_completed = 0
    xp_awarded = 0
    imported_ids: list[str] = []
    earned_before = {
        item["id"] for item in achievement_catalog(user, db) if item["earned"]
    }
    for lesson in sorted(lessons.values(), key=lambda item: item.order_num):
        lesson_progress = db.get(LessonProgress, (user.id, lesson.id))
        if lesson.id in requested_completed:
            if lesson_progress is None:
                lesson_progress = LessonProgress(user_id=user.id, lesson_id=lesson.id)
                db.add(lesson_progress)
            if not lesson_progress.completed:
                lesson_progress.completed = True
                lesson_progress.score = max(lesson_progress.score or 0, 100)
                lesson_progress.updated_at = utcnow()
                user.xp += lesson.xp_reward
                xp_awarded += lesson.xp_reward
                imported_completed += 1
                imported_ids.append(lesson.id)
                db.add(Activity(user_id=user.id, kind="lesson", item_id=lesson.id, xp=lesson.xp_reward))
        elif lesson_progress is None:
            db.add(LessonProgress(user_id=user.id, lesson_id=lesson.id, completed=False))
            imported_started += 1
            imported_ids.append(lesson.id)

    if xp_awarded:
        update_streak(user, db)
    if imported_ids:
        import_key = hashlib.sha256(",".join(sorted(imported_ids)).encode()).hexdigest()[:32]
        create_notification(
            user, db, event_key="guest-progress-import:" + import_key,
            title="Guest progress synced",
            body=(str(imported_completed) + " completed and " + str(imported_started)
                  + " started free lessons transferred to your account."
                  + (" You earned " + str(xp_awarded) + " XP." if xp_awarded else "")),
            category="learning", target_route="learn",
        )
        notify_new_achievements(user, db, earned_before)
    db.commit()
    return {
        "imported_started": imported_started,
        "imported_completed": imported_completed,
        "xp_awarded": xp_awarded,
        "total_xp": user.xp,
    }


@app.get("/api/leaderboard")
def leaderboard(
    period: str = "all",
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
):
    if period not in {"week", "month", "all"}:
        raise HTTPException(status_code=400, detail="Choose week, month or all.")
    users = list(db.scalars(select(User)))
    scores = leaderboard_scores(period, users, db)
    ranked = sorted(users, key=lambda u: (-scores.get(u.id, 0), u.created_at, u.id))
    current_rank = next((i for i, u in enumerate(ranked, 1) if u.id == user.id), None) if user else None
    return {
        "period": period,
        "leaders": [{"rank": i, "name": u.name.split()[0], "xp": scores.get(u.id, 0)}
                    for i, u in enumerate(ranked[:10], 1)],
        "current": {"rank": current_rank, "xp": scores.get(user.id, 0)} if user else None,
    }


@app.get("/api/notifications")
def notifications(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Notice).where(Notice.user_id == user.id).order_by(Notice.created_at.desc()).limit(100))
    return [{"id": n.id, "title": n.title, "body": n.body, "category": n.category, "read": n.read,
             "target_route": n.target_route, "created_at": n.created_at.isoformat()} for n in rows]


@app.get("/api/notifications/unread-count")
def unread_notification_count(user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    if user is None:
        return {"count": 0}
    count = db.scalar(select(func.count(Notice.id)).where(
        Notice.user_id == user.id,
        Notice.read.is_(False),
    )) or 0
    return {"count": count}


@app.post("/api/notifications/read-all")
def read_all_notifications(user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.query(Notice).filter(Notice.user_id == user.id, Notice.read.is_(False)).update({"read": True})
    db.commit()
    return {"ok": True}


@app.post("/api/notifications/{notice_id}/read")
def read_notification(notice_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    notice = db.scalar(select(Notice).where(Notice.id == notice_id, Notice.user_id == user.id))
    if notice is None:
        raise HTTPException(status_code=404, detail="Notification not found.")
    notice.read = True
    db.commit()
    return {"ok": True}


@app.delete("/api/notifications")
def clear_notifications(user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.query(Notice).filter(Notice.user_id == user.id).delete()
    db.commit()
    return {"ok": True}


PAWAPAY_PROVIDERS = {
    "AIRTEL_OAPI_ZMB": "Airtel Money",
    "MTN_MOMO_ZMB": "MTN MoMo",
    "ZAMTEL_ZMB": "Zamtel Kwacha",
}


def pawapay_enabled() -> bool:
    return (
        os.getenv("KITE_PAWAPAY_ENABLED", "").lower() == "true"
        and bool(os.getenv("KITE_PAWAPAY_API_TOKEN"))
        and os.getenv("KITE_PAWAPAY_ENV", "sandbox") in {"sandbox", "production"}
    )


def mobile_money_checkout_available() -> bool:
    if not pawapay_enabled():
        return False
    try:
        pawapay_amount("premium_monthly")
        pawapay_amount("premium_annual")
    except HTTPException:
        return False
    return True


def pawapay_amount(plan_id: str) -> str:
    env_name = (
        "KITE_PAWAPAY_MONTHLY_AMOUNT" if plan_id == "premium_monthly"
        else "KITE_PAWAPAY_ANNUAL_AMOUNT"
    )
    default = "19.99" if plan_id == "premium_monthly" else "99.99"
    try:
        amount = Decimal(os.getenv(env_name, default))
    except InvalidOperation as exc:
        raise HTTPException(status_code=503, detail="Mobile-money pricing is not configured correctly.") from exc
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        raise HTTPException(status_code=503, detail="Mobile-money pricing must be positive and use at most two decimal places.")
    return format(amount, ".2f")


def pawapay_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not pawapay_enabled():
        raise HTTPException(status_code=503, detail="Mobile-money checkout is not configured.")
    base_url = (
        "https://api.pawapay.io" if os.getenv("KITE_PAWAPAY_ENV") == "production"
        else "https://api.sandbox.pawapay.io"
    )
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.request(
                method,
                base_url + path,
                headers={"Authorization": "Bearer " + os.environ["KITE_PAWAPAY_API_TOKEN"]},
                json=payload,
            )
    except httpx.HTTPError:
        log.exception("PawaPay request failed")
        raise HTTPException(status_code=503, detail="Mobile-money status could not be confirmed. Please check again.")
    if response.status_code < 200 or response.status_code >= 300:
        failure = {}
        try:
            failure = response.json().get("failureReason", {})
        except (ValueError, AttributeError):
            pass
        log.warning("PawaPay returned HTTP %s, failure code %s", response.status_code, failure.get("failureCode"))
        raise HTTPException(status_code=503, detail="Mobile-money status could not be confirmed. Please check again.")
    try:
        data = response.json()
    except ValueError as exc:
        log.warning("PawaPay returned a non-JSON response.")
        raise HTTPException(status_code=503, detail="Mobile-money status could not be confirmed. Please check again.") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=503, detail="Mobile-money status could not be confirmed. Please check again.")
    return data


def pawapay_deposit_operation(configuration: dict[str, Any], provider: str) -> dict[str, Any] | None:
    countries = configuration.get("countries")
    if not isinstance(countries, list):
        return None
    country = next(
        (item for item in countries if isinstance(item, dict) and item.get("country") == "ZMB"),
        None,
    )
    providers = country.get("providers") if isinstance(country, dict) else None
    if not isinstance(providers, list):
        return None
    gateway = next(
        (item for item in providers if isinstance(item, dict) and item.get("provider") == provider),
        None,
    )
    currencies = gateway.get("currencies") if isinstance(gateway, dict) else None
    if not isinstance(currencies, list):
        return None
    currency = next(
        (item for item in currencies if isinstance(item, dict) and item.get("currency") == "ZMW"),
        None,
    )
    operations = currency.get("operationTypes") if isinstance(currency, dict) else None
    operation = operations.get("DEPOSIT") if isinstance(operations, dict) else None
    return operation if isinstance(operation, dict) else None


def pawapay_active_provider_ids() -> list[str]:
    configuration = pawapay_request(
        "GET", "/v2/active-conf?country=ZMB&operationType=DEPOSIT",
    )
    return [
        provider for provider in PAWAPAY_PROVIDERS
        if (operation := pawapay_deposit_operation(configuration, provider))
        and operation.get("status") == "OPERATIONAL"
    ]


def pawapay_provider_config(provider: str, amount: str) -> None:
    configuration = pawapay_request(
        "GET", "/v2/active-conf?country=ZMB&operationType=DEPOSIT",
    )
    operation = pawapay_deposit_operation(configuration, provider)
    if not operation or operation.get("status") != "OPERATIONAL":
        raise HTTPException(status_code=503, detail="This mobile-money provider is not available for checkout right now.")
    payment_amount = Decimal(amount)
    if operation.get("decimalsInAmount") == "NONE" and payment_amount != payment_amount.to_integral_value():
        raise HTTPException(status_code=503, detail="This mobile-money provider does not accept decimal amounts.")
    try:
        minimum = Decimal(operation["minAmount"]) if operation.get("minAmount") is not None else None
        maximum = Decimal(operation["maxAmount"]) if operation.get("maxAmount") is not None else None
    except InvalidOperation as exc:
        raise HTTPException(status_code=503, detail="Mobile-money provider limits could not be verified.") from exc
    if (minimum is not None and payment_amount < minimum) or (maximum is not None and payment_amount > maximum):
        raise HTTPException(status_code=503, detail="This plan price is outside the provider's current transaction limits.")


def mobile_money_payment_result(payment: MobileMoneyPayment) -> dict[str, Any]:
    return {
        "payment_id": payment.id,
        "plan_id": payment.plan_id,
        "status": payment.status,
        "failure_code": payment.failure_code,
        "currency": "ZMW",
        "amount": payment.amount,
    }


def link_legacy_pawapay_payment(
    registration: SubscriptionRegistration,
    payment: MobileMoneyPayment,
    idempotency_key: str,
    db: Session,
) -> None:
    transaction = latest_payment_transaction(registration.id, db)
    if transaction is None:
        raise HTTPException(status_code=409, detail="This registration has no payment record.")
    if transaction.status in {
        PaymentStatus.FAILED.value, PaymentStatus.CANCELLED.value, PaymentStatus.EXPIRED.value,
    }:
        transaction = PaymentTransaction(
            id=str(uuid.uuid4()),
            registration_id=registration.id,
            reference="KITE-SUB-" + uuid.uuid4().hex[:20].upper(),
            amount=payment.amount,
            currency="ZMW",
            status=PaymentStatus.NOT_INITIATED.value,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        db.add(transaction)
        db.flush()
        db.add(PaymentStatusHistory(
            transaction_id=transaction.id,
            previous_status=None,
            status=PaymentStatus.NOT_INITIATED.value,
            source="pawapay-retry",
        ))
    if transaction.status in {
        PaymentStatus.PENDING.value, PaymentStatus.PROCESSING.value,
        PaymentStatus.SUCCESSFUL.value,
    } and transaction.provider_reference != payment.id:
        raise HTTPException(status_code=409, detail="Another payment is already in progress for this registration.")
    existing_attempt = db.scalar(select(PaymentAttempt).where(
        PaymentAttempt.idempotency_key == idempotency_key,
    ))
    if existing_attempt:
        if existing_attempt.transaction_id != transaction.id:
            raise HTTPException(status_code=409, detail="This payment request was already used.")
        return
    attempt_count = db.scalar(select(func.count(PaymentAttempt.id)).join(
        PaymentTransaction, PaymentTransaction.id == PaymentAttempt.transaction_id,
    ).where(PaymentTransaction.registration_id == registration.id)) or 0
    transaction.provider_id = "pawapay"
    transaction.provider_reference = payment.id
    transaction.amount = payment.amount
    transaction.currency = "ZMW"
    transaction.expires_at = utcnow() + timedelta(minutes=30)
    set_payment_status(transaction, PaymentStatus.PROCESSING, "pawapay-initiation", db)
    db.add(PaymentAttempt(
        transaction_id=transaction.id,
        attempt_number=attempt_count + 1,
        idempotency_key=idempotency_key,
        provider_id="pawapay",
        status=PaymentStatus.PROCESSING.value,
        started_at=utcnow(),
    ))
    db.commit()
    sync_legacy_pawapay_payment(payment, db)


def sync_legacy_pawapay_payment(payment: MobileMoneyPayment, db: Session) -> None:
    transaction = db.scalar(select(PaymentTransaction).where(
        PaymentTransaction.provider_id == "pawapay",
        PaymentTransaction.provider_reference == payment.id,
    ))
    if transaction is None:
        return
    attempt = db.scalar(select(PaymentAttempt).where(
        PaymentAttempt.transaction_id == transaction.id,
    ).order_by(PaymentAttempt.attempt_number.desc()).limit(1))
    if attempt:
        attempt.completed_at = utcnow()
        attempt.status = (
            PaymentStatus.SUCCESSFUL.value if payment.status == "COMPLETED"
            else PaymentStatus.FAILED.value if payment.status == "FAILED"
            else PaymentStatus.PROCESSING.value
        )
        attempt.failure_code = payment.failure_code
    registration = db.get(SubscriptionRegistration, transaction.registration_id)
    if payment.status == "COMPLETED":
        set_payment_status(transaction, PaymentStatus.SUCCESSFUL, "pawapay-verification", db)
        transaction.verified_at = utcnow()
        subscription = db.scalar(select(Subscription).where(
            Subscription.token_hash == hashlib.sha256(("pawapay:" + payment.id).encode()).hexdigest(),
        ))
        if subscription:
            transaction.subscription_id = subscription.id
            registration.status = "ACTIVE"
            registration.updated_at = utcnow()
    elif payment.status == "FAILED":
        set_payment_status(
            transaction, PaymentStatus.FAILED, "pawapay-verification", db,
            payment.failure_code,
        )
    elif payment.status == "REVIEW_REQUIRED":
        transaction.failure_code = payment.failure_code
        transaction.updated_at = utcnow()
    else:
        set_payment_status(transaction, PaymentStatus.PENDING, "pawapay-response", db)
    db.commit()


def settle_mobile_money_payment(payment: MobileMoneyPayment, details: dict[str, Any], db: Session) -> None:
    payer_details = details.get("payer", {}).get("accountDetails", {})
    try:
        details_amount = Decimal(str(details.get("amount")))
        expected_amount = Decimal(payment.amount)
    except (InvalidOperation, TypeError):
        details_amount = Decimal("-1")
        expected_amount = Decimal(payment.amount)
    verified = (
        details.get("depositId") == payment.id
        and details.get("country") == "ZMB"
        and details.get("currency") == "ZMW"
        and details.get("status") == "COMPLETED"
        and details_amount == expected_amount
        and payer_details.get("provider") == payment.provider
    )
    if not verified:
        payment.status = "REVIEW_REQUIRED"
        payment.failure_code = "PAYMENT_DETAILS_MISMATCH"
        payment.updated_at = utcnow()
        db.commit()
        sync_legacy_pawapay_payment(payment, db)
        log.error("PawaPay completion did not match payment intent %s.", payment.id)
        return

    now = utcnow()
    token_hash = hashlib.sha256(("pawapay:" + payment.id).encode()).hexdigest()
    subscription = db.scalar(select(Subscription).where(Subscription.token_hash == token_hash))
    if subscription is None:
        existing_subscriptions = db.scalars(
            select(Subscription).where(Subscription.user_id == payment.user_id)
        )
        active_mobile_expiries = []
        for item in existing_subscriptions:
            expiry = item.expires_at
            if expiry and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if (
                item.product_id.startswith("pawapay_")
                and item.state in {"SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_CANCELED"}
                and expiry and expiry > now
            ):
                active_mobile_expiries.append(expiry)
        start = max(active_mobile_expiries, default=now)
        duration = timedelta(days=30 if payment.plan_id == "premium_monthly" else 365)
        subscription = Subscription(
            user_id=payment.user_id,
            product_id="pawapay_" + payment.plan_id,
            token_hash=token_hash,
            token_ciphertext=encrypt_purchase_token(payment.id),
            state="SUBSCRIPTION_STATE_ACTIVE",
            expires_at=start + duration,
            auto_renew=False,
            updated_at=now,
        )
        db.add(subscription)
        period_name = "monthly" if payment.plan_id == "premium_monthly" else "annual"
        create_notification(
            db.get(User, payment.user_id), db,
            event_key="mobile-money-subscription:" + payment.id,
            title="Kite Premium subscription confirmed",
            body="Your " + period_name + " prepaid access is active through "
            + subscription.expires_at.astimezone(timezone.utc).date().isoformat() + ".",
            category="subscription", target_route="preferences",
        )
    payment.status = "COMPLETED"
    payment.failure_code = None
    payment.updated_at = now
    db.commit()
    sync_legacy_pawapay_payment(payment, db)


def refresh_mobile_money_payment(payment: MobileMoneyPayment, db: Session) -> None:
    result = pawapay_request("GET", "/v2/deposits/" + quote(payment.id, safe=""))
    if result.get("status") != "FOUND":
        return
    details = result.get("data")
    if not isinstance(details, dict):
        return
    remote_status = details.get("status")
    if remote_status == "COMPLETED":
        settle_mobile_money_payment(payment, details, db)
    elif remote_status == "FAILED":
        payment.status = "FAILED"
        reason = details.get("failureReason")
        payment.failure_code = reason.get("failureCode") if isinstance(reason, dict) else None
        payment.updated_at = utcnow()
        db.commit()
        sync_legacy_pawapay_payment(payment, db)


def encrypt_subscription_phone(value: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    context = b"kite-subscription-phone-v1"
    key = hashlib.sha256(token_key() + context).digest()
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(key).encrypt(nonce, value.encode(), context)
    return base64.urlsafe_b64encode(nonce + encrypted).decode()


def decrypt_subscription_phone(value: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    context = b"kite-subscription-phone-v1"
    key = hashlib.sha256(token_key() + context).digest()
    raw = base64.urlsafe_b64decode(value.encode())
    return AESGCM(key).decrypt(raw[:12], raw[12:], context).decode()


def latest_payment_transaction(registration_id: str, db: Session) -> PaymentTransaction | None:
    return db.scalar(
        select(PaymentTransaction)
        .where(PaymentTransaction.registration_id == registration_id)
        .order_by(PaymentTransaction.created_at.desc())
        .limit(1)
    )


def set_payment_status(
    payment: PaymentTransaction,
    status: PaymentStatus,
    source: str,
    db: Session,
    failure_code: str | None = None,
) -> None:
    previous = PaymentStatus(payment.status)
    if previous == status:
        payment.failure_code = failure_code
        payment.updated_at = utcnow()
        return
    transitions = {
        PaymentStatus.NOT_INITIATED: {
            PaymentStatus.PENDING, PaymentStatus.PROCESSING, PaymentStatus.FAILED,
            PaymentStatus.CANCELLED, PaymentStatus.EXPIRED,
        },
        PaymentStatus.PENDING: {
            PaymentStatus.PROCESSING, PaymentStatus.SUCCESSFUL, PaymentStatus.FAILED,
            PaymentStatus.CANCELLED, PaymentStatus.EXPIRED,
        },
        PaymentStatus.PROCESSING: {
            PaymentStatus.PENDING, PaymentStatus.SUCCESSFUL, PaymentStatus.FAILED,
            PaymentStatus.CANCELLED, PaymentStatus.EXPIRED,
        },
        PaymentStatus.FAILED: {PaymentStatus.SUCCESSFUL},
        PaymentStatus.CANCELLED: {PaymentStatus.SUCCESSFUL},
        PaymentStatus.EXPIRED: {PaymentStatus.SUCCESSFUL},
        PaymentStatus.SUCCESSFUL: set(),
    }
    if status not in transitions[previous]:
        raise HTTPException(status_code=409, detail="This payment status transition is not allowed.")
    payment.status = status.value
    payment.failure_code = failure_code
    payment.updated_at = utcnow()
    db.add(PaymentStatusHistory(
        transaction_id=payment.id,
        previous_status=previous.value,
        status=status.value,
        source=source,
    ))


def payment_transaction_payload(payment: PaymentTransaction, db: Session) -> dict[str, Any]:
    attempt_count = db.scalar(
        select(func.count(PaymentAttempt.id)).where(PaymentAttempt.transaction_id == payment.id)
    ) or 0
    return {
        "id": payment.id,
        "reference": payment.reference,
        "provider_id": payment.provider_id,
        "provider_reference": payment.provider_reference,
        "amount": payment.amount,
        "currency": payment.currency,
        "status": payment.status,
        "failure_code": payment.failure_code,
        "attempt_count": attempt_count,
        "expires_at": payment.expires_at.isoformat() if payment.expires_at else None,
        "verified_at": payment.verified_at.isoformat() if payment.verified_at else None,
    }


def subscription_registration_payload(
    registration: SubscriptionRegistration, db: Session,
) -> dict[str, Any]:
    plan = db.get(SubscriptionPlan, registration.plan_id)
    payment = latest_payment_transaction(registration.id, db)
    payment_payload = payment_transaction_payload(payment, db) if payment else None
    active_subscription = db.get(Subscription, payment.subscription_id) if payment and payment.subscription_id else None
    expires_at = ensure_utc(active_subscription.expires_at) if active_subscription and active_subscription.expires_at else None
    if registration.status == "ACTIVE" and expires_at and expires_at <= utcnow():
        registration.status = "EXPIRED"
        registration.updated_at = utcnow()
        db.commit()
    if registration.status == "ACTIVE":
        message = "Verified payment received. Your prepaid Kite Premium access is active."
    elif payment_payload and payment_payload["status"] == PaymentStatus.NOT_INITIATED.value:
        message = "Registration saved. Payment has not started, and Premium is not active."
    elif (
        payment_payload
        and payment_payload["provider_id"] in LENCO_PROVIDER_IDS
        and payment_payload["status"] in {
            PaymentStatus.PENDING.value, PaymentStatus.PROCESSING.value,
        }
    ):
        message = (
            "Lenco payment request sent. Approve it on your selected mobile-money phone; "
            "Kite will activate Premium only after server-side verification."
        )
    elif payment_payload and payment_payload["status"] == PaymentStatus.FAILED.value:
        message = "Registration saved, but the latest payment attempt failed. Premium is not active."
    elif payment_payload and payment_payload["status"] == PaymentStatus.SUCCESSFUL.value:
        message = "Payment was verified. Subscription activation is pending account association."
    else:
        message = "Registration saved. Premium starts only after payment is verified."
    return {
        "registration_id": registration.id,
        "plan": {
            "id": plan.id,
            "name": plan.name,
            "price": plan.display_price,
            "amount": plan.amount,
            "currency": plan.currency,
            "period": plan.period,
            "summary": plan.summary,
            "features": json.loads(plan.features_json or "[]"),
        } if plan else None,
        "phone_number": "••••" + registration.phone_last4,
        "subscription_status": registration.status,
        "subscription_expires_at": expires_at.isoformat() if expires_at else None,
        "renewal": "manual",
        "premium_active": bool(
            registration.user_id is not None
            and registration.status == "ACTIVE"
            and premium_entitled(db, registration.user_id)
        ),
        "payment": payment_payload,
        "message": message,
        "created_at": registration.created_at.isoformat(),
    }


def authorize_subscription_registration(
    registration: SubscriptionRegistration,
    user: User | None,
    access_key: str | None,
    db: Session,
) -> None:
    if user is not None and registration.user_id == user.id:
        return
    if not access_key or not hmac.compare_digest(
        registration.access_key_hash, hashlib.sha256(access_key.encode()).hexdigest(),
    ):
        raise HTTPException(status_code=404, detail="Subscription registration not found.")


def activate_verified_payment(
    payment: PaymentTransaction, db: Session,
) -> None:
    registration = db.get(SubscriptionRegistration, payment.registration_id)
    if registration is None or registration.user_id is None:
        log.error("Verified payment %s has no linked account.", payment.reference)
        return
    if payment.subscription_id is not None:
        registration.status = "ACTIVE"
        registration.updated_at = utcnow()
        return
    plan = db.get(SubscriptionPlan, registration.plan_id)
    if plan is None or not plan.premium:
        log.error("Verified payment %s references an invalid subscription plan.", payment.reference)
        return
    now = utcnow()
    token_hash = hashlib.sha256(("mobile-money:" + payment.id).encode()).hexdigest()
    subscription = db.scalar(select(Subscription).where(Subscription.token_hash == token_hash))
    if subscription is None:
        active_mobile_expiries = []
        for existing in db.scalars(select(Subscription).where(
            Subscription.user_id == registration.user_id,
        )):
            expiry = ensure_utc(existing.expires_at) if existing.expires_at else None
            if (
                existing.product_id.startswith(("pawapay_", "mobile_money_"))
                and existing.state in {"SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_CANCELED"}
                and expiry and expiry > now
            ):
                active_mobile_expiries.append(expiry)
        start = max(active_mobile_expiries, default=now)
        duration = timedelta(days=30 if plan.period == "month" else 365)
        subscription = Subscription(
            user_id=registration.user_id,
            product_id="mobile_money_" + registration.plan_id,
            token_hash=token_hash,
            token_ciphertext=encrypt_purchase_token(payment.id),
            state="SUBSCRIPTION_STATE_ACTIVE",
            expires_at=start + duration,
            auto_renew=False,
            updated_at=now,
        )
        db.add(subscription)
        db.flush()
    payment.subscription_id = subscription.id
    registration.status = "ACTIVE"
    registration.updated_at = now
    user = db.get(User, registration.user_id)
    if user:
        create_notification(
            user, db, event_key="mobile-money-verified:" + payment.id,
            title="Kite Premium subscription confirmed",
            body="Your prepaid " + ("monthly" if plan.period == "month" else "annual")
            + " access is active through "
            + subscription.expires_at.astimezone(timezone.utc).date().isoformat() + ".",
            category="subscription", target_route="preferences",
        )


def reconcile_payment_result(
    payment: PaymentTransaction,
    result: ProviderPaymentResult,
    source: str,
    db: Session,
) -> bool:
    locked_payment = db.scalar(
        select(PaymentTransaction)
        .where(PaymentTransaction.id == payment.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_payment is None:
        return False
    payment = locked_payment
    amounts_match = False
    try:
        actual_amount = Decimal(str(result.amount)) if result.amount is not None else None
        expected_amount = Decimal(payment.amount)
        amounts_match = bool(
            actual_amount is not None
            and actual_amount.is_finite()
            and expected_amount.is_finite()
            and actual_amount == expected_amount
        )
    except (InvalidOperation, TypeError, ValueError):
        amounts_match = False
    if (
        not result.verified
        or result.transaction_reference != payment.reference
        or not amounts_match
        or result.currency != payment.currency
    ):
        payment.failure_code = result.failure_code or "VERIFICATION_MISMATCH"
        payment.updated_at = utcnow()
        log.warning("Rejected unverified or mismatched provider result for %s.", payment.reference)
        return False
    if result.provider_reference:
        payment.provider_reference = result.provider_reference
    if result.status == PaymentStatus.SUCCESSFUL:
        set_payment_status(payment, PaymentStatus.SUCCESSFUL, source, db)
        payment.verified_at = utcnow()
        activate_verified_payment(payment, db)
    elif result.status in {
        PaymentStatus.PENDING, PaymentStatus.PROCESSING, PaymentStatus.FAILED,
        PaymentStatus.CANCELLED, PaymentStatus.EXPIRED,
    }:
        set_payment_status(payment, result.status, source, db, result.failure_code)
    return True


@app.get("/api/plans")
def plans(db: Session = Depends(get_db)):
    mobile_money_enabled = mobile_money_checkout_available()
    providers = []
    mobile_money_message = None
    if mobile_money_enabled:
        try:
            active_providers = pawapay_active_provider_ids()
        except HTTPException:
            log.warning("PawaPay provider availability could not be loaded.")
            mobile_money_message = "Mobile-money provider availability could not be checked. Try again later."
        else:
            providers = [
                {"id": provider, "name": PAWAPAY_PROVIDERS[provider]}
                for provider in active_providers
            ]
            if not providers:
                mobile_money_message = "No Zambia mobile-money providers are currently enabled and operational on this merchant account."
    configured_payment_providers = []
    for config in db.scalars(select(PaymentProviderConfiguration).where(
        PaymentProviderConfiguration.enabled.is_(True),
    )):
        provider = get_mobile_money_provider(config.provider_id)
        if provider and is_payment_provider_available(provider):
            configured_payment_providers.append({
                "id": provider.provider_id,
                "name": provider.display_name,
            })
    plan_rows = db.scalars(select(SubscriptionPlan).where(
        SubscriptionPlan.active.is_(True),
    ).order_by(SubscriptionPlan.sort_order))
    plan_payloads = []
    for plan in plan_rows:
        price = plan.display_price
        if plan.id == "premium_monthly":
            price = os.getenv("KITE_MONTHLY_PRICE", price)
        elif plan.id == "premium_annual":
            price = os.getenv("KITE_ANNUAL_PRICE", price)
        item = {
            "id": plan.id,
            "name": plan.name,
            "price": price,
            "amount": plan.amount,
            "currency": plan.currency,
            "period": plan.period,
            "premium": plan.premium,
            "summary": plan.summary,
            "features": json.loads(plan.features_json or "[]"),
        }
        if plan.id == "premium_monthly":
            item["product_id"] = os.getenv("KITE_PLAY_MONTHLY_PRODUCT_ID", "kite_premium_monthly")
        elif plan.id == "premium_annual":
            item.update({
                "product_id": os.getenv("KITE_PLAY_ANNUAL_PRODUCT_ID", "kite_premium_annual"),
                "savings": "K139.89 annually",
                "percentage_savings": "58.3%",
                "best_value": True,
            })
        plan_payloads.append(item)
    return {
        "plans": plan_payloads,
        "payment_providers": configured_payment_providers,
        "mobile_money": {
            "enabled": mobile_money_enabled and bool(providers),
            "currency": "ZMW",
            "renewal": "manual",
            "providers": providers,
            "message": mobile_money_message,
        },
    }


@app.get("/api/subscription")
def subscription(user: User = Depends(current_user), db: Session = Depends(get_db)):
    registration = db.scalar(select(SubscriptionRegistration).where(
        SubscriptionRegistration.user_id == user.id,
    ).order_by(SubscriptionRegistration.created_at.desc()).limit(1))
    registration_payload = (
        subscription_registration_payload(registration, db) if registration else None
    )
    sub = db.scalar(select(Subscription).where(Subscription.user_id == user.id).order_by(Subscription.updated_at.desc()))
    if not sub:
        return {"plan": "free", "status": "none", "premium": False, "cancel_url": None,
                "billing_provider": None, "registration": registration_payload}
    mobile_money = sub.product_id.startswith(("pawapay_", "mobile_money_"))
    return {"plan": "premium", "status": sub.state, "premium": premium_entitled(db, user.id),
            "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
            "auto_renew": sub.auto_renew,
            "cancel_url": None if mobile_money else "https://play.google.com/store/account/subscriptions",
            "billing_provider": "mobile_money" if mobile_money else "google_play",
            "registration": registration_payload}


@app.post("/api/subscription/registrations", status_code=201)
def create_subscription_registration(
    payload: SubscriptionRegistrationIn,
    request: Request,
    response: Response,
    user: User | None = Depends(optional_user),
    db: Session = Depends(get_db),
):
    enforce_auth_rate_limit(request, "subscription-registration", "", 10, 3600)
    try:
        phone_number = normalize_zambian_phone(payload.phone_number)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    plan = db.get(SubscriptionPlan, payload.plan_id)
    if plan is None or not plan.active or not plan.premium:
        raise HTTPException(status_code=422, detail="Choose an available Premium plan.")
    secret = token_key()
    request_digest = hmac.new(
        secret,
        ((str(user.id) if user else "guest") + "|" + payload.plan_id + "|" + phone_number).encode(),
        hashlib.sha256,
    ).hexdigest()
    phone_digest = hmac.new(
        secret, b"kite-phone-lookup-v1|" + phone_number.encode(), hashlib.sha256,
    ).hexdigest()
    access_key_hash = hashlib.sha256(payload.access_key.encode()).hexdigest()
    registration = db.scalar(select(SubscriptionRegistration).where(
        SubscriptionRegistration.idempotency_key == payload.idempotency_key,
    ))
    if registration:
        if (
            not hmac.compare_digest(registration.request_hash, request_digest)
            or not hmac.compare_digest(registration.access_key_hash, access_key_hash)
        ):
            raise HTTPException(status_code=409, detail="This registration request was already used for different details.")
        if registration.user_id != (user.id if user else None):
            raise HTTPException(status_code=409, detail="This registration belongs to a different account.")
        response.status_code = 200
        return subscription_registration_payload(registration, db)

    now = utcnow()
    registration = SubscriptionRegistration(
        id=str(uuid.uuid4()),
        user_id=user.id if user else None,
        plan_id=plan.id,
        idempotency_key=payload.idempotency_key,
        request_hash=request_digest,
        access_key_hash=access_key_hash,
        phone_ciphertext=encrypt_subscription_phone(phone_number),
        phone_hash=phone_digest,
        phone_last4=phone_number[-4:],
        status="PENDING_PAYMENT",
        created_at=now,
        updated_at=now,
    )
    payment = PaymentTransaction(
        id=str(uuid.uuid4()),
        registration_id=registration.id,
        reference="KITE-SUB-" + uuid.uuid4().hex[:20].upper(),
        amount=plan.amount,
        currency=plan.currency,
        status=PaymentStatus.NOT_INITIATED.value,
        created_at=now,
        updated_at=now,
    )
    db.add_all([registration, payment])
    db.flush()
    db.add(PaymentStatusHistory(
        transaction_id=payment.id,
        previous_status=None,
        status=PaymentStatus.NOT_INITIATED.value,
        source="registration",
    ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(SubscriptionRegistration).where(
            SubscriptionRegistration.idempotency_key == payload.idempotency_key,
        ))
        if (
            existing is None
            or not hmac.compare_digest(existing.request_hash, request_digest)
            or not hmac.compare_digest(existing.access_key_hash, access_key_hash)
        ):
            raise HTTPException(status_code=409, detail="This registration could not be created safely.")
        response.status_code = 200
        return subscription_registration_payload(existing, db)
    response.status_code = 201
    return subscription_registration_payload(registration, db)


@app.get("/api/subscription/registrations/{registration_id}")
def get_subscription_registration(
    registration_id: str,
    user: User | None = Depends(optional_user),
    access_key: Annotated[str | None, Header(alias="X-Kite-Registration-Key")] = None,
    db: Session = Depends(get_db),
):
    registration = db.get(SubscriptionRegistration, registration_id)
    if registration is None:
        raise HTTPException(status_code=404, detail="Subscription registration not found.")
    authorize_subscription_registration(registration, user, access_key, db)
    return subscription_registration_payload(registration, db)


@app.post("/api/subscription/registrations/{registration_id}/claim")
def claim_subscription_registration(
    registration_id: str,
    payload: SubscriptionRegistrationClaimIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    registration = db.scalar(
        select(SubscriptionRegistration)
        .where(SubscriptionRegistration.id == registration_id)
        .with_for_update()
    )
    if registration is None or not hmac.compare_digest(
        registration.access_key_hash, hashlib.sha256(payload.access_key.encode()).hexdigest(),
    ):
        raise HTTPException(status_code=404, detail="Subscription registration not found.")
    if registration.user_id not in {None, user.id}:
        raise HTTPException(status_code=409, detail="This registration is already associated with another account.")
    registration.user_id = user.id
    registration.updated_at = utcnow()
    db.commit()
    return subscription_registration_payload(registration, db)


@app.post("/api/subscription/registrations/{registration_id}/cancel")
def cancel_subscription_registration(
    registration_id: str,
    user: User | None = Depends(optional_user),
    access_key: Annotated[str | None, Header(alias="X-Kite-Registration-Key")] = None,
    db: Session = Depends(get_db),
):
    registration = db.scalar(
        select(SubscriptionRegistration)
        .where(SubscriptionRegistration.id == registration_id)
        .with_for_update()
    )
    if registration is None:
        raise HTTPException(status_code=404, detail="Subscription registration not found.")
    authorize_subscription_registration(registration, user, access_key, db)
    if registration.status == "ACTIVE":
        raise HTTPException(
            status_code=409,
            detail="This prepaid subscription does not auto-renew. Its verified access remains active until expiry.",
        )
    payment = latest_payment_transaction(registration.id, db)
    if payment and payment.status in {
        PaymentStatus.PENDING.value, PaymentStatus.PROCESSING.value,
        PaymentStatus.SUCCESSFUL.value,
    }:
        raise HTTPException(
            status_code=409,
            detail="A payment is in progress or already verified. Refresh its status before taking further action.",
        )
    if payment and payment.status == PaymentStatus.NOT_INITIATED.value:
        set_payment_status(payment, PaymentStatus.CANCELLED, "registration-cancelled", db)
    registration.status = "CANCELLED"
    registration.updated_at = utcnow()
    db.commit()
    return subscription_registration_payload(registration, db)


@app.post("/api/subscription/registrations/{registration_id}/payments")
def initiate_subscription_payment(
    registration_id: str,
    payload: PaymentInitiationIn,
    user: User | None = Depends(optional_user),
    access_key: Annotated[str | None, Header(alias="X-Kite-Registration-Key")] = None,
    db: Session = Depends(get_db),
):
    registration = db.scalar(
        select(SubscriptionRegistration)
        .where(SubscriptionRegistration.id == registration_id)
        .with_for_update()
    )
    if registration is None:
        raise HTTPException(status_code=404, detail="Subscription registration not found.")
    authorize_subscription_registration(registration, user, access_key, db)
    if registration.status in {"CANCELLED", "FAILED", "EXPIRED"}:
        raise HTTPException(
            status_code=409,
            detail="This subscription registration is no longer available for payment.",
        )

    duplicate_attempt = db.scalar(select(PaymentAttempt).where(
        PaymentAttempt.idempotency_key == payload.idempotency_key,
    ))
    if duplicate_attempt:
        payment = db.get(PaymentTransaction, duplicate_attempt.transaction_id)
        if payment is None or payment.registration_id != registration.id:
            raise HTTPException(status_code=409, detail="This payment request was already used.")
        return subscription_registration_payload(registration, db)

    provider_config = db.get(PaymentProviderConfiguration, payload.provider_id)
    provider = get_mobile_money_provider(payload.provider_id)
    if (
        not provider_config or not provider_config.enabled or provider is None
        or not is_payment_provider_available(provider)
    ):
        return {
            **subscription_registration_payload(registration, db),
            "message": "Registration is saved. Payment has not started because this payment provider is not available for checkout.",
        }
    if registration.user_id is None:
        raise HTTPException(
            status_code=409,
            detail="Sign in and link this registration before starting payment so Premium can be associated with your account.",
        )
    now = utcnow()
    existing_subscriptions = db.scalars(select(Subscription).where(
        Subscription.user_id == registration.user_id,
    ))
    for existing in existing_subscriptions:
        expiry = ensure_utc(existing.expires_at) if existing.expires_at else None
        if (
            not existing.product_id.startswith(("pawapay_", "mobile_money_"))
            and existing.state in {
                "SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
                "SUBSCRIPTION_STATE_CANCELED",
            }
            and expiry and expiry > now
        ):
            raise HTTPException(
                status_code=409,
                detail="An active Google Play subscription already exists. Manage it through Google Play.",
            )
    if registration.status == "ACTIVE":
        raise HTTPException(status_code=409, detail="This subscription registration is already active.")

    payment = latest_payment_transaction(registration.id, db)
    if payment is None:
        raise HTTPException(status_code=409, detail="The registration has no payment record.")
    if payment.status in {
        PaymentStatus.PENDING.value, PaymentStatus.PROCESSING.value,
        PaymentStatus.SUCCESSFUL.value,
    }:
        raise HTTPException(status_code=409, detail="A payment is already in progress or has been verified.")
    if payment.status in {
        PaymentStatus.FAILED.value, PaymentStatus.CANCELLED.value, PaymentStatus.EXPIRED.value,
    }:
        now = utcnow()
        payment = PaymentTransaction(
            id=str(uuid.uuid4()),
            registration_id=registration.id,
            reference="KITE-SUB-" + uuid.uuid4().hex[:20].upper(),
            amount=payment.amount,
            currency=payment.currency,
            status=PaymentStatus.NOT_INITIATED.value,
            created_at=now,
            updated_at=now,
        )
        db.add(payment)
        db.flush()
        db.add(PaymentStatusHistory(
            transaction_id=payment.id,
            previous_status=None,
            status=PaymentStatus.NOT_INITIATED.value,
            source="retry",
        ))
    number = decrypt_subscription_phone(registration.phone_ciphertext)
    now = utcnow()
    attempt_count = db.scalar(select(func.count(PaymentAttempt.id)).join(
        PaymentTransaction, PaymentTransaction.id == PaymentAttempt.transaction_id,
    ).where(PaymentTransaction.registration_id == registration.id)) or 0
    attempt = PaymentAttempt(
        transaction_id=payment.id,
        attempt_number=attempt_count + 1,
        idempotency_key=payload.idempotency_key,
        provider_id=provider.provider_id,
        status=PaymentStatus.PROCESSING.value,
        started_at=now,
    )
    payment.provider_id = provider.provider_id
    payment.expires_at = now + timedelta(minutes=30)
    set_payment_status(payment, PaymentStatus.PROCESSING, "initiation", db)
    db.add(attempt)
    db.commit()

    try:
        result = provider.initiate(payment.reference, payment.amount, payment.currency, number)
    except PaymentProviderError:
        log.exception("Payment provider initiation failed for reference %s.", payment.reference)
        result = ProviderPaymentResult(
            status=PaymentStatus.PENDING,
            failure_code="PROVIDER_RESPONSE_UNKNOWN",
        )
    payment.provider_reference = result.provider_reference or payment.provider_reference
    attempt.completed_at = utcnow()
    attempt.status = result.status.value if result.status != PaymentStatus.SUCCESSFUL else PaymentStatus.PROCESSING.value
    attempt.failure_code = result.failure_code
    if result.status == PaymentStatus.SUCCESSFUL:
        set_payment_status(payment, PaymentStatus.PROCESSING, "initiation", db)
    elif result.status in {
        PaymentStatus.PENDING, PaymentStatus.PROCESSING, PaymentStatus.FAILED,
        PaymentStatus.CANCELLED, PaymentStatus.EXPIRED,
    }:
        set_payment_status(payment, result.status, "initiation", db, result.failure_code)
    db.commit()
    return subscription_registration_payload(registration, db)


@app.post("/api/subscription/registrations/{registration_id}/verify")
def verify_subscription_payment(
    registration_id: str,
    user: User | None = Depends(optional_user),
    access_key: Annotated[str | None, Header(alias="X-Kite-Registration-Key")] = None,
    db: Session = Depends(get_db),
):
    registration = db.get(SubscriptionRegistration, registration_id)
    if registration is None:
        raise HTTPException(status_code=404, detail="Subscription registration not found.")
    authorize_subscription_registration(registration, user, access_key, db)
    payment = latest_payment_transaction(registration.id, db)
    if payment is None or not payment.provider_id:
        return subscription_registration_payload(registration, db)
    provider_config = db.get(PaymentProviderConfiguration, payment.provider_id)
    provider = get_mobile_money_provider(payment.provider_id)
    if (
        not provider_config or not provider_config.enabled or provider is None
        or not is_payment_provider_available(provider)
    ):
        return subscription_registration_payload(registration, db)
    try:
        result = provider.verify(payment.reference, payment.provider_reference)
    except PaymentProviderError:
        log.warning("Payment provider verification could not be completed.")
        raise HTTPException(
            status_code=503,
            detail="Payment status could not be confirmed. Please check again.",
        ) from None
    reconcile_payment_result(payment, result, "verification", db)
    db.commit()
    return subscription_registration_payload(registration, db)


@app.post("/api/payment-webhooks/{provider_id}")
async def receive_payment_webhook(provider_id: str, request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    payment = None
    configured_provider_id = provider_id
    if provider_id == "lenco":
        if not verify_lenco_webhook_signature(
            body, request.headers, os.getenv("KITE_LENCO_API_TOKEN"),
        ):
            raise HTTPException(status_code=400, detail="Payment webhook signature is invalid.")
        try:
            event = json.loads(body)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Payment webhook payload is invalid.") from None
        if not isinstance(event, dict):
            raise HTTPException(status_code=400, detail="Payment webhook payload is invalid.")
        if event.get("event") not in LENCO_COLLECTION_WEBHOOK_EVENTS:
            return {"received": True, "ignored": True}
        event_data = event.get("data")
        reference = event_data.get("reference") if isinstance(event_data, dict) else None
        if not isinstance(reference, str) or not reference:
            raise HTTPException(status_code=400, detail="Payment webhook reference is invalid.")
        payment = db.scalar(select(PaymentTransaction).where(
            PaymentTransaction.reference == reference,
            PaymentTransaction.provider_id.in_(LENCO_PROVIDER_IDS),
        ))
        if payment is None:
            return {"received": True, "ignored": True}
        configured_provider_id = payment.provider_id
    provider_config = db.get(PaymentProviderConfiguration, configured_provider_id)
    provider = get_mobile_money_provider(configured_provider_id)
    if (
        not provider_config or not provider_config.enabled or provider is None
        or not is_payment_provider_available(provider)
    ):
        raise HTTPException(status_code=404, detail="Payment provider webhook is not configured.")
    try:
        result = provider.verify_webhook(body, request.headers)
    except PaymentProviderError:
        log.warning("Payment webhook transaction could not be independently verified.")
        raise HTTPException(
            status_code=503,
            detail="Payment status could not be confirmed. The provider may retry this webhook.",
        ) from None
    if result.failure_code == "IGNORED_EVENT":
        return {"received": True, "ignored": True}
    if not result.verified or not result.transaction_reference:
        raise HTTPException(status_code=400, detail="Payment webhook signature or transaction is invalid.")
    if payment is None:
        payment = db.scalar(select(PaymentTransaction).where(
            PaymentTransaction.reference == result.transaction_reference,
            PaymentTransaction.provider_id == configured_provider_id,
        ))
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment transaction not found.")
    reconcile_payment_result(payment, result, "webhook", db)
    db.commit()
    return {"received": True, "status": payment.status}


@app.post("/api/subscription/mobile-money")
def create_mobile_money_payment(
    payload: MobileMoneyPaymentIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not mobile_money_checkout_available():
        raise HTTPException(status_code=503, detail="Mobile-money checkout is not configured.")
    amount = pawapay_amount(payload.plan_id)
    registration = None
    if payload.registration_id:
        registration = db.get(SubscriptionRegistration, payload.registration_id)
        if registration is None:
            raise HTTPException(status_code=404, detail="Subscription registration not found.")
        authorize_subscription_registration(registration, user, payload.registration_key, db)
        if registration.user_id not in {None, user.id}:
            raise HTTPException(status_code=409, detail="This registration belongs to another account.")
        if registration.plan_id != payload.plan_id:
            raise HTTPException(status_code=409, detail="The selected plan does not match this registration.")
        phone_number = decrypt_subscription_phone(registration.phone_ciphertext)
        if payload.phone_number:
            try:
                supplied_phone = normalize_zambian_phone(payload.phone_number)
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            if not hmac.compare_digest(supplied_phone, phone_number):
                raise HTTPException(status_code=409, detail="The mobile number does not match this registration.")
        registration.user_id = user.id
        registration.updated_at = utcnow()
    else:
        if not payload.phone_number:
            raise HTTPException(status_code=422, detail="Enter a Zambian mobile-money number.")
        try:
            phone_number = normalize_zambian_phone(payload.phone_number)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    phone_number = phone_number.lstrip("+")
    request_digest = hmac.new(
        token_key(),
        (payload.plan_id + "|" + payload.provider + "|" + phone_number + "|" +
         (registration.id if registration else "")).encode(),
        hashlib.sha256,
    ).hexdigest()
    payment = db.scalar(select(MobileMoneyPayment).where(
        MobileMoneyPayment.user_id == user.id,
        MobileMoneyPayment.idempotency_key == payload.idempotency_key,
    ))
    if payment and not hmac.compare_digest(payment.request_hash, request_digest):
        raise HTTPException(status_code=409, detail="This payment request was already used for different details.")
    if payment and payment.status in {"COMPLETED", "FAILED", "REVIEW_REQUIRED"}:
        if registration:
            link_legacy_pawapay_payment(registration, payment, payload.idempotency_key, db)
        return mobile_money_payment_result(payment)

    if payment is None:
        subscriptions = db.scalars(select(Subscription).where(Subscription.user_id == user.id))
        now = utcnow()
        for item in subscriptions:
            expiry = item.expires_at
            if expiry and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if (
                not item.product_id.startswith("pawapay_")
                and item.state in {"SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD", "SUBSCRIPTION_STATE_CANCELED"}
                and expiry and expiry > now
            ):
                raise HTTPException(
                    status_code=409,
                    detail="A Google Play subscription is already active. Manage it through Google Play.",
                )
        payment = MobileMoneyPayment(
            id=str(uuid.uuid4()),
            user_id=user.id,
            idempotency_key=payload.idempotency_key,
            request_hash=request_digest,
            plan_id=payload.plan_id,
            amount=amount,
            provider=payload.provider,
            status="INITIATING",
            created_at=now,
            updated_at=now,
        )
        db.add(payment)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            payment = db.scalar(select(MobileMoneyPayment).where(
                MobileMoneyPayment.user_id == user.id,
                MobileMoneyPayment.idempotency_key == payload.idempotency_key,
            ))
            if payment is None or not hmac.compare_digest(payment.request_hash, request_digest):
                raise HTTPException(status_code=409, detail="This payment request could not be created safely.")
            if payment.status in {"COMPLETED", "FAILED", "REVIEW_REQUIRED"}:
                if registration:
                    link_legacy_pawapay_payment(registration, payment, payload.idempotency_key, db)
                return mobile_money_payment_result(payment)

    if registration:
        link_legacy_pawapay_payment(registration, payment, payload.idempotency_key, db)
    pawapay_provider_config(payment.provider, payment.amount)
    try:
        result = pawapay_request("POST", "/v2/deposits", {
            "depositId": payment.id,
            "amount": payment.amount,
            "currency": "ZMW",
            "payer": {
                "type": "MMO",
                "accountDetails": {
                    "phoneNumber": phone_number,
                    "provider": payment.provider,
                },
            },
        })
    except HTTPException:
        payment.status = "PENDING"
        payment.updated_at = utcnow()
        db.commit()
        if registration:
            link_legacy_pawapay_payment(registration, payment, payload.idempotency_key, db)
        return {
            **mobile_money_payment_result(payment),
            "message": "Payment submission is unconfirmed. Check its status before trying again.",
        }

    status = result.get("status")
    if result.get("depositId") not in {None, payment.id}:
        payment.status = "REVIEW_REQUIRED"
        payment.failure_code = "PAYMENT_REFERENCE_MISMATCH"
    elif status == "REJECTED":
        payment.status = "FAILED"
        reason = result.get("failureReason")
        payment.failure_code = reason.get("failureCode") if isinstance(reason, dict) else None
    elif status in {"ACCEPTED", "DUPLICATE_IGNORED"}:
        payment.status = "PENDING"
        payment.failure_code = None
    else:
        payment.status = "PENDING"
        payment.failure_code = None
    payment.updated_at = utcnow()
    db.commit()
    if registration:
        sync_legacy_pawapay_payment(payment, db)
    return mobile_money_payment_result(payment)


@app.get("/api/subscription/mobile-money/{payment_id}")
def get_mobile_money_payment(
    payment_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    payment = db.get(MobileMoneyPayment, payment_id)
    if payment is None or payment.user_id != user.id:
        raise HTTPException(status_code=404, detail="Payment not found.")
    if payment.status in {"INITIATING", "PENDING"}:
        refresh_mobile_money_payment(payment, db)
    result = mobile_money_payment_result(payment)
    result["premium"] = premium_entitled(db, user.id)
    if payment.status == "COMPLETED":
        subscription = db.scalar(select(Subscription).where(
            Subscription.token_hash == hashlib.sha256(("pawapay:" + payment.id).encode()).hexdigest(),
        ))
        result["expires_at"] = subscription.expires_at.isoformat() if subscription and subscription.expires_at else None
    return result


def fetch_google_purchase(product_id: str, purchase_token: str) -> dict[str, Any]:
    package_name = os.getenv("GOOGLE_PLAY_PACKAGE_NAME")
    credentials_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not package_name or not credentials_file:
        raise HTTPException(status_code=503, detail="Google Play purchase verification is not configured.")
    allowed = {
        os.getenv("KITE_PLAY_MONTHLY_PRODUCT_ID", "kite_premium_monthly"),
        os.getenv("KITE_PLAY_ANNUAL_PRODUCT_ID", "kite_premium_annual"),
    }
    if product_id not in allowed:
        raise HTTPException(status_code=400, detail="This Google Play product is not configured for Kite.")
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            credentials_file, scopes=["https://www.googleapis.com/auth/androidpublisher"]
        )
        client = AuthorizedSession(credentials)
        url = f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{quote(package_name, safe='')}/purchases/subscriptionsv2/tokens/{quote(purchase_token, safe='')}"
        response = client.get(url, timeout=15)
    except Exception as exc:
        log.exception("Google Play verification request failed")
        raise HTTPException(status_code=502, detail="Google Play could not verify the purchase right now.")
    if response.status_code != 200:
        log.warning("Google Play returned verification status %s", response.status_code)
        raise HTTPException(status_code=400, detail="Google Play did not verify this purchase.")
    result = response.json()
    line_items = result.get("lineItems", [])
    item = next((i for i in line_items if i.get("productId") == product_id), None)
    if item is None:
        raise HTTPException(status_code=400, detail="Purchase product does not match the selected Kite plan.")
    state = result.get("subscriptionState", "SUBSCRIPTION_STATE_UNSPECIFIED")
    expiry_value = item.get("expiryTime")
    try:
        expires_at = datetime.fromisoformat(expiry_value.replace("Z", "+00:00")) if expiry_value else None
    except (ValueError, AttributeError):
        expires_at = None
    return {"state": state, "expires_at": expires_at,
            "auto_renew": item.get("autoRenewingPlan", {}).get("autoRenewEnabled", False)}


@app.post("/api/subscription/google/verify")
def verify_subscription(payload: GooglePurchaseIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    token_hash = hashlib.sha256(payload.purchase_token.encode()).hexdigest()
    existing = db.scalar(select(Subscription).where(Subscription.token_hash == token_hash))
    if existing and existing.user_id != user.id:
        raise HTTPException(status_code=409, detail="This purchase is already associated with another account.")
    verified = fetch_google_purchase(payload.product_id, payload.purchase_token)
    expiry = verified["expires_at"]
    if verified["state"] not in {"SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD", "SUBSCRIPTION_STATE_CANCELED"} or not expiry or expiry <= utcnow():
        raise HTTPException(status_code=402, detail="This Google Play subscription is not currently active.")
    previous_expiry = existing.expires_at if existing else None
    was_active = bool(existing and premium_entitled(db, user.id))
    sub = existing or Subscription(user_id=user.id, product_id=payload.product_id, token_hash=token_hash,
                                   token_ciphertext=encrypt_purchase_token(payload.purchase_token),
                                   state=verified["state"])
    sub.product_id = payload.product_id
    sub.token_ciphertext = encrypt_purchase_token(payload.purchase_token)
    sub.state = verified["state"]
    sub.expires_at = verified["expires_at"]
    sub.auto_renew = verified["auto_renew"]
    sub.updated_at = utcnow()
    db.add(sub)
    if existing is None or not same_instant(previous_expiry, expiry):
        expiry_key = str(int(expiry.timestamp()))
        create_notification(
            user, db,
            event_key="subscription:" + token_hash[:48] + ":" + expiry_key,
            title="Kite Premium subscription confirmed",
            body="Your " + ("monthly" if "monthly" in payload.product_id else "annual")
            + " subscription is active through " + expiry.astimezone(timezone.utc).date().isoformat() + ".",
            category="subscription", target_route="preferences",
        )
    elif not was_active and verified["state"] == "SUBSCRIPTION_STATE_ACTIVE":
        create_notification(
            user, db,
            event_key="subscription-reactivated:" + token_hash[:64],
            title="Kite Premium is active again",
            body="Your verified subscription has restored premium access.",
            category="subscription", target_route="preferences",
        )
    db.commit()
    return {"plan": "premium", "premium": True, "status": sub.state, "expires_at": sub.expires_at.isoformat(),
            "auto_renew": sub.auto_renew}


@app.post("/api/subscription/refresh")
def refresh_subscription(user: User = Depends(current_user), db: Session = Depends(get_db)):
    sub = db.scalar(select(Subscription).where(Subscription.user_id == user.id).order_by(Subscription.updated_at.desc()))
    if not sub:
        return {"plan": "free", "premium": False, "status": "none"}
    if sub.product_id.startswith("pawapay_"):
        expiry = ensure_utc(sub.expires_at) if sub.expires_at else None
        was_active = premium_entitled(db, user.id)
        if expiry and expiry <= utcnow() and sub.state == "SUBSCRIPTION_STATE_ACTIVE":
            sub.state = "SUBSCRIPTION_STATE_EXPIRED"
            sub.updated_at = utcnow()
            if was_active:
                create_notification(
                    user, db,
                    event_key="mobile-money-expired:" + str(sub.id),
                    title="Kite Premium has expired",
                    body="Your prepaid Premium access has ended. Renew from Settings whenever you are ready.",
                    category="subscription", target_route="preferences",
                )
            db.commit()
        return {
            "plan": "premium" if premium_entitled(db, user.id) else "free",
            "premium": premium_entitled(db, user.id),
            "status": sub.state,
            "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
            "auto_renew": False,
            "cancel_url": None,
            "billing_provider": "mobile_money",
        }
    try:
        purchase_token = decrypt_purchase_token(sub.token_ciphertext)
    except Exception:
        log.exception("Unable to decrypt stored Google Play purchase token")
        raise HTTPException(status_code=503, detail="Subscription status could not be refreshed.")
    verified = fetch_google_purchase(sub.product_id, purchase_token)
    previous_state = sub.state
    previous_expiry = sub.expires_at
    sub.state = verified["state"]
    sub.expires_at = verified["expires_at"]
    sub.auto_renew = verified["auto_renew"]
    sub.updated_at = utcnow()
    expiry_changed = not same_instant(previous_expiry, sub.expires_at)
    if previous_state != sub.state or expiry_changed:
        if sub.state == "SUBSCRIPTION_STATE_EXPIRED":
            title, body = "Kite Premium has expired", "Your premium access has ended. You can renew from Settings."
        elif sub.state == "SUBSCRIPTION_STATE_CANCELED":
            title, body = "Subscription renewal canceled", "Premium access remains available until the current paid period ends."
        elif expiry_changed:
            title, body = "Kite Premium renewed", "Your verified subscription period has been updated."
        else:
            title, body = "Kite Premium status updated", "Your subscription status was updated by Google Play."
        event_suffix = (sub.state + ":" + (sub.expires_at.isoformat() if sub.expires_at else "none"))
        create_notification(
            user, db,
            event_key="subscription-refresh:" + str(sub.id) + ":" + hashlib.sha256(event_suffix.encode()).hexdigest()[:32],
            title=title, body=body, category="subscription", target_route="preferences",
        )
    db.commit()
    return {"plan": "premium" if premium_entitled(db, user.id) else "free",
            "premium": premium_entitled(db, user.id), "status": sub.state,
            "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
            "auto_renew": sub.auto_renew,
            "cancel_url": "https://play.google.com/store/account/subscriptions"}


async def call_runner(code: str, challenge_id: str | None = None,
                      execution_id: str | None = None) -> dict[str, Any]:
    runner_url = os.getenv("RUNNER_URL", "http://runner:8010")
    secret = os.getenv("RUNNER_SHARED_SECRET", "")
    if not secret:
        raise HTTPException(status_code=503, detail="The isolated Python runner is not configured.")
    try:
        payload: dict[str, str] = {"code": code}
        if execution_id:
            payload["execution_id"] = execution_id
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.post(f"{runner_url}/run", json=payload,
                                         headers={"X-Kite-Runner-Key": secret})
    except httpx.HTTPError:
        log.exception("Isolated runner request failed")
        raise HTTPException(status_code=503, detail="The isolated Python runner is unavailable.")
    if response.status_code == 503:
        raise HTTPException(status_code=503, detail="Python execution is temporarily unavailable.")
    if response.status_code != 200:
        log.error("Isolated Python runner returned HTTP %s", response.status_code)
        raise HTTPException(status_code=502, detail="The isolated Python runner returned an error.")
    try:
        result = response.json()
    except ValueError as exc:
        log.error("Isolated Python runner returned invalid JSON")
        raise HTTPException(status_code=502, detail="The isolated Python runner returned an invalid response.") from exc
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("stdout"), str)
        or not isinstance(result.get("stderr"), str)
        or not (result.get("error") is None or isinstance(result.get("error"), str))
    ):
        log.error("Isolated Python runner returned an invalid response shape")
        raise HTTPException(status_code=502, detail="The isolated Python runner returned an invalid response.")
    return result


async def cancel_runner_execution(execution_id: str) -> dict[str, bool]:
    runner_url = os.getenv("RUNNER_URL", "http://runner:8010")
    secret = os.getenv("RUNNER_SHARED_SECRET", "")
    if not secret:
        raise HTTPException(status_code=503, detail="The isolated Python runner is not configured.")
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            response = await client.post(
                f"{runner_url}/cancel/{execution_id}",
                headers={"X-Kite-Runner-Key": secret},
            )
    except httpx.HTTPError:
        log.exception("Isolated runner cancellation request failed")
        raise HTTPException(status_code=503, detail="The isolated Python runner is unavailable.")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="The isolated Python runner rejected cancellation.")
    return response.json()


@app.post("/api/execution/run")
async def run_code(payload: RunIn, user: User | None = Depends(playground_user), db: Session = Depends(get_db)):
    execution = None
    guest_execution = None
    if user and payload.execution_id:
        if db.get(PlaygroundExecution, payload.execution_id):
            raise HTTPException(status_code=409, detail="Execution ID is already in use.")
        execution = PlaygroundExecution(execution_id=payload.execution_id, user_id=user.id)
        db.add(execution)
        db.commit()
    elif user is None:
        if not payload.execution_id or not payload.guest_token:
            raise HTTPException(status_code=422, detail="A guest execution ID and token are required.")
        db.query(GuestPlaygroundExecution).filter(
            GuestPlaygroundExecution.expires_at <= utcnow(),
        ).delete(synchronize_session=False)
        db.commit()
        if db.get(PlaygroundExecution, payload.execution_id) or db.get(
            GuestPlaygroundExecution, payload.execution_id,
        ):
            raise HTTPException(status_code=409, detail="Execution ID is already in use.")
        guest_execution = GuestPlaygroundExecution(
            execution_id=payload.execution_id,
            token_hash=hashlib.sha256(payload.guest_token.encode()).hexdigest(),
            expires_at=utcnow() + timedelta(seconds=30),
        )
        db.add(guest_execution)
        db.commit()
    try:
        result = await call_runner(payload.code, execution_id=payload.execution_id)
    finally:
        if execution:
            db.delete(execution)
            db.commit()
        if guest_execution:
            db.delete(guest_execution)
            db.commit()
    if result.get("error") and result["error"] != "Execution cancelled.":
        if user:
            db.add(PlaygroundError(user_id=user.id))
            db.commit()
    elif not result.get("error"):
        if user:
            pending_error = db.scalar(select(PlaygroundError).where(
                PlaygroundError.user_id == user.id, PlaygroundError.fixed_at.is_(None)
            ).order_by(PlaygroundError.created_at, PlaygroundError.id).limit(1))
            if pending_error:
                pending_error.fixed_at = utcnow()
                db.commit()
    return result


@app.post("/api/execution/cancel")
async def cancel_code(payload: CancelRunIn, user: User | None = Depends(playground_user), db: Session = Depends(get_db)):
    execution = db.get(PlaygroundExecution, payload.execution_id)
    if execution is not None:
        if user is None or execution.user_id != user.id:
            raise HTTPException(status_code=404, detail="Active execution not found.")
        return await cancel_runner_execution(payload.execution_id)
    guest_execution = db.get(GuestPlaygroundExecution, payload.execution_id)
    supplied_hash = hashlib.sha256((payload.guest_token or "").encode()).hexdigest()
    if (
        guest_execution is None
        or ensure_utc(guest_execution.expires_at) <= utcnow()
        or not hmac.compare_digest(guest_execution.token_hash, supplied_hash)
    ):
        raise HTTPException(status_code=404, detail="Active execution not found.")
    return await cancel_runner_execution(payload.execution_id)


def module_is_locked(module_id: str, user: User | None, db: Session) -> bool:
    if user and premium_entitled(db, user.id):
        return False
    module_lessons = [
        row for row in LESSONS if LESSON_MODULES.get(row[0], ("python-foundations", 0))[0] == module_id
    ]
    return bool(module_lessons) and all(row[5] for row in module_lessons)


@app.get("/api/challenges")
def list_challenges(user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    return [
        {
            **item,
            "module_title": MODULE_BY_ID[item["module_id"]]["title"],
            "locked": module_is_locked(item["module_id"], user, db),
        }
        for item in CHALLENGE_METADATA
    ]


@app.post("/api/challenges/{challenge_id}/submit")
async def submit_challenge(challenge_id: str, payload: ChallengeIn, user: User | None = Depends(optional_user),
                           db: Session = Depends(get_db)):
    challenge = CHALLENGES.get(challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="Challenge not found.")
    metadata = CHALLENGE_METADATA_BY_ID[challenge_id]
    if module_is_locked(metadata["module_id"], user, db):
        raise HTTPException(status_code=402, detail="This challenge is part of Kite Premium.")
    results = []
    for prelude, expected in challenge["tests"]:
        code = (prelude + "\n" if prelude else "") + payload.code
        result = await call_runner(code)
        results.append({
            "passed": result.get("error") is None and result.get("stdout", "").strip() == expected,
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "error": result.get("error"),
        })
    passed = all(result["passed"] for result in results)
    awarded = 0
    if user:
        challenge_done = db.scalar(select(Activity.id).where(
            Activity.user_id == user.id,
            Activity.kind == "challenge",
            Activity.item_id == challenge_id,
        )) is not None
        if passed and not challenge_done:
            earned_before = {
                item["id"] for item in achievement_catalog(user, db) if item["earned"]
            }
            awarded = challenge["xp"]
            user.xp += awarded
            update_streak(user, db)
            db.add(Activity(user_id=user.id, kind="challenge", item_id=challenge_id, xp=awarded))
            create_notification(
                user, db, event_key="challenge-completed:" + challenge_id,
                title="Challenge completed",
                body="You passed every test and earned " + str(awarded) + " XP.",
                category="learning", target_route="challenge",
            )
            notify_new_achievements(user, db, earned_before)
            db.commit()
    return {
        "passed": passed,
        "results": results,
        "guest": user is None,
        "xp_awarded": awarded,
        "total_xp": user.xp if user else None,
    }


@app.get("/api/quizzes")
def list_quizzes(lesson_id: str | None = Query(default=None),
                 user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
    results = {}
    attempt_counts = {}
    attempts_by_quiz: dict[str, list[QuizAttempt]] = {}
    if user:
        results = {
            result.quiz_id: result
            for result in db.scalars(select(QuizResult).where(QuizResult.user_id == user.id))
        }
        attempt_counts = dict(db.execute(
            select(QuizAttempt.quiz_id, func.count(QuizAttempt.id))
            .where(QuizAttempt.user_id == user.id)
            .group_by(QuizAttempt.quiz_id)
        ).all())
        recent_attempts = db.scalars(
            select(QuizAttempt)
            .where(QuizAttempt.user_id == user.id)
            .order_by(QuizAttempt.created_at.desc(), QuizAttempt.id.desc())
            .limit(250)
        )
        for attempt in recent_attempts:
            quiz_attempts = attempts_by_quiz.setdefault(attempt.quiz_id, [])
            if len(quiz_attempts) < 5:
                quiz_attempts.append(attempt)

    metadata_items = QUIZ_METADATA
    if lesson_id is not None:
        if lesson_id not in LESSON_MODULES:
            raise HTTPException(status_code=404, detail="Lesson not found.")
        metadata_items = QUIZ_METADATA + LESSON_ONLY_QUIZ_METADATA
        metadata_items = [
            item for item in metadata_items if lesson_id in item["lesson_ids"]
        ]

    catalog = []
    for item in metadata_items:
        result = results.get(item["id"])
        attempts = attempts_by_quiz.get(item["id"], [])
        latest_attempt = attempts[0] if attempts else None
        catalog.append({
            **item,
            "locked": module_is_locked(item["module_id"], user, db),
            "completed": result is not None,
            "best_score": result.best_score if result else None,
            "best_total": result.total_questions if result else None,
            "best_percentage": round(result.best_score / result.total_questions * 100) if result and result.total_questions else None,
            "attempt_count": attempt_counts.get(item["id"], 0),
            "last_attempt": (
                latest_attempt.created_at.isoformat() if latest_attempt else
                result.updated_at.isoformat() if result else None
            ),
            "attempts": [
                {
                    "score": attempt.score,
                    "total": attempt.total_questions,
                    "percentage": attempt.percentage,
                    "passed": attempt.passed,
                    "created_at": attempt.created_at.isoformat(),
                    "feedback": json.loads(attempt.feedback_json),
                }
                for attempt in attempts
            ],
        })
    return catalog


def validate_quiz_lesson_scope(metadata: dict[str, Any], lesson_id: str | None) -> None:
    lesson_ids = metadata["lesson_ids"]
    if metadata.get("lesson_only") and lesson_id not in lesson_ids:
        raise HTTPException(status_code=404, detail="Quiz not found.")
    if lesson_id is not None and lesson_id not in lesson_ids:
        raise HTTPException(status_code=404, detail="Quiz not found.")


@app.get("/api/quizzes/{quiz_id}")
def get_quiz(quiz_id: str, user: User | None = Depends(optional_user),
             db: Session = Depends(get_db), lesson_id: str | None = Query(default=None)):
    questions = quiz_questions(quiz_id)
    metadata = QUIZ_METADATA_BY_ID.get(quiz_id)
    if questions is None or metadata is None:
        raise HTTPException(status_code=404, detail="Quiz not found.")
    validate_quiz_lesson_scope(metadata, lesson_id)
    if module_is_locked(metadata["module_id"], user, db):
        raise HTTPException(status_code=402, detail="This quiz is part of Kite Premium.")
    return {
        "id": quiz_id,
        "title": metadata["title"],
        "topic": metadata["topic"],
        "module_id": metadata["module_id"],
        "module_title": metadata["module_title"],
        "difficulty": metadata["difficulty"],
        "pass_threshold": metadata["pass_threshold"],
        "lesson_ids": metadata["lesson_ids"],
        "practice_challenge_id": metadata["practice_challenge_id"],
        "questions": [
            {
                "question": q["question"],
                "type": q.get("type", "multiple_choice"),
                "options": q["options"],
                **({"code": q["code"]} if q.get("code") else {}),
            }
            for q in questions
        ],
    }


@app.post("/api/quizzes/{quiz_id}/submit")
def submit_quiz(quiz_id: str, payload: QuizIn, user: User | None = Depends(optional_user),
                db: Session = Depends(get_db), lesson_id: str | None = Query(default=None)):
    questions = quiz_questions(quiz_id)
    metadata = QUIZ_METADATA_BY_ID.get(quiz_id)
    if questions is None or metadata is None:
        raise HTTPException(status_code=404, detail="Quiz not found.")
    validate_quiz_lesson_scope(metadata, lesson_id)
    if module_is_locked(metadata["module_id"], user, db):
        raise HTTPException(status_code=402, detail="This quiz is part of Kite Premium.")
    if len(payload.answers) != len(questions):
        raise HTTPException(status_code=400, detail="Submit one answer for every question.")
    if any(answer < -1 or answer >= len(question["options"])
           for answer, question in zip(payload.answers, questions)):
        raise HTTPException(status_code=400, detail="One or more selected answers are not valid.")
    request_hash = hashlib.sha256(
        json.dumps(payload.answers, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if user and payload.submission_id:
        existing_attempt = db.scalar(select(QuizAttempt).where(
            QuizAttempt.user_id == user.id,
            QuizAttempt.quiz_id == quiz_id,
            QuizAttempt.submission_id == payload.submission_id,
        ))
        if existing_attempt:
            if existing_attempt.request_hash != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail="This submission has already been saved with different answers.",
                )
            return json.loads(existing_attempt.result_json)

    score = sum(1 for answer, question in zip(payload.answers, questions) if answer == question["answer"])
    total = len(questions)
    percentage = round(score / total * 100)
    passed = percentage >= metadata["pass_threshold"]
    feedback = [
        {
            "question": question["question"],
            "type": question.get("type", "multiple_choice"),
            "your_answer": question["options"][answer] if answer >= 0 else None,
            "correct": answer == question["answer"],
            "correct_option": question["options"][question["answer"]],
            "explanation": question.get(
                "explanation", "Compare the selected answer with the concept taught in the lesson."
            ),
        }
        for answer, question in zip(payload.answers, questions)
    ]
    awarded = 0
    attempt_number = None
    if user:
        xp = score * 5
        quiz_result = db.get(QuizResult, (user.id, quiz_id))
        is_best = quiz_result is None or score > quiz_result.best_score
        has_award = db.scalar(select(Activity.id).where(
            Activity.user_id == user.id, Activity.kind == "quiz", Activity.item_id == quiz_id
        ).limit(1))
        awarded = xp if xp and not has_award else 0
        earned_before = {
            item["id"] for item in achievement_catalog(user, db) if item["earned"]
        }
        if quiz_result is None:
            quiz_result = QuizResult(
                user_id=user.id,
                quiz_id=quiz_id,
                best_score=score,
                total_questions=total,
                updated_at=utcnow(),
            )
        elif score > quiz_result.best_score:
            quiz_result.best_score = score
            quiz_result.total_questions = total
            quiz_result.updated_at = utcnow()
        db.add(quiz_result)
        attempt_number = int(db.scalar(
            select(func.count(QuizAttempt.id)).where(
                QuizAttempt.user_id == user.id, QuizAttempt.quiz_id == quiz_id
            )
        ) or 0) + 1
        attempt = QuizAttempt(
            user_id=user.id,
            quiz_id=quiz_id,
            score=score,
            total_questions=total,
            percentage=percentage,
            passed=passed,
            feedback_json=json.dumps(feedback),
            submission_id=payload.submission_id,
            request_hash=request_hash if payload.submission_id else None,
            created_at=utcnow(),
        )
        db.add(attempt)
        if awarded:
            user.xp += awarded
            db.add(Activity(user_id=user.id, kind="quiz", item_id=quiz_id, xp=awarded))
        update_streak(user, db)
    result = {
        "score": score,
        "total": total,
        "percentage": percentage,
        "passed": passed,
        "pass_threshold": metadata["pass_threshold"],
        "attempt_number": attempt_number,
        "xp_awarded": awarded,
        "total_xp": user.xp if user else None,
        "guest": user is None,
        "submission_id": payload.submission_id,
        "feedback": feedback,
        "review_lesson_ids": metadata["lesson_ids"],
        "practice_challenge_id": metadata["practice_challenge_id"],
    }
    if user:
        attempt.result_json = json.dumps(result, separators=(",", ":"))
        event_key = "quiz-attempt:" + quiz_id + ":" + (
            payload.submission_id or secrets.token_hex(16)
        )
        create_notification(
            user, db,
            event_key=event_key,
            title="Quiz result saved",
            body="You scored " + str(score) + "/" + str(total) + " (" + str(percentage)
            + "%) and " + ("passed." if passed else "did not pass.")
            + ((" You earned " + str(awarded) + " XP.") if awarded else ""),
            category="learning", target_route="quiz",
        )
        notify_new_achievements(user, db, earned_before)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if user and payload.submission_id:
            existing_attempt = db.scalar(select(QuizAttempt).where(
                QuizAttempt.user_id == user.id,
                QuizAttempt.quiz_id == quiz_id,
                QuizAttempt.submission_id == payload.submission_id,
            ))
            if existing_attempt:
                if existing_attempt.request_hash != request_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="This submission has already been saved with different answers.",
                    )
                return json.loads(existing_attempt.result_json)
        raise
    return result


@app.exception_handler(Exception)
async def log_unhandled(request: Request, exc: Exception):
    log.exception("Unhandled server error for %s %s", request.method, request.url.path, exc_info=exc)
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=500, content={"detail": "An unexpected server error occurred."})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
