from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from typing import Mapping, Protocol


class PaymentStatus(StrEnum):
    NOT_INITIATED = "NOT_INITIATED"
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCESSFUL = "SUCCESSFUL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class PaymentProviderError(Exception):
    pass


@dataclass(frozen=True)
class ProviderPaymentResult:
    status: PaymentStatus
    verified: bool = False
    transaction_reference: str | None = None
    provider_reference: str | None = None
    amount: str | None = None
    currency: str | None = None
    failure_code: str | None = None


class MobileMoneyProvider(Protocol):
    provider_id: str
    display_name: str

    def initiate(
        self, reference: str, amount: str, currency: str, phone_number: str,
    ) -> ProviderPaymentResult: ...

    def verify(self, reference: str, provider_reference: str | None) -> ProviderPaymentResult: ...

    def verify_webhook(
        self, body: bytes, headers: Mapping[str, str],
    ) -> ProviderPaymentResult: ...


_providers: dict[str, MobileMoneyProvider] = {}
_providers_lock = RLock()


def register_mobile_money_provider(provider: MobileMoneyProvider) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", provider.provider_id):
        raise ValueError("Provider IDs must be lowercase alphanumeric identifiers.")
    with _providers_lock:
        _providers[provider.provider_id] = provider


def get_mobile_money_provider(provider_id: str) -> MobileMoneyProvider | None:
    with _providers_lock:
        return _providers.get(provider_id)


def is_payment_provider_available(provider: MobileMoneyProvider) -> bool:
    availability_check = getattr(provider, "is_available", None)
    return availability_check() if callable(availability_check) else True


def normalize_zambian_phone(value: str) -> str:
    value = value.strip()
    if not value or not re.fullmatch(r"\+?[0-9\s().-]+", value):
        raise ValueError("Use digits and optional spaces, parentheses, hyphens, or a leading +.")
    if value.count("+") > 1 or ("+" in value and not value.startswith("+")):
        raise ValueError("Use a single leading + for an international number.")
    digits = re.sub(r"\D", "", value)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("260"):
        national_number = digits[3:]
    elif digits.startswith("0"):
        national_number = digits[1:]
    else:
        national_number = digits
    if len(national_number) != 9 or national_number[0] not in {"7", "9"}:
        raise ValueError("Enter a valid Zambian mobile number, such as +260 97 123 4567.")
    return "+260" + national_number
