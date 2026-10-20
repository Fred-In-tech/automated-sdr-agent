"""The three HTTP adapters, tested against a fake network: request shape, parsing, the JSON
fallback ladder, plain-English errors, and that an API key never appears in an error."""

import io
import json
import socket
import unittest
import urllib.error

from core.ai.connectors import AIError, chat, extract_json, list_models, redact
from core.ai.providers import load_ai_config

SCHEMA = {"type": "object", "properties": {"intent": {"type": "string"}}, "required": ["intent"]}


def cfg(provider, mode="hosted", **extra):
    ai = {"mode": mode, "provider": provider, "model": "m1", **extra}
    return load_ai_config({"ai": ai}, environ={"AI_API_KEY": "sk-secret-123"})


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records requests; replies with queued payloads (dict -> JSON) or raises queued exceptions."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode()) if request.data else None
        self.requests.append({"url": request.full_url, "method": request.get_method(),
                              "headers": {k.lower(): v for k, v in request.header_items()},
                              "body": body, "timeout": timeout})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return FakeResponse(json.dumps(reply).encode())


def http_error(code, text=""):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(text.encode()))


OPENAI_OK = {"choices": [{"message": {"content": "Hello there"}}], "usage": {"prompt_tokens": 12, "completion_tokens": 3}}


