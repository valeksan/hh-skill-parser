import argparse
import os
import stat
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from hh_parser.cli import build_parser, make_source, run_auth_login, run_auth_refresh
from hh_parser.oauth import authorization_url, pkce_pair, read_token_file, request_token, write_token_file


class OAuthTests(unittest.TestCase):
    def test_pkce_authorization_url_contains_expected_state_and_challenge(self):
        verifier, challenge = pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        url = authorization_url(
            client_id="client-id", redirect_uri="http://127.0.0.1:8765/callback",
            state="state-value", challenge=challenge,
        )
        values = parse_qs(urlparse(url).query)
        self.assertEqual(values["response_type"], ["code"])
        self.assertEqual(values["code_challenge_method"], ["S256"])
        self.assertEqual(values["state"], ["state-value"])

    def test_request_token_uses_form_body_and_never_needs_real_network(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"access_token": "access", "refresh_token": "refresh", "expires_in": 60}

        class Session:
            def __init__(self):
                self.call = None

            def post(self, url, **kwargs):
                self.call = (url, kwargs)
                return Response()

        session = Session()
        result = request_token({"grant_type": "refresh_token"}, user_agent="test/1.0 (test@example.com)", session=session)
        self.assertEqual(result["access_token"], "access")
        self.assertEqual(session.call[0], "https://api.hh.ru/token")
        self.assertEqual(session.call[1]["data"], {"grant_type": "refresh_token"})
        self.assertEqual(session.call[1]["headers"]["HH-User-Agent"], "test/1.0 (test@example.com)")

    def test_login_and_refresh_store_secret_file_without_returning_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token.json"
            parser = build_parser()
            login = parser.parse_args([
                "auth", "login", "--client-id", "client", "--client-secret", "secret",
                "--token-file", str(token_path), "--no-browser",
            ])
            requested = {}

            def callback(redirect_uri, state, timeout):
                self.assertEqual(redirect_uri, "http://127.0.0.1:8765/callback")
                self.assertTrue(state)
                self.assertEqual(timeout, 300)
                return "authorization-code"

            def requester(payload, *, user_agent):
                requested.update(payload)
                self.assertEqual(user_agent, "hh-skill-parser/1.0 (contact@example.invalid)")
                return {"access_token": "access-one", "refresh_token": "refresh-one", "expires_in": 60}

            result = run_auth_login(login, opener=lambda _: True, callback_waiter=callback, token_requester=requester)
            self.assertEqual(result, {"status": "completed", "token_file": str(token_path), "refreshable": True, "expires_in": 60})
            self.assertEqual(requested["code"], "authorization-code")
            self.assertEqual(requested["client_secret"], "secret")
            self.assertNotIn("access-one", str(result))
            self.assertEqual(read_token_file(token_path)["access_token"], "access-one")
            self.assertEqual(stat.S_IMODE(os.stat(token_path).st_mode), 0o600)

            refresh = parser.parse_args([
                "auth", "refresh", "--client-id", "client", "--client-secret", "secret",
                "--token-file", str(token_path),
            ])
            refreshed = run_auth_refresh(
                refresh,
                token_requester=lambda payload, **_: {
                    "access_token": "access-two", "refresh_token": "refresh-two", "expires_in": 120,
                },
            )
            self.assertEqual(refreshed["expires_in"], 120)
            self.assertEqual(read_token_file(token_path)["access_token"], "access-two")

    def test_transport_reads_token_file_without_exposing_it_as_cli_output(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token.json"
            write_token_file(token_path, {"access_token": "token-value"}, overwrite=False)
            settings = build_parser().parse_args([
                "collect", "--area", "1", "--token-file", str(token_path),
            ])
            source = make_source(settings)
            self.assertEqual(source.session.headers["Authorization"], "Bearer token-value")


if __name__ == "__main__":
    unittest.main()
