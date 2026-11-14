from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import smtplib
import time
import base64
from threading import Lock
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, func, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.exc import IntegrityError

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
)


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("kite")
auth_attempts: dict[str, list[float]] = {}
auth_attempts_lock = Lock()


class Base(DeclarativeBase):
    pass


def database_url() -> str:
    value = os.getenv("DATABASE_URL", "sqlite:///./kite.db")
    if value.startswith("postgres://"):
        value = "postgresql+psycopg://" + value[len("postgres://") :]
    elif value.startswith("postgresql://"):
        value = "postgresql+psycopg://" + value[len("postgresql://") :]
    return value


DB_URL = database_url()
engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class RevokedToken(Base):
    __tablename__ = "revoked_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


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


class QuizResult(Base):
    __tablename__ = "quiz_results"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    quiz_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    best_score: Mapped[int] = mapped_column(Integer)
    total_questions: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"
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
QUIZ_METADATA_BY_ID = {item["id"]: item for item in QUIZ_METADATA}
CHALLENGE_METADATA_BY_ID = {item["id"]: item for item in CHALLENGE_METADATA}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
        raise HTTPException(status_code=503, detail="Authentication is not configured.")
    if len(secret) < 32:
        raise HTTPException(status_code=503, detail="KITE_SECRET_KEY must contain at least 32 characters.")
    return secret.encode()


def supabase_configured() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_ANON_KEY"))


def enforce_auth_rate_limit(
    request: Request,
    action: str,
    identity: str,
    limit: int,
    window_seconds: int,
) -> None:
    client_host = request.client.host if request.client else "unknown"
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


def issue_token(user_id: int) -> str:
    payload = f"{user_id}.{int(time.time()) + 60 * 60 * 24 * 30}"
    signature = hmac.new(token_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    if supabase_configured():
        auth_user = supabase_request("/user", access_token=authorization[7:])
        if not auth_user.get("id") or not auth_user.get("email"):
            raise HTTPException(status_code=401, detail="Your session has expired. Sign in again.")
        user = local_user_for_supabase(auth_user, db)
        db.commit()
        return user
    try:
        user_s, expiry_s, signature = authorization[7:].split(".", 2)
        payload = f"{user_s}.{expiry_s}"
        expected = hmac.new(token_key(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected) or int(expiry_s) <= int(time.time()):
            raise ValueError
        user_id = int(user_s)
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Your session has expired. Sign in again.")
    token_hash = hashlib.sha256(authorization[7:].encode()).hexdigest()
    if db.get(RevokedToken, token_hash):
        raise HTTPException(status_code=401, detail="Your session has been signed out.")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Account not found.")
    return user


def optional_user(
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> User | None:
    if not authorization:
        return None
    return current_user(authorization, db)


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


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


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


class CancelRunIn(BaseModel):
    execution_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")


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


class GoogleAuthCodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    code_verifier: str = Field(min_length=43, max_length=128)


class SupabaseSessionIn(BaseModel):
    access_token: str = Field(min_length=1, max_length=4096)
    refresh_token: str | None = Field(default=None, max_length=4096)


class VerificationResendIn(BaseModel):
    email: EmailStr


app = FastAPI(title="Kite API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv(
        "KITE_CORS_ORIGINS", "http://localhost,https://localhost,capacitor://localhost"
    ).split(",") if origin.strip()],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
app.mount("/assets", StaticFiles(directory=Path(__file__).parent.parent / "web" / "assets"), name="assets")


@app.on_event("startup")
def startup() -> None:
    if bool(os.getenv("SUPABASE_URL")) != bool(os.getenv("SUPABASE_ANON_KEY")):
        raise RuntimeError("Set both SUPABASE_URL and SUPABASE_ANON_KEY, or leave both empty.")
    Base.metadata.create_all(engine)
    notification_columns = {column["name"] for column in inspect(engine).get_columns("notifications")}
    quiz_attempt_columns = {column["name"] for column in inspect(engine).get_columns("quiz_attempts")}
    with engine.begin() as connection:
        if "event_key" not in notification_columns:
            connection.execute(text("ALTER TABLE notifications ADD COLUMN event_key VARCHAR(160)"))
        if "target_route" not in notification_columns:
            connection.execute(text("ALTER TABLE notifications ADD COLUMN target_route VARCHAR(40)"))
        if "submission_id" not in quiz_attempt_columns:
            connection.execute(text("ALTER TABLE quiz_attempts ADD COLUMN submission_id VARCHAR(64)"))
        if "request_hash" not in quiz_attempt_columns:
            connection.execute(text("ALTER TABLE quiz_attempts ADD COLUMN request_hash VARCHAR(64)"))
        if "result_json" not in quiz_attempt_columns:
            connection.execute(text("ALTER TABLE quiz_attempts ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}'"))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_notifications_user_event "
            "ON notifications (user_id, event_key)"
        ))
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_quiz_attempts_submission "
            "ON quiz_attempts (user_id, quiz_id, submission_id) WHERE submission_id IS NOT NULL"
        ))
    with SessionLocal() as db:
        for row in LESSONS:
            lesson = db.get(Lesson, row[0])
            if lesson is None:
                db.add(Lesson(id=row[0], title=row[1], description=row[2], content=row[3],
                              order_num=row[4], premium=row[5], xp_reward=row[6]))
            else:
                lesson.title = row[1]
                lesson.description = row[2]
                lesson.content = row[3]
        db.commit()
    if not os.getenv("KITE_SECRET_KEY"):
        log.warning("KITE_SECRET_KEY is unset; authenticated requests will be unavailable.")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def home() -> FileResponse:
    return FileResponse(Path(__file__).parent.parent / "web" / "index.html")