class TestOpenAICompatible(unittest.TestCase):
    def test_request_shape_and_parsing(self):
        fake = FakeOpener(OPENAI_OK)
        reply = chat(cfg("openai"), "sys", "user text", opener=fake)
        req = fake.requests[0]
        self.assertEqual(req["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(req["headers"]["authorization"], "Bearer sk-secret-123")
        self.assertEqual(req["body"]["messages"], [{"role": "system", "content": "sys"}, {"role": "user", "content": "user text"}])
        self.assertEqual(req["body"]["model"], "m1")
        self.assertNotIn("temperature", req["body"])
        self.assertEqual((reply.text, reply.input_tokens, reply.output_tokens), ("Hello there", 12, 3))

    def test_azure_uses_api_key_header(self):
        fake = FakeOpener(OPENAI_OK)
        chat(cfg("azure", base_url="https://acme.openai.azure.com/openai/v1"), "s", "u", opener=fake)
        self.assertEqual(fake.requests[0]["headers"]["api-key"], "sk-secret-123")
        self.assertNotIn("authorization", fake.requests[0]["headers"])

    def test_local_server_sends_no_key(self):
        fake = FakeOpener(OPENAI_OK)
        chat(cfg("lmstudio", mode="local_only"), "s", "u", opener=fake)
        self.assertEqual(fake.requests[0]["url"], "http://localhost:1234/v1/chat/completions")
        self.assertNotIn("authorization", fake.requests[0]["headers"])

    def test_schema_ladder(self):
        fake = FakeOpener(http_error(400, "response_format json_schema not supported"),
                          http_error(400, "response_format not supported"), OPENAI_OK)
        reply = chat(cfg("openai"), "s", "u", schema=SCHEMA, opener=fake)
        formats = [r["body"].get("response_format") for r in fake.requests]
        self.assertEqual(formats[0]["type"], "json_schema")
        self.assertEqual(formats[1], {"type": "json_object"})
        self.assertIsNone(formats[2])
        self.assertIn("intent", fake.requests[2]["body"]["messages"][0]["content"])   # schema moved into the prompt
        self.assertEqual(reply.text, "Hello there")

    def test_list_models(self):
        fake = FakeOpener({"data": [{"id": "gpt-a"}, {"id": "gpt-b"}]})
        self.assertEqual(list_models(cfg("openai"), opener=fake), ["gpt-a", "gpt-b"])
        self.assertEqual(fake.requests[0]["url"], "https://api.openai.com/v1/models")


class TestAnthropic(unittest.TestCase):
    def test_messages_api(self):
        fake = FakeOpener({"content": [{"type": "text", "text": "Hi"}], "usage": {"input_tokens": 5, "output_tokens": 1}})
        reply = chat(cfg("anthropic"), "sys", "u", schema=SCHEMA, opener=fake)
        req = fake.requests[0]
        self.assertEqual(req["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(req["headers"]["x-api-key"], "sk-secret-123")
        self.assertEqual(req["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(req["body"]["system"][:3], "sys")
        self.assertIn("intent", req["body"]["system"])       # schema goes into the instructions
        self.assertEqual((reply.text, reply.input_tokens), ("Hi", 5))

    def test_list_models(self):
        fake = FakeOpener({"data": [{"id": "claude-a"}]})
        self.assertEqual(list_models(cfg("anthropic"), opener=fake), ["claude-a"])


class TestOllama(unittest.TestCase):
    def test_native_chat_with_format(self):
        fake = FakeOpener({"message": {"content": "{\"intent\": \"question\"}"}, "prompt_eval_count": 9, "eval_count": 4})
        reply = chat(cfg("ollama", mode="local_only"), "s", "u", schema=SCHEMA, opener=fake)
        req = fake.requests[0]
        self.assertEqual(req["url"], "http://localhost:11434/api/chat")
        self.assertEqual(req["body"]["format"], SCHEMA)
        self.assertIs(req["body"]["stream"], False)
        self.assertEqual((reply.input_tokens, reply.output_tokens), (9, 4))

    def test_v1_suffix_is_stripped(self):
        fake = FakeOpener({"models": [{"name": "llama3.2"}, {"name": "qwen3"}]})
        models = list_models(cfg("ollama", mode="local_only", base_url="http://localhost:11434/v1"), opener=fake)
        self.assertEqual(fake.requests[0]["url"], "http://localhost:11434/api/tags")
        self.assertEqual(models, ["llama3.2", "qwen3"])


class TestErrors(unittest.TestCase):
    def test_local_server_not_running(self):
        fake = FakeOpener(urllib.error.URLError(ConnectionRefusedError(61, "refused")))
        with self.assertRaises(AIError) as err:
            chat(cfg("ollama", mode="local_only"), "s", "u", opener=fake)
        self.assertIn("isn't running", str(err.exception))

    def test_bad_key(self):
        with self.assertRaises(AIError) as err:
            chat(cfg("openai"), "s", "u", opener=FakeOpener(http_error(401, "invalid key sk-secret-123")))
        self.assertIn("rejected the key", str(err.exception))
        self.assertNotIn("sk-secret-123", str(err.exception))

    def test_missing_model(self):
        with self.assertRaises(AIError) as err:
            chat(cfg("ollama", mode="local_only"), "s", "u", opener=FakeOpener(http_error(404, "model 'm1' not found")))
        self.assertIn("ollama pull m1", str(err.exception))

    def test_timeout(self):
        with self.assertRaises(AIError) as err:
            chat(cfg("openai"), "s", "u", opener=FakeOpener(socket.timeout()))
        self.assertIn("seconds", str(err.exception))

    def test_refuses_disallowed_url_before_any_request(self):
        fake = FakeOpener()
        with self.assertRaises(AIError):
            chat(cfg("openai", mode="local_only"), "s", "u", opener=fake)
        self.assertEqual(fake.requests, [])

    def test_hosted_without_key(self):
        c = load_ai_config({"ai": {"mode": "hosted", "provider": "openai", "model": "m"}}, environ={})
        with self.assertRaises(AIError) as err:
            chat(c, "s", "u", opener=FakeOpener())
        self.assertIn("key", str(err.exception))

    def test_unreadable_answer(self):
        with self.assertRaises(AIError):
            chat(cfg("openai"), "s", "u", opener=FakeOpener({"weird": True}))


class TestHelpers(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(extract_json('Sure! {"intent": "question"} done'), {"intent": "question"})
        self.assertEqual(extract_json('```json\n{"a": {"b": 1}}\n```'), {"a": {"b": 1}})
        self.assertIsNone(extract_json("no json here"))
        self.assertIsNone(extract_json("{broken"))

    def test_redact(self):
        self.assertEqual(redact("key sk-1 and sk-1", ["sk-1", "", None]), "key [hidden] and [hidden]")


if __name__ == "__main__":
    unittest.main()
