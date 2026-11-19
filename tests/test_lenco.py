import hashlib
import hmac
import os
import unittest
from unittest.mock import patch

import httpx

from backend.lenco import (
    LENCO_PROVIDER_IDS,
    LENCO_ZAMBIA_OPERATORS,
    LencoConfigurationError,
    LencoConnectivityError,
    _lenco_api_request,
    check_lenco_authentication,
    load_lenco_config,
    LencoProvider,
    validate_lenco_reference,
    validate_zmw_amount,
    verify_lenco_webhook_signature,
)
from backend.payments import PaymentProviderError, PaymentStatus


class LencoBoundaryTests(unittest.TestCase):
    def test_missing_api_configuration_reports_names_without_values(self):
        with self.assertRaises(LencoConfigurationError) as raised:
            load_lenco_config({})

        message = str(raised.exception)
        self.assertIn("KITE_LENCO_BASE_URL", message)
        self.assertIn("KITE_LENCO_API_TOKEN", message)
        self.assertNotIn("secret", message.lower())

    def test_valid_configuration_hides_credentials_from_repr(self):
        token = "test-only-lenco-token"
        public_key = "test-only-lenco-public-key"
        config = load_lenco_config({
            "KITE_LENCO_BASE_URL": "https://api.lenco.co/access/v2/",
            "KITE_LENCO_API_TOKEN": token,
            "KITE_LENCO_PUBLIC_KEY": public_key,
        })

        self.assertEqual(config.base_url, "https://api.lenco.co/access/v2")
        self.assertEqual(config.api_token, token)
        self.assertEqual(config.public_key, public_key)
        self.assertNotIn(token, repr(config))
        self.assertNotIn(public_key, repr(config))

    def test_v1_remains_supported_as_an_official_documented_base_url(self):
        config = load_lenco_config({
            "KITE_LENCO_BASE_URL": "https://api.lenco.co/access/v1",
            "KITE_LENCO_API_TOKEN": "test-only-lenco-token",
        })
        self.assertEqual(config.base_url, "https://api.lenco.co/access/v1")

    def test_base_url_must_be_the_documented_https_lenco_api_root(self):
        for value in (
            "http://api.lenco.co/access/v1",
            "https://api.lenco.co/access/v3",
            "https://evil.example/access/v1",
            "https://api.lenco.co/access/v1?redirect=example",
            "https://user:password@api.lenco.co/access/v1",
            "https://api.lenco.co/access/v1/../../other",
            "https://[invalid/access/v1",
        ):
            with self.subTest(value=value), self.assertRaises(LencoConfigurationError) as raised:
                load_lenco_config({
                    "KITE_LENCO_BASE_URL": value,
                    "KITE_LENCO_API_TOKEN": "test-only-lenco-token",
                })
            self.assertNotIn("password", str(raised.exception))

    def test_read_only_auth_check_uses_only_bearer_token_and_discards_account_data(self):
        token = "test-only-lenco-token"
        public_key = "test-only-lenco-public-key"
        config = load_lenco_config({
            "KITE_LENCO_BASE_URL": "https://api.lenco.co/access/v2",
            "KITE_LENCO_API_TOKEN": token,
            "KITE_LENCO_PUBLIC_KEY": public_key,
        })

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "status": True,
                    "data": [{"accountNumber": "sensitive-account-data"}],
                }

        class FakeClient:
            request = None

            def get(self, url, **kwargs):
                self.request = (url, kwargs)
                return FakeResponse()

        client = FakeClient()
        result = check_lenco_authentication(config, client=client)
        url, request = client.request

        self.assertEqual(url, "https://api.lenco.co/access/v2/accounts")
        self.assertEqual(request["headers"]["Authorization"], "Bearer " + token)
        self.assertNotIn(public_key, str(request["headers"]))
        self.assertNotIn("accountNumber", repr(result))
        self.assertTrue(result.authenticated)

    def test_read_only_auth_errors_do_not_include_secret_or_response_body(self):
        token = "test-only-lenco-token"
        config = load_lenco_config({
            "KITE_LENCO_BASE_URL": "https://api.lenco.co/access/v2",
            "KITE_LENCO_API_TOKEN": token,
        })

        class UnauthorizedResponse:
            status_code = 401

            @staticmethod
            def json():
                return {"message": "secret echoed by provider"}

        class UnauthorizedClient:
            def get(self, url, **kwargs):
                return UnauthorizedResponse()

        with self.assertRaises(LencoConnectivityError) as raised:
            check_lenco_authentication(config, client=UnauthorizedClient())
        self.assertIn("401", str(raised.exception))
        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn("secret echoed", str(raised.exception))

        class TimeoutClient:
            def get(self, url, **kwargs):
                raise httpx.TimeoutException("request carried " + token)

        with self.assertRaises(LencoConnectivityError) as raised:
            check_lenco_authentication(config, client=TimeoutClient())
        self.assertNotIn(token, str(raised.exception))

    def test_zmw_amount_is_validated_without_reformatting_or_rounding(self):
        self.assertEqual(validate_zmw_amount("19.990", "ZMW"), "19.990")

        with self.assertRaisesRegex(ValueError, "requires ZMW"):
            validate_zmw_amount("19.99", "NGN")
        for value in ("0", "-1", "NaN", "1e2", "not-an-amount"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_zmw_amount(value, "ZMW")

    def test_webhook_signature_uses_documented_derived_key_and_raw_body(self):
        body = b'{"event":"transaction.successful","data":{"amount":"19.99"}}'
        token = "test-only-lenco-token"
        webhook_key = hashlib.sha256(token.encode("utf-8")).hexdigest().encode("ascii")
        signature = hmac.new(webhook_key, body, hashlib.sha512).hexdigest()

        self.assertTrue(verify_lenco_webhook_signature(
            body, {"X-Lenco-Signature": signature}, token,
        ))
        self.assertFalse(verify_lenco_webhook_signature(
            body + b" ", {"X-Lenco-Signature": signature}, token,
        ))
        self.assertFalse(verify_lenco_webhook_signature(
            body, {"X-Lenco-Signature": signature.upper()}, token,
        ))

    def test_missing_token_or_signature_fails_webhook_authentication(self):
        self.assertFalse(verify_lenco_webhook_signature(
            b"{}", {"X-Lenco-Signature": "signature"}, None,
        ))
        self.assertFalse(verify_lenco_webhook_signature(
            b"{}", {}, "test-only-lenco-token",
        ))

    def test_provider_stays_unavailable_when_collection_flags_are_off(self):
        provider = LencoProvider("airtel")
        with patch.dict(os.environ, {
            "KITE_LENCO_BASE_URL": "https://api.lenco.co/access/v2",
            "KITE_LENCO_API_TOKEN": "test-only-lenco-token",
            "KITE_LENCO_PUBLIC_KEY": "test-only-lenco-public-key",
            "KITE_LENCO_COLLECTION_ENABLED": "false",
            "KITE_LENCO_ZMW_COLLECTION_CONFIRMED": "false",
        }, clear=False):
            self.assertFalse(provider.is_available())
            with self.assertRaisesRegex(PaymentProviderError, "not enabled"):
                provider.initiate("kite-ref", "19.99", "ZMW", "+260971234567")
            with self.assertRaisesRegex(PaymentProviderError, "not enabled"):
                provider.verify("kite-ref", None)
        self.assertEqual(LENCO_PROVIDER_IDS, ("lenco_airtel", "lenco_mtn", "lenco_zamtel"))
        self.assertEqual(set(LENCO_ZAMBIA_OPERATORS), {"airtel", "mtn", "zamtel"})

    def test_valid_webhook_signature_requires_independent_collection_verification(self):
        body = (
            b'{"event":"collection.successful","data":{"reference":"kite-ref",'
            b'"amount":"19.99","currency":"ZMW","type":"mobile-money",'
            b'"lencoReference":"lenco-123"}}'
        )
        token = "test-only-lenco-token"
        webhook_key = hashlib.sha256(token.encode("utf-8")).hexdigest().encode("ascii")
        signature = hmac.new(webhook_key, body, hashlib.sha512).hexdigest()
        provider = LencoProvider("airtel")
        api_response = self.collection_data(status="successful")

        with patch.dict(os.environ, self.enabled_environment(token), clear=False):
            with patch("backend.lenco._lenco_api_request", return_value=api_response) as request:
                result = provider.verify_webhook(body, {"x-lenco-signature": signature})

        self.assertTrue(result.verified)
        self.assertEqual(result.status, PaymentStatus.SUCCESSFUL)
        request.assert_called_once_with("GET", "/collections/status/kite-ref")

    @staticmethod
    def enabled_environment(token="test-only-lenco-token"):
        return {
            "KITE_LENCO_BASE_URL": "https://api.lenco.co/access/v2",
            "KITE_LENCO_API_TOKEN": token,
            "KITE_LENCO_COLLECTION_ENABLED": "true",
            "KITE_LENCO_ZMW_COLLECTION_CONFIRMED": "true",
        }

    @staticmethod
    def collection_data(
        status="pay-offline",
        reference="kite-ref",
        amount="19.99",
        currency="ZMW",
        operator="airtel",
        country="zm",
        payment_type="mobile-money",
        lenco_reference="lenco-123",
    ):
        return {
            "reference": reference,
            "amount": amount,
            "currency": currency,
            "type": payment_type,
            "status": status,
            "lencoReference": lenco_reference,
            "mobileMoneyDetails": {"country": country, "operator": operator},
        }

    def test_initiation_posts_documented_zambia_mobile_money_request(self):
        provider = LencoProvider("airtel")
        data = self.collection_data()
        with patch.dict(os.environ, self.enabled_environment(), clear=False):
            with patch("backend.lenco._lenco_api_request", return_value=data) as request:
                result = provider.initiate(
                    "kite-ref", "19.99", "ZMW", "+260971234567",
                )

        request.assert_called_once_with("POST", "/collections/mobile-money", {
            "amount": 19.99,
            "reference": "kite-ref",
            "phone": "0971234567",
            "operator": "airtel",
            "country": "zm",
            "bearer": "merchant",
        })
        self.assertEqual(result.status, PaymentStatus.PENDING)
        self.assertFalse(result.verified)
        self.assertEqual(result.transaction_reference, "kite-ref")
        self.assertEqual(result.provider_reference, "lenco-123")

    def test_all_supported_operators_are_routed_and_invalid_phones_are_rejected(self):
        with patch.dict(os.environ, self.enabled_environment(), clear=False):
            for operator in LENCO_ZAMBIA_OPERATORS:
                provider = LencoProvider(operator)
                response = self.collection_data(operator=operator)
                with self.subTest(operator=operator), patch(
                    "backend.lenco._lenco_api_request", return_value=response,
                ) as request:
                    result = provider.initiate(
                        "kite-ref", "19.99", "ZMW", "+260971234567",
                    )
                self.assertEqual(result.status, PaymentStatus.PENDING)
                self.assertEqual(request.call_args.args[2]["operator"], operator)

            provider = LencoProvider("airtel")
            with patch("backend.lenco._lenco_api_request") as request:
                for phone in ("123", "+26097123456x", "+260123456789"):
                    with self.subTest(phone=phone), self.assertRaises(PaymentProviderError):
                        provider.initiate("kite-ref", "19.99", "ZMW", phone)
                request.assert_not_called()

    def test_initiation_rejects_bad_amount_currency_reference_and_operator(self):
        provider = LencoProvider("airtel")
        with patch.dict(os.environ, self.enabled_environment(), clear=False):
            with patch("backend.lenco._lenco_api_request") as request:
                for amount, currency, reference in (
                    ("0", "ZMW", "kite-ref"),
                    ("not-a-number", "ZMW", "kite-ref"),
                    ("19.99", "USD", "kite-ref"),
                    ("19.99", "ZMW", "kite/ref"),
                ):
                    with self.subTest(amount=amount, currency=currency, reference=reference):
                        with self.assertRaises(ValueError):
                            provider.initiate(reference, amount, currency, "+260971234567")
                request.assert_not_called()
        with self.assertRaisesRegex(ValueError, "operator"):
            LencoProvider("unsupported")
        for reference in ("", "bad/ref", "has space", "x" * 65):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                validate_lenco_reference(reference)

    def test_initiation_does_not_mark_a_success_response_as_paid(self):
        provider = LencoProvider("mtn")
        with patch.dict(os.environ, self.enabled_environment(), clear=False):
            with patch(
                "backend.lenco._lenco_api_request",
                return_value=self.collection_data(status="successful", operator="mtn"),
            ):
                result = provider.initiate(
                    "kite-ref", "19.99", "ZMW", "0971234567",
                )
        self.assertEqual(result.status, PaymentStatus.PROCESSING)
        self.assertFalse(result.verified)

    def test_verification_maps_success_pending_and_failure_from_lenco_v2(self):
        provider = LencoProvider("airtel")
        with patch.dict(os.environ, self.enabled_environment(), clear=False):
            for lenco_status, expected_status in (
                ("successful", PaymentStatus.SUCCESSFUL),
                ("pay-offline", PaymentStatus.PENDING),
                ("pending", PaymentStatus.PENDING),
                ("failed", PaymentStatus.FAILED),
            ):
                with self.subTest(status=lenco_status):
                    with patch(
                        "backend.lenco._lenco_api_request",
                        return_value=self.collection_data(status=lenco_status),
                    ) as request:
                        result = provider.verify("kite-ref", "lenco-123")
                    request.assert_called_once_with(
                        "GET", "/collections/status/kite-ref",
                    )
                    self.assertTrue(result.verified)
                    self.assertEqual(result.status, expected_status)
                    self.assertEqual(result.transaction_reference, "kite-ref")
                    self.assertEqual(result.currency, "ZMW")

    def test_verification_fails_closed_on_reference_amount_currency_method_or_provider_ref_mismatch(self):
        provider = LencoProvider("airtel")
        mismatches = (
            {"reference": "other-ref"},
            {"amount": "NaN"},
            {"currency": "USD"},
            {"payment_type": "card"},
            {"operator": "mtn"},
            {"country": "mw"},
            {"lenco_reference": "different-provider-ref"},
        )
        with patch.dict(os.environ, self.enabled_environment(), clear=False):
            for changes in mismatches:
                data = self.collection_data(**changes)
                with self.subTest(changes=changes), patch(
                    "backend.lenco._lenco_api_request", return_value=data,
                ):
                    result = provider.verify("kite-ref", "lenco-123")
                    self.assertFalse(result.verified)
                    self.assertEqual(
                        result.failure_code, "PROVIDER_VERIFICATION_MISMATCH",
                    )

    def test_provider_errors_do_not_expose_secrets_or_response_bodies(self):
        token = "test-only-lenco-token"
        provider = LencoProvider("airtel")
        with patch.dict(os.environ, self.enabled_environment(token), clear=False):
            with patch(
                "backend.lenco._lenco_api_request",
                side_effect=PaymentProviderError("request failed"),
            ):
                with self.assertRaises(PaymentProviderError) as raised:
                    provider.verify("kite-ref", None)
        self.assertNotIn(token, str(raised.exception))

    def test_invalid_webhook_signature_never_queries_lenco(self):
        provider = LencoProvider("airtel")
        with patch.dict(os.environ, self.enabled_environment(), clear=False):
            with patch("backend.lenco._lenco_api_request") as request:
                result = provider.verify_webhook(b"{}", {"X-Lenco-Signature": "invalid"})
        self.assertFalse(result.verified)
        self.assertEqual(result.failure_code, "INVALID_WEBHOOK_SIGNATURE")
        request.assert_not_called()

    def test_non_collection_webhook_is_acknowledged_as_ignored(self):
        body = b'{"event":"transfer.successful","data":{}}'
        token = "test-only-lenco-token"
        key = hashlib.sha256(token.encode("utf-8")).hexdigest().encode("ascii")
        signature = hmac.new(key, body, hashlib.sha512).hexdigest()
        provider = LencoProvider("airtel")
        with patch.dict(os.environ, self.enabled_environment(token), clear=False):
            with patch("backend.lenco._lenco_api_request") as request:
                result = provider.verify_webhook(body, {"X-Lenco-Signature": signature})
        self.assertEqual(result.failure_code, "IGNORED_EVENT")
        request.assert_not_called()

    def test_generic_transaction_webhook_is_not_treated_as_collection_event(self):
        body = b'{"event":"transaction.successful","data":{"clientReference":"kite-ref"}}'
        token = "test-only-lenco-token"
        key = hashlib.sha256(token.encode("utf-8")).hexdigest().encode("ascii")
        signature = hmac.new(key, body, hashlib.sha512).hexdigest()
        provider = LencoProvider("airtel")
        with patch.dict(os.environ, self.enabled_environment(token), clear=False):
            with patch("backend.lenco._lenco_api_request") as request:
                result = provider.verify_webhook(body, {"X-Lenco-Signature": signature})
        self.assertEqual(result.failure_code, "IGNORED_EVENT")
        request.assert_not_called()

    def test_lenco_api_timeout_is_reported_as_provider_error(self):
        provider = LencoProvider("airtel")
        with patch.dict(os.environ, self.enabled_environment(), clear=False):
            with patch(
                "backend.lenco._lenco_api_request",
                side_effect=PaymentProviderError("Lenco API request could not be completed."),
            ):
                with self.assertRaisesRegex(PaymentProviderError, "could not be completed"):
                    provider.initiate(
                        "kite-ref", "19.99", "ZMW", "+260971234567",
                    )

    def test_collection_api_uses_v2_bearer_auth_and_hides_failed_response_body(self):
        token = "test-only-lenco-token"

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"status": True, "data": {"reference": "kite-ref"}}

        class FakeClient:
            request_args = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def request(self, *args, **kwargs):
                self.request_args = (args, kwargs)
                return FakeResponse()

        client = FakeClient()
        with patch.dict(os.environ, self.enabled_environment(token), clear=False):
            with patch("backend.lenco.httpx.Client", return_value=client):
                result = _lenco_api_request("GET", "/collections/status/kite-ref")
        args, kwargs = client.request_args
        self.assertEqual(args, ("GET", "https://api.lenco.co/access/v2/collections/status/kite-ref"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + token)
        self.assertEqual(result["reference"], "kite-ref")

        class RejectedResponse:
            status_code = 401
            text = "Bearer " + token

            @staticmethod
            def json():
                return {"message": "Bearer " + token}

        client.request = lambda *args, **kwargs: RejectedResponse()
        with patch.dict(os.environ, self.enabled_environment(token), clear=False):
            with patch("backend.lenco.httpx.Client", return_value=client):
                with self.assertRaises(PaymentProviderError) as raised:
                    _lenco_api_request("GET", "/collections/status/kite-ref")
        self.assertNotIn(token, str(raised.exception))

    def test_collection_api_rejects_server_errors_and_malformed_responses(self):
        class FakeResponse:
            status_code = 200

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class FakeClient:
            def __init__(self, response):
                self.response = response

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def request(self, *args, **kwargs):
                return self.response

        for response, error in (
            (type("ServerError", (), {"status_code": 503})(), "HTTP 503"),
            (FakeResponse({"status": True, "data": []}), "invalid collection response"),
            (FakeResponse({"status": False, "message": "provider failure"}), "did not confirm"),
        ):
            with self.subTest(error=error), patch.dict(
                os.environ, self.enabled_environment(), clear=False,
            ), patch("backend.lenco.httpx.Client", return_value=FakeClient(response)):
                with self.assertRaisesRegex(PaymentProviderError, error):
                    _lenco_api_request("GET", "/collections/status/kite-ref")


if __name__ == "__main__":
    unittest.main()