@app.post("/api/auth/register", status_code=201)
def register(payload: RegisterIn, request: Request, db: Session = Depends(get_db)):
    email = str(payload.email).strip().lower()
    enforce_auth_rate_limit(request, "register", email, 5, 3600)
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
        db.commit()
        return {"access_token": result["access_token"], "refresh_token": result.get("refresh_token"),
                "token_type": result.get("token_type", "bearer"), "user": user_payload(user, db)}
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
    db.commit()
    return {"access_token": issue_token(user.id), "token_type": "bearer", "user": user_payload(user, db)}


@app.post("/api/auth/login")
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)):
    email = str(payload.email).strip().lower()
    enforce_auth_rate_limit(request, "login", email, 8, 900)
    if supabase_configured():
        result = supabase_request("/token?grant_type=password", method="POST", data={
            "email": email,
            "password": payload.password,
        })
        auth_user = result.get("user") or {}
        if not auth_user.get("id"):
            raise HTTPException(status_code=401, detail="Email or password is incorrect.")
        user = local_user_for_supabase(auth_user, db)
        db.commit()
        return {"access_token": result["access_token"], "refresh_token": result.get("refresh_token"),
                "token_type": result.get("token_type", "bearer"), "user": user_payload(user, db)}
    token_key()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    db.commit()
    return {"access_token": issue_token(user.id), "token_type": "bearer", "user": user_payload(user, db)}


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
def google_auth_exchange(payload: GoogleAuthCodeIn, request: Request, db: Session = Depends(get_db)):
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
    db.commit()
    return {"access_token": result["access_token"], "refresh_token": result.get("refresh_token"),
            "token_type": result.get("token_type", "bearer"), "user": user_payload(user, db)}


