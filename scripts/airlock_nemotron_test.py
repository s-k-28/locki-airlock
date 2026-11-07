"""Transport safety unit tests; mocked responses are not inference evidence."""
import importlib.util
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("airlock_agent", Path(__file__).parents[1] / "backend/app/intelligence/airlock_agent.py")
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class AirlockNemotronTransportTests(unittest.TestCase):
    def test_low_memory_fails_before_network_or_model_load(self):
        with patch.object(agent, "resource_preflight", return_value={"ready": False, "reason": "memory pressure"}), patch.object(agent.urllib.request, "build_opener") as network, self.assertRaisesRegex(agent.ModelUnavailable, "memory pressure"):
            agent.infer({"messages": []})
            network.assert_not_called()

    def test_preflight_requires_memory_and_low_swap(self):
        with patch.object(agent.psutil, "virtual_memory", return_value=SimpleNamespace(available=11 * 1024**3, total=24 * 1024**3)):
            with patch.object(agent.psutil, "swap_memory", return_value=SimpleNamespace(percent=31)):
                self.assertFalse(agent.resource_preflight()["ready"])
            with patch.object(agent.psutil, "swap_memory", return_value=SimpleNamespace(percent=0)):
                self.assertTrue(agent.resource_preflight()["ready"])

    def test_remote_endpoint_and_embedded_credentials_denied(self):
        for value in ["https://example.com", "http://localhost.evil", "http://user:pass@localhost", "http://127.0.0.1/api", "http://localhost?token=secret", "file:///tmp/model"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                agent.local_endpoint(value)

    def test_loopback_endpoint_accepted(self):
        self.assertEqual(agent.local_endpoint("http://127.0.0.1:11434/"), "http://127.0.0.1:11434")

    def response(self, data):
        opener = Mock()
        response = io.BytesIO(json.dumps(data).encode())
        opener.open.return_value = response
        return opener

    def test_exact_model_bounded_options_timeout_no_proxy_and_thinking_redacted(self):
        opener = self.response({"model": agent.MODEL, "done": True, "message": {"role": "assistant", "content": "advisory", "thinking": "private", "tool_calls": []}, "eval_count": 4})
        with patch.object(agent, "resource_preflight", return_value={"ready": True}), patch.object(agent.urllib.request, "build_opener", return_value=opener):
            result = agent.infer({"messages": [], "tools": []})
        self.assertNotIn("thinking", result["message"])
        request = opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["model"], agent.MODEL)
        self.assertEqual(body["keep_alive"], 0)
        self.assertEqual(body["options"]["num_ctx"], 2048)
        self.assertEqual(body["options"]["num_predict"], 400)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 45)
        self.assertFalse(body["think"])
        self.assertFalse(body["stream"])

    def test_model_substitution_rejected(self):
        opener = self.response({"model": "unapproved", "done": True, "message": {"role": "assistant", "content": "x"}})
        with patch.object(agent, "resource_preflight", return_value={"ready": True}), patch.object(agent.urllib.request, "build_opener", return_value=opener), self.assertRaisesRegex(ValueError, "model id"):
            agent.infer({"messages": []})

    def test_partial_generation_never_reported_as_complete(self):
        for additional in [{"done": False}, {"done": True, "done_reason": "length"}]:
            opener = self.response({"model": agent.MODEL, "message": {"role": "assistant", "content": "x"}, **additional})
            with patch.object(agent, "resource_preflight", return_value={"ready": True}), patch.object(agent.urllib.request, "build_opener", return_value=opener), self.assertRaises(agent.ModelUnavailable):
                agent.infer({"messages": []})

    def test_unknown_transport_option_rejected(self):
        with patch.object(agent, "resource_preflight", return_value={"ready": True}), self.assertRaises(ValueError):
            agent.infer({"messages": [], "model": "other"})

    def test_oversized_request_fails_before_network(self):
        with patch.object(agent, "resource_preflight", return_value={"ready": True}), patch.object(agent.urllib.request, "build_opener") as network, self.assertRaisesRegex(ValueError, "budget"):
            agent.infer({"messages": [{"role": "user", "content": "x" * 100_000}]})
            network.assert_not_called()

    def test_invalid_assistant_message_rejected(self):
        opener = self.response({"model": agent.MODEL, "done": True, "message": {"role": "system", "content": "x"}})
        with patch.object(agent, "resource_preflight", return_value={"ready": True}), patch.object(agent.urllib.request, "build_opener", return_value=opener), self.assertRaisesRegex(ValueError, "assistant"):
            agent.infer({"messages": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
