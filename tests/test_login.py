"""Login boundary regressions, without authenticating an account."""
import ssl
import threading
import unittest
from unittest.mock import Mock, patch

import websocket

from yuketang.config import Config
from yuketang.login import LoginError, LoginManager, QrcodeLogin


class LoginTests(unittest.TestCase):
    def test_missing_school_id_fails_before_any_lookup_or_qr_connection(self):
        with patch("requests.get") as lookup, patch("websocket.WebSocketApp") as connect:
            with self.assertRaisesRegex(LoginError, "手动填写学校 ID"):
                LoginManager(Config()).login(force_qrcode=True)
        lookup.assert_not_called()
        connect.assert_not_called()

    def test_manual_school_id_reaches_qr_without_automatic_lookup(self):
        with patch("requests.get") as lookup, patch.object(QrcodeLogin, "run", side_effect=LoginError("await scan")) as qr:
            with self.assertRaisesRegex(LoginError, "await scan"):
                LoginManager(Config(university_id="123")).login(force_qrcode=True)
        lookup.assert_not_called()
        qr.assert_called_once()

    def test_qr_websocket_uses_site_origin_and_propagates_qr_to_ui_callback(self):
        stop = threading.Event()
        callback = Mock(side_effect=lambda _: stop.set())
        fake = Mock()
        def factory(url, **handlers):
            self.assertEqual(url, "wss://example.invalid/wsapp/")
            fake.run_forever.side_effect = lambda **_: handlers["on_message"](
                fake, '{"ticket":"synthetic","qrcode":"synthetic-qr"}')
            return fake
        with patch("websocket.WebSocketApp", side_effect=factory):
            with self.assertRaisesRegex(LoginError, "取消"):
                QrcodeLogin("example.invalid", callback=callback, stop_event=stop).run()
        callback.assert_called_once_with("synthetic-qr")
        fake.run_forever.assert_called_once_with(origin="https://example.invalid")

    def test_handshake_diagnostics_do_not_expose_headers_or_response_body(self):
        login = QrcodeLogin("example.invalid")
        error = websocket.WebSocketBadStatusException("secret-cookie-response", status_code=403,
                                                       resp_headers={"Set-Cookie": "secret-cookie"})
        login._on_error(None, error)
        self.assertIn("HTTP 403", login.error_message)
        self.assertNotIn("secret", login.error_message)

    def test_certificate_and_timeout_errors_are_distinct_and_sanitized(self):
        login = QrcodeLogin("example.invalid")
        login._on_error(None, ssl.SSLCertVerificationError("synthetic-secret"))
        self.assertIn("证书", login.error_message)
        self.assertNotIn("synthetic-secret", login.error_message)
        login._on_error(None, websocket.WebSocketTimeoutException("synthetic-secret"))
        self.assertIn("超时", login.error_message)
        self.assertNotIn("synthetic-secret", login.error_message)

    def test_failure_inside_connection_thread_is_reported_immediately(self):
        fake = Mock()
        fake.run_forever.side_effect = websocket.WebSocketTimeoutException("private details")
        with patch("websocket.WebSocketApp", return_value=fake):
            with self.assertRaisesRegex(LoginError, "超时") as error:
                QrcodeLogin("example.invalid", timeout=1).run()
        self.assertNotIn("private details", str(error.exception))


if __name__ == "__main__":
    unittest.main()
