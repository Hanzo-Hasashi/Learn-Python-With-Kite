from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Mapping
from urllib.parse import quote, urlsplit

import httpx

from backend.payments import (
    MobileMoneyProvider,
    PaymentProviderError,
    PaymentStatus,
    ProviderPaymentResult,
    normalize_zambian_phone,
)


LENCO_ZMW_COLLECTION_FLOW_IMPLEMENTED = True

OFFICIAL_LENCO_BASE_URLS = frozenset({
    "https://api.lenco.co/access/v1",
    "https://api.lenco.co/access/v2",
})
LENCO_AUTH_CHECK_PATH = "/accounts"
LENCO_COLLECTION_WEBHOOK_EVENTS = frozenset({
    "collection.successful",
    "collection.failed",
    "collection.settled",
})
LENCO_ZAMBIA_OPERATORS = {
    "airtel": "Airtel Money",
    "mtn": "MTN MoMo",
    "zamtel": "Zamtel Kwacha",
}
LENCO_PROVIDER_IDS = tuple("lenco_" + operator for operator in LENCO_ZAMBIA_OPERATORS)
LENCO_V2_BASE_URL = "https://api.lenco.co/access/v2"


class LencoConfigurationError(ValueError):
    pass


class LencoConnectivityError(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class LencoConfig:
    base_url: str = field(repr=False)
    api_token: str = field(repr=False)
    public_key: str | None = field(repr=False)

    def __repr__(self) -> str:
        return "LencoConfig(configured=True)"


@dataclass(frozen=True)
class LencoConnectivityResult:
    authenticated: bool
    status_code: int


def validate_lenco_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise LencoConfigurationError("KITE_LENCO_BASE_URL is missing or malformed.")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise LencoConfigurationError(
            "KITE_LENCO_BASE_URL must match the documented HTTPS Lenco API base URL."
        ) from None
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.lenco.co"
        or parsed.path not in {
            "/access/v1",
            "/access/v1/",
            "/access/v2",
            "/access/v2/",
        }
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise LencoConfigurationError(
            "KITE_LENCO_BASE_URL must match the documented HTTPS Lenco API base URL."
        )
    normalized_url = "https://" + parsed.netloc + parsed.path.rstrip("/")
    if normalized_url not in OFFICIAL_LENCO_BASE_URLS:
        raise LencoConfigurationError(
            "KITE_LENCO_BASE_URL must match the documented HTTPS Lenco API base URL."
        )
    return normalized_url


def _required_secret(name: str, value: str | None) -> str:
    if not value:
        raise LencoConfigurationError(f"{name} is required.")
    if value != value.strip() or any(character.isspace() for character in value):
        raise LencoConfigurationError(f"{name} must not contain whitespace.")
    return value


def load_lenco_config(env: Mapping[str, str] | None = None) -> LencoConfig:
    source = os.environ if env is None else env
    missing = [
        name for name in ("KITE_LENCO_BASE_URL", "KITE_LENCO_API_TOKEN")
        if not source.get(name)
    ]
    if missing:
        raise LencoConfigurationError(
            "Missing required Lenco setting(s): " + ", ".join(missing) + "."
        )
    base_url = validate_lenco_base_url(source["KITE_LENCO_BASE_URL"])
    api_token = _required_secret("KITE_LENCO_API_TOKEN", source["KITE_LENCO_API_TOKEN"])
    public_key = source.get("KITE_LENCO_PUBLIC_KEY") or None
    return LencoConfig(
        base_url=base_url,
        api_token=api_token,
        public_key=public_key,
    )


def check_lenco_authentication(
    config: LencoConfig,
    client: httpx.Client | None = None,
) -> LencoConnectivityResult:
    def request_accounts(http_client: httpx.Client) -> LencoConnectivityResult:
        response = http_client.get(
            config.base_url + LENCO_AUTH_CHECK_PATH,
            headers={
                "Authorization": "Bearer " + config.api_token,
                "Accept": "application/json",
            },
            timeout=10.0,
            follow_redirects=False,
        )
        if response.status_code != 200:
            raise LencoConnectivityError(
                f"Lenco read-only authentication check failed (HTTP {response.status_code})."
            )
        try:
            payload = response.json()
        except ValueError:
            raise LencoConnectivityError(
                "Lenco read-only authentication check returned invalid JSON."
            ) from None
        if not isinstance(payload, dict) or payload.get("status") is not True:
            raise LencoConnectivityError(
                "Lenco read-only authentication check was not confirmed."
            )
        return LencoConnectivityResult(authenticated=True, status_code=200)

    try:
        if client is not None:
            return request_accounts(client)
        with httpx.Client(timeout=10.0, follow_redirects=False) as http_client:
            return request_accounts(http_client)
    except httpx.HTTPError:
        raise LencoConnectivityError(
            "Lenco read-only authentication check could not reach the documented API."
        ) from None


def validate_zmw_amount(amount: str, currency: str) -> str:
    if currency != "ZMW":
        raise ValueError("Lenco checkout requires ZMW.")
    if not isinstance(amount, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", amount):
        raise ValueError("The payment amount must be a positive decimal string.")
    try:
        parsed_amount = Decimal(amount)
    except InvalidOperation as error:
        raise ValueError("The payment amount must be a positive decimal string.") from error
    if not parsed_amount.is_finite() or parsed_amount <= 0:
        raise ValueError("The payment amount must be a positive decimal string.")
    return amount


def validate_lenco_reference(reference: str) -> str:
    if (
        not isinstance(reference, str)
        or not reference
        or len(reference) > 64
        or not re.fullmatch(r"[A-Za-z0-9._-]+", reference)
    ):
        raise ValueError("The Lenco payment reference is invalid.")
    return reference


def _decimal_value(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def _amounts_match(left: object, right: object) -> bool:
    first = _decimal_value(left)
    second = _decimal_value(right)
    return first is not None and second is not None and first == second


def _lenco_numeric_amount(amount: str) -> float:
    decimal_amount = Decimal(validate_zmw_amount(amount, "ZMW"))
    try:
        numeric_amount = float(decimal_amount)
    except (OverflowError, ValueError):
        raise ValueError("The Lenco amount cannot be represented safely.") from None
    if not math.isfinite(numeric_amount) or Decimal(str(numeric_amount)) != decimal_amount:
        raise ValueError("The Lenco amount cannot be represented safely.")
    return numeric_amount


def _lenco_api_request(
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    config = load_lenco_config()
    if config.base_url != LENCO_V2_BASE_URL:
        raise PaymentProviderError("Lenco collection requires the documented V2 API base URL.")
    try:
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            response = client.request(
                method,
                config.base_url + path,
                headers={
                    "Authorization": "Bearer " + config.api_token,
                    "Accept": "application/json",
                },
                json=payload,
            )
    except httpx.HTTPError:
        raise PaymentProviderError("Lenco API request could not be completed.") from None
    if response.status_code < 200 or response.status_code >= 300:
        raise PaymentProviderError(f"Lenco API returned HTTP {response.status_code}.")
    try:
        response_payload = response.json()
    except ValueError:
        raise PaymentProviderError("Lenco API returned invalid JSON.") from None
    if not isinstance(response_payload, dict) or response_payload.get("status") is not True:
        raise PaymentProviderError("Lenco API did not confirm the request.")
    data = response_payload.get("data")
    if not isinstance(data, dict):
        raise PaymentProviderError("Lenco API returned an invalid collection response.")
    return data


def verify_lenco_webhook_signature(
    body: bytes,
    headers: Mapping[str, str],
    api_token: str | None,
) -> bool:
    if not api_token:
        return False
    signature = next(
        (value for name, value in headers.items() if name.lower() == "x-lenco-signature"),
        None,
    )
    if not signature:
        return False
    webhook_hash_key = hashlib.sha256(api_token.encode("utf-8")).hexdigest().encode("ascii")
    expected_signature = hmac.new(webhook_hash_key, body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(signature, expected_signature)


class LencoProvider(MobileMoneyProvider):
    def __init__(self, operator: str = "airtel"):
        if operator not in LENCO_ZAMBIA_OPERATORS:
            raise ValueError("Unsupported Lenco Zambia mobile-money operator.")
        self.operator = operator
        self.provider_id = "lenco_" + operator
        self.display_name = "Lenco · " + LENCO_ZAMBIA_OPERATORS[operator]

    def is_available(self) -> bool:
        if (
            not LENCO_ZMW_COLLECTION_FLOW_IMPLEMENTED
            or os.getenv("KITE_LENCO_COLLECTION_ENABLED", "").lower() != "true"
            or os.getenv("KITE_LENCO_ZMW_COLLECTION_CONFIRMED", "").lower() != "true"
        ):
            return False
        try:
            config = load_lenco_config()
        except LencoConfigurationError:
            return False
        return config.base_url == LENCO_V2_BASE_URL

    def initiate(
        self, reference: str, amount: str, currency: str, phone_number: str,
    ) -> ProviderPaymentResult:
        validate_zmw_amount(amount, currency)
        validate_lenco_reference(reference)
        if not self.is_available():
            raise PaymentProviderError("Lenco mobile-money collection is not enabled.")
        try:
            normalized_phone = normalize_zambian_phone(phone_number)
        except ValueError:
            raise PaymentProviderError("The registered Zambian mobile number is invalid.") from None
        data = _lenco_api_request("POST", "/collections/mobile-money", {
            "amount": _lenco_numeric_amount(amount),
            "reference": reference,
            "phone": "0" + normalized_phone[4:],
            "operator": self.operator,
            "country": "zm",
            "bearer": "merchant",
        })
        result = self._collection_result(data, reference, amount)
        if result.status == PaymentStatus.SUCCESSFUL:
            return ProviderPaymentResult(
                status=PaymentStatus.PROCESSING,
                transaction_reference=reference,
                provider_reference=result.provider_reference,
                amount=amount,
                currency="ZMW",
            )
        return ProviderPaymentResult(
            status=result.status,
            transaction_reference=reference,
            provider_reference=result.provider_reference,
            amount=amount,
            currency="ZMW",
            failure_code=result.failure_code,
        )

    def verify(
        self, reference: str, provider_reference: str | None,
    ) -> ProviderPaymentResult:
        validate_lenco_reference(reference)
        if not self.is_available():
            raise PaymentProviderError("Lenco mobile-money collection is not enabled.")
        if provider_reference is not None and (
            not isinstance(provider_reference, str) or not provider_reference.strip()
        ):
            raise PaymentProviderError("The Lenco provider reference is invalid.")
        data = _lenco_api_request(
            "GET",
            "/collections/status/" + quote(reference, safe=""),
        )
        amount_value = data.get("amount")
        currency = data.get("currency")
        if not isinstance(amount_value, (str, int, float)) or currency != "ZMW":
            return ProviderPaymentResult(
                status=PaymentStatus.PENDING,
                failure_code="PROVIDER_VERIFICATION_MISMATCH",
            )
        try:
            amount = validate_zmw_amount(str(amount_value), currency)
            return self._collection_result(
                data,
                reference,
                amount,
                expected_provider_reference=provider_reference,
                verified=True,
            )
        except (PaymentProviderError, ValueError):
            return ProviderPaymentResult(
                status=PaymentStatus.PENDING,
                failure_code="PROVIDER_VERIFICATION_MISMATCH",
            )

    def verify_webhook(
        self, body: bytes, headers: Mapping[str, str],
    ) -> ProviderPaymentResult:
        if not verify_lenco_webhook_signature(
            body, headers, os.getenv("KITE_LENCO_API_TOKEN"),
        ):
            return ProviderPaymentResult(
                status=PaymentStatus.PENDING,
                failure_code="INVALID_WEBHOOK_SIGNATURE",
            )
        try:
            event = json.loads(body)
        except (TypeError, ValueError):
            return ProviderPaymentResult(
                status=PaymentStatus.PENDING,
                failure_code="INVALID_WEBHOOK_PAYLOAD",
            )
        if not isinstance(event, dict):
            return ProviderPaymentResult(
                status=PaymentStatus.PENDING,
                failure_code="INVALID_WEBHOOK_PAYLOAD",
            )
        event_name = event.get("event")
        if event_name not in LENCO_COLLECTION_WEBHOOK_EVENTS:
            return ProviderPaymentResult(
                status=PaymentStatus.PENDING,
                failure_code="IGNORED_EVENT",
            )
        data = event.get("data")
        if not isinstance(data, dict):
            return ProviderPaymentResult(
                status=PaymentStatus.PENDING,
                failure_code="INVALID_WEBHOOK_PAYLOAD",
            )
        reference = data.get("reference")
        if not isinstance(reference, str):
            return ProviderPaymentResult(
                status=PaymentStatus.PENDING,
                failure_code="INVALID_WEBHOOK_PAYLOAD",
            )
        try:
            validate_lenco_reference(reference)
        except ValueError:
            return ProviderPaymentResult(
                status=PaymentStatus.PENDING,
                failure_code="INVALID_WEBHOOK_PAYLOAD",
            )
        verified_result = self.verify(reference, data.get("lencoReference"))
        if (
            not verified_result.verified
            or not _amounts_match(data.get("amount"), verified_result.amount)
            or data.get("currency") != verified_result.currency
            or (data.get("type") is not None and data.get("type") != "mobile-money")
        ):
            return ProviderPaymentResult(
                status=PaymentStatus.PENDING,
                failure_code="PROVIDER_VERIFICATION_MISMATCH",
            )
        return verified_result

    def _collection_result(
        self,
        data: dict[str, object],
        reference: str,
        expected_amount: str,
        expected_provider_reference: str | None = None,
        verified: bool = False,
    ) -> ProviderPaymentResult:
        if (
            data.get("reference") != reference
            or not _amounts_match(data.get("amount"), expected_amount)
            or data.get("currency") != "ZMW"
            or data.get("type") != "mobile-money"
        ):
            raise PaymentProviderError("Lenco returned collection details that do not match the request.")
        provider_reference = data.get("lencoReference")
        if not isinstance(provider_reference, str) or not provider_reference.strip():
            raise PaymentProviderError("Lenco returned an invalid provider reference.")
        if (
            expected_provider_reference is not None
            and provider_reference != expected_provider_reference
        ):
            raise PaymentProviderError("Lenco returned a mismatched provider reference.")
        details = data.get("mobileMoneyDetails")
        if not isinstance(details, dict) or (
            details.get("country") != "zm" or details.get("operator") != self.operator
        ):
            raise PaymentProviderError("Lenco returned a mismatched mobile-money method.")
        status_value = data.get("status")
        statuses = {
            "successful": PaymentStatus.SUCCESSFUL,
            "failed": PaymentStatus.FAILED,
            "pending": PaymentStatus.PENDING,
            "pay-offline": PaymentStatus.PENDING,
        }
        status = statuses.get(status_value) if isinstance(status_value, str) else None
        if status is None:
            raise PaymentProviderError("Lenco returned an unsupported collection status.")
        return ProviderPaymentResult(
            status=status,
            verified=verified,
            transaction_reference=reference,
            provider_reference=provider_reference,
            amount=str(data["amount"]),
            currency="ZMW",
            failure_code="LENCO_COLLECTION_FAILED" if status == PaymentStatus.FAILED else None,
        )


lenco_providers = tuple(LencoProvider(operator) for operator in LENCO_ZAMBIA_OPERATORS)
lenco_provider = lenco_providers[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a read-only Lenco API authentication check.")
    parser.add_argument(
        "--check-auth",
        action="store_true",
        help="Call the documented read-only GET /accounts endpoint.",
    )
    arguments = parser.parse_args(argv)
    if not arguments.check_auth:
        parser.error("specify --check-auth")
    try:
        config = load_lenco_config()
        result = check_lenco_authentication(config)
    except (LencoConfigurationError, LencoConnectivityError) as error:
        print(str(error), file=sys.stderr)
        return 1
    if result.authenticated:
        print("Lenco read-only authentication check passed (GET /accounts).")
        return 0
    print("Lenco read-only authentication check was not confirmed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
