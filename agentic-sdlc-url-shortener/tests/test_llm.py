"""Tests for the OpenAI-compatible API backend (llm.py)."""
import unittest
import urllib.error

from sdlc_agent.llm import ApiBackend, LlmConfig, LlmConfigError, LlmRequestError, validate_schema


class LlmTests(unittest.TestCase):
    def test_missing_configuration_fails(self):
        with self.assertRaises(LlmConfigError):
            LlmConfig.from_env({})

    def test_config_requires_https_or_loopback(self):
        with self.assertRaises(LlmConfigError):
            LlmConfig.from_env({
                "LLM_MODEL": "gpt-test",
                "LLM_API_KEY": "secret",
                "LLM_BASE_URL": "http://example.com/v1",
            })

    def test_config_allows_loopback_http_without_api_key(self):
        config = LlmConfig.from_env({
            "LLM_MODEL": "local-model",
            "LLM_BASE_URL": "http://127.0.0.1:11434/v1",
        })
        self.assertIsNone(config.api_key)
        self.assertEqual(config.base_url, "http://127.0.0.1:11434/v1")

    def test_config_requires_api_key_for_remote_https(self):
        with self.assertRaises(LlmConfigError):
            LlmConfig.from_env({
                "LLM_MODEL": "gpt-test",
                "LLM_BASE_URL": "https://api.openai.com/v1",
            })

    def test_retry_exhaustion_stops(self):
        config = LlmConfig(model="gpt-test", api_key="secret", base_url="https://example.com/v1",
                            max_retries=3)
        attempts: list = []

        def failing_transport(request):
            attempts.append(request)
            raise urllib.error.HTTPError(request.full_url, 503, "Service Unavailable", {}, None)

        backend = ApiBackend(config, transport=failing_transport, sleep=lambda seconds: None)
        schema = {"required": ["task"], "properties": {"task": str}}

        with self.assertRaises(LlmRequestError):
            backend.generate({"task": "analysis"}, schema)

        self.assertEqual(len(attempts), 3)  # bounded, never retried past max_retries

    def test_successful_generate_returns_validated_response(self):
        config = LlmConfig(model="gpt-test", api_key="secret", base_url="https://example.com/v1")

        def succeeding_transport(request):
            return b'{"summary": "ok", "key_requirements": []}'

        backend = ApiBackend(config, transport=succeeding_transport)
        schema = {
            "required": ["task"], "properties": {"task": str},
            "response": {"required": ["summary"], "properties": {"summary": str}},
        }
        result = backend.generate({"task": "analysis"}, schema)
        self.assertEqual(result["summary"], "ok")

    def test_generate_sends_real_bearer_token_in_authorization_header(self):
        # Regression test for CR-01: the Authorization header must carry the
        # real API key using the documented bearer scheme, never a literal
        # masked placeholder string.
        config = LlmConfig(model="gpt-test", api_key="super-secret-key",
                            base_url="https://example.com/v1")
        captured_requests: list = []

        def capturing_transport(request):
            captured_requests.append(request)
            return b'{"summary": "ok", "key_requirements": []}'

        backend = ApiBackend(config, transport=capturing_transport)
        schema = {
            "required": ["task"], "properties": {"task": str},
            "response": {"required": ["summary"], "properties": {"summary": str}},
        }
        backend.generate({"task": "analysis"}, schema)

        self.assertEqual(len(captured_requests), 1)
        auth_header = captured_requests[0].get_header("Authorization")
        self.assertEqual(auth_header, "Bearer " + config.api_key)
        self.assertNotEqual(auth_header, "******")
        self.assertIn(config.api_key, auth_header)

    def test_validate_schema_rejects_missing_required_field(self):
        with self.assertRaises(Exception):
            validate_schema({}, {"required": ["task"], "properties": {}})


if __name__ == "__main__":
    unittest.main()