@app.post("/api/auth/session")
def verify_supabase_session(payload: SupabaseSessionIn, db: Session = Depends(get_db)):
    if not supabase_configured():
        raise HTTPException(status_code=503, detail="Provider sign-in is not configured.")
    auth_user = supabase_request("/user", access_token=payload.access_token)
    if not auth_user.get("id") or not auth_user.get("email"):
        raise HTTPException(status_code=401, detail="This verification link is invalid or expired.")
    user = local_user_for_supabase(auth_user, db)
    db.commit()
    return {
        "access_token": payload.access_token,
        "refresh_token": payload.refresh_token,
        "token_type": "bearer",
        "user": user_payload(user, db),
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
def logout(authorization: Annotated[str | None, Header()] = None,
           _: User = Depends(current_user), db: Session = Depends(get_db)):
    if supabase_configured() and authorization:
        supabase_request("/logout", method="POST", access_token=authorization[7:])
    elif authorization:
        token = authorization[7:]
        expiry = int(token.split(".", 2)[1])
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        db.query(RevokedToken).filter(RevokedToken.expires_at <= utcnow()).delete(synchronize_session=False)
        db.add(RevokedToken(
            token_hash=token_hash,
            expires_at=datetime.fromtimestamp(expiry, timezone.utc),
        ))
        db.commit()
    return {"ok": True}


@app.post("/api/auth/refresh")
def refresh_auth(payload: dict[str, str], db: Session = Depends(get_db)):
    refresh_token = payload.get("refresh_token", "")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Sign in again to continue.")
    if supabase_configured():
        result = supabase_request("/token?grant_type=refresh_token", method="POST",
                                  data={"refresh_token": refresh_token})
        auth_user = result.get("user") or {}
        user = local_user_for_supabase(auth_user, db)
        db.commit()
        return {"access_token": result["access_token"], "refresh_token": result.get("refresh_token"),
                "user": user_payload(user, db)}
    raise HTTPException(status_code=401, detail="Sign in again to continue.")


@app.post("/api/auth/password-reset")
def request_password_reset(payload: ResetRequestIn, request: Request, db: Session = Depends(get_db)):
    email = str(payload.email).strip().lower()
    enforce_auth_rate_limit(request, "password-reset", email, 4, 3600)
    if supabase_configured():
        redirect = os.getenv("PUBLIC_APP_URL", "http://localhost:8000").rstrip("/") + "/#reset"
        supabase_request("/recover?redirect_to=" + quote(redirect, safe=""), method="POST", data={"email": email})
        return {"message": "If that address has an account, password reset instructions will be sent."}
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
def update_email(payload: EmailUpdateIn, authorization: Annotated[str | None, Header()] = None,
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    email = str(payload.email).strip().lower()
    existing = db.scalar(select(User.id).where(User.email == email, User.id != user.id))
    if existing:
        raise HTTPException(status_code=409, detail="An account with that email already exists.")
    if supabase_configured():
        if not authorization:
            raise HTTPException(status_code=401, detail="Sign in to continue.")
        supabase_request("/user", method="PUT", data={"email": email}, access_token=authorization[7:])
        return {"message": "Check your new email address to confirm the change."}
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    user.email = email
    db.commit()
    return {"email": user.email}


@app.patch("/api/me/password")
def update_password(payload: PasswordUpdateIn, authorization: Annotated[str | None, Header()] = None,
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    password_error = password_strength_error(payload.new_password)
    if password_error:
        raise HTTPException(status_code=422, detail=password_error)
    if supabase_configured():
        if not authorization:
            raise HTTPException(status_code=401, detail="Sign in to continue.")
        supabase_request("/user", method="PUT", data={"password": payload.new_password},
                         access_token=authorization[7:])
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
        quizzes = [item for item in QUIZ_METADATA if item["module_id"] == module["id"]]
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
        item for item in QUIZ_METADATA
        if not module_is_locked(item["module_id"], user, db)
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
    for item in available_quizzes:
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
def leaderboard(period: str = "all", user: User = Depends(current_user), db: Session = Depends(get_db)):
    if period not in {"week", "month", "all"}:
        raise HTTPException(status_code=400, detail="Choose week, month or all.")
    users = list(db.scalars(select(User)))
    scores = leaderboard_scores(period, users, db)
    ranked = sorted(users, key=lambda u: (-scores.get(u.id, 0), u.created_at, u.id))
    current_rank = next((i for i, u in enumerate(ranked, 1) if u.id == user.id), None)
    return {
        "period": period,
        "leaders": [{"rank": i, "name": u.name.split()[0], "xp": scores.get(u.id, 0)}
                    for i, u in enumerate(ranked[:10], 1)],
        "current": {"rank": current_rank, "xp": scores.get(user.id, 0)},
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


@app.get("/api/plans")
def plans():
    return {
        "plans": [
            {"id": "free", "name": "Kite Free", "price": "Free", "period": None, "premium": False},
            {"id": "premium_monthly", "name": "Kite Premium", "price": os.getenv("KITE_MONTHLY_PRICE", "K19.99"), "period": "month", "premium": True,
             "product_id": os.getenv("KITE_PLAY_MONTHLY_PRODUCT_ID", "kite_premium_monthly")},
            {"id": "premium_annual", "name": "Kite Premium", "price": os.getenv("KITE_ANNUAL_PRICE", "K99.99"), "period": "year", "premium": True,
             "savings": "K139.89 annually", "percentage_savings": "58.3%", "best_value": True,
             "product_id": os.getenv("KITE_PLAY_ANNUAL_PRODUCT_ID", "kite_premium_annual")},
        ]
    }


@app.get("/api/subscription")
def subscription(user: User = Depends(current_user), db: Session = Depends(get_db)):
    sub = db.scalar(select(Subscription).where(Subscription.user_id == user.id).order_by(Subscription.updated_at.desc()))
    if not sub:
        return {"plan": "free", "status": "none", "premium": False, "cancel_url": None}
    return {"plan": "premium", "status": sub.state, "premium": premium_entitled(db, user.id),
            "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
            "auto_renew": sub.auto_renew,
            "cancel_url": "https://play.google.com/store/account/subscriptions"}


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
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="The isolated Python runner returned an error.")
    return response.json()


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
async def run_code(payload: RunIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    execution = None
    if payload.execution_id:
        if db.get(PlaygroundExecution, payload.execution_id):
            raise HTTPException(status_code=409, detail="Execution ID is already in use.")
        execution = PlaygroundExecution(execution_id=payload.execution_id, user_id=user.id)
        db.add(execution)
        db.commit()
    try:
        result = await call_runner(payload.code, execution_id=payload.execution_id)
    finally:
        if execution:
            db.delete(execution)
            db.commit()
    if result.get("error") and result["error"] != "Execution cancelled.":
        db.add(PlaygroundError(user_id=user.id))
        db.commit()
    elif not result.get("error"):
        pending_error = db.scalar(select(PlaygroundError).where(
            PlaygroundError.user_id == user.id, PlaygroundError.fixed_at.is_(None)
        ).order_by(PlaygroundError.created_at, PlaygroundError.id).limit(1))
        if pending_error:
            pending_error.fixed_at = utcnow()
            db.commit()
    return result


@app.post("/api/execution/cancel")
async def cancel_code(payload: CancelRunIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    execution = db.get(PlaygroundExecution, payload.execution_id)
    if execution is None or execution.user_id != user.id:
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
async def submit_challenge(challenge_id: str, payload: ChallengeIn, user: User = Depends(current_user),
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
    return {"passed": passed, "results": results, "xp_awarded": awarded, "total_xp": user.xp}


@app.get("/api/quizzes")
def list_quizzes(user: User | None = Depends(optional_user), db: Session = Depends(get_db)):
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

    catalog = []
    for item in QUIZ_METADATA:
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


@app.get("/api/quizzes/{quiz_id}")
def get_quiz(quiz_id: str, user: User | None = Depends(optional_user),
             db: Session = Depends(get_db)):
    questions = QUIZZES.get(quiz_id)
    if questions is None:
        raise HTTPException(status_code=404, detail="Quiz not found.")
    metadata = QUIZ_METADATA_BY_ID[quiz_id]
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
                db: Session = Depends(get_db)):
    questions = QUIZZES.get(quiz_id)
    if questions is None:
        raise HTTPException(status_code=404, detail="Quiz not found.")
    metadata = QUIZ_METADATA_BY_ID[quiz_id]
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
