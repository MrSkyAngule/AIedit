"""Потоковая выдача, повторы запросов и таймауты провайдеров."""
import http.server
import json
import threading
import unittest
import urllib.error
from unittest import mock

from aiedit import catalog, connections
from aiedit.providers import AnthropicProvider, OpenAIProvider

MSG = [{"role": "user", "content": "hi"}]


def sse(*events) -> bytes:
    return "".join(f"data: {e if isinstance(e, str) else json.dumps(e)}\n\n" for e in events).encode()


def scripted(testcase, *responses):
    """Локальный сервер: каждому запросу отвечает следующим из списка (status, content_type, body, headers)."""
    seen, queue = [], list(responses)

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            status, ctype, body, headers = (queue.pop(0) if len(queue) > 1 else queue[0])
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    testcase.addCleanup(srv.server_close)
    testcase.addCleanup(srv.shutdown)
    return f"http://127.0.0.1:{srv.server_port}", seen


def ok(text="hello"):
    return 200, "application/json", json.dumps({"choices": [{"message": {"content": text}}]}).encode(), None


def fail(code, headers=None):
    return code, "application/json", b'{"error":"x"}', headers


class OpenAIStreamTest(unittest.TestCase):
    def test_text_is_assembled_and_usage_read_from_the_last_chunk(self):
        body = sse({"choices": [{"delta": {"content": "Hel"}}]}, {"choices": [{"delta": {"content": "lo"}}]},
                   {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 7,
                                             "prompt_tokens_details": {"cached_tokens": 20}}}, "[DONE]")
        url, seen = scripted(self, (200, "text/event-stream", body, None))
        p = OpenAIProvider("m", "k", url=url + "/v1/chat/completions")
        got = []
        p.set_progress(got.append)
        self.assertEqual(p.complete("S", MSG), "Hello")
        self.assertEqual(seen[0]["stream"], True)
        self.assertEqual(seen[0]["stream_options"], {"include_usage": True})
        self.assertEqual(p.last_usage, {"input": 30, "cache_read": 20, "cache_write": 0, "output": 7})
        self.assertEqual(got[-1], 5)  # сколько символов пришло

    def test_error_event_in_the_stream_is_an_error_not_an_empty_answer(self):
        url, _ = scripted(self, (200, "text/event-stream", sse({"error": {"message": "overloaded"}}), None))
        with self.assertRaisesRegex(RuntimeError, "overloaded"):
            OpenAIProvider("m", "k", url=url).complete("S", MSG)

    def test_server_that_ignores_streaming_and_answers_json_still_works(self):
        url, _ = scripted(self, ok("plain"))
        self.assertEqual(OpenAIProvider("m", url=url).complete("S", MSG), "plain")

    def test_server_that_rejects_streaming_gets_one_retry_without_it(self):
        url, seen = scripted(self, fail(400), ok("no stream"))
        p = OpenAIProvider("m", url=url)
        self.assertEqual(p.complete("S", MSG), "no stream")
        self.assertEqual([("stream" in b) for b in seen], [True, False])
        self.assertFalse(p.stream)  # запомнили: дальше сразу без потока


class AnthropicStreamTest(unittest.TestCase):
    def test_text_and_usage_from_events(self):
        body = sse({"type": "message_start", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 80,
                                                                     "cache_creation_input_tokens": 5, "output_tokens": 1}}},
                   {"type": "content_block_start", "content_block": {"type": "text"}},
                   {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Дел"}},
                   {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{}"}},
                   {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "аю"}},
                   {"type": "message_delta", "usage": {"output_tokens": 12}}, {"type": "message_stop"})
        url, seen = scripted(self, (200, "text/event-stream", body, None))
        p = AnthropicProvider("claude", "k", url=url)
        self.assertEqual(p.complete("S", MSG), "Делаю")
        self.assertTrue(seen[0]["stream"])
        self.assertEqual(p.last_usage, {"input": 10, "cache_read": 80, "cache_write": 5, "output": 12})

    def test_error_event(self):
        url, _ = scripted(self, (200, "text/event-stream", sse({"type": "error", "error": {"message": "Overloaded"}}), None))
        with self.assertRaisesRegex(RuntimeError, "Overloaded"):
            AnthropicProvider("c", "k", url=url).complete("S", MSG)


class RetryTest(unittest.TestCase):
    def provider(self, url):
        p = OpenAIProvider("m", "k", url=url)
        self.slept = []
        p.sleep = self.slept.append
        return p

    def test_temporary_server_error_is_retried_with_a_pause(self):
        url, seen = scripted(self, fail(503), ok("done"))
        self.assertEqual(self.provider(url).complete("S", MSG), "done")
        self.assertEqual((len(seen), self.slept), (2, [2.0]))

    def test_retry_after_header_is_respected(self):
        url, _ = scripted(self, fail(429, {"Retry-After": "1"}), ok())
        self.provider(url).complete("S", MSG)
        self.assertEqual(self.slept, [1.0])

    def test_gives_up_after_three_attempts_and_raises_the_last_error(self):
        url, seen = scripted(self, fail(503))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.provider(url).complete("S", MSG)
        self.assertEqual((cm.exception.code, len(seen), self.slept), (503, 3, [2.0, 6.0]))

    def test_bad_key_and_bad_address_are_not_retried(self):
        for code in (401, 403, 404):
            url, seen = scripted(self, fail(code))
            with self.assertRaises(urllib.error.HTTPError):
                self.provider(url).complete("S", MSG)
            self.assertEqual(len(seen), 1, code)
        self.assertEqual(self.slept, [])

    def test_timeout_and_dns_failure_are_retried(self):
        class Resp:
            headers = type("H", (), {"get_content_type": staticmethod(lambda: "application/json")})()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return json.dumps({"choices": [{"message": {"content": "after"}}]}).encode()
        p = self.provider("http://x/v1")
        errors = [TimeoutError("The read operation timed out"), urllib.error.URLError("Временный сбой в разрешении имен")]
        with mock.patch("urllib.request.urlopen", side_effect=errors + [Resp()]):
            self.assertEqual(p.complete("S", MSG), "after")
        self.assertEqual(self.slept, [2.0, 6.0])

    def test_connection_dropped_in_the_middle_of_a_stream_starts_over_without_duplicates(self):
        class Dropping:
            headers = type("H", (), {"get_content_type": staticmethod(lambda: "text/event-stream")})()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                yield from sse({"choices": [{"delta": {"content": "Кусок "}}]}).splitlines(keepends=True)
                raise ConnectionResetError("обрыв")

        class Whole(Dropping):
            def __iter__(self):
                yield from sse({"choices": [{"delta": {"content": "Целый ответ"}}]}, "[DONE]").splitlines(keepends=True)
        p = self.provider("http://x/v1")
        with mock.patch("urllib.request.urlopen", side_effect=[Dropping(), Whole()]):
            self.assertEqual(p.complete("S", MSG), "Целый ответ")
        self.assertEqual(self.slept, [2.0])

    def test_ping_answers_at_once_and_never_waits_for_retries(self):
        url, seen = scripted(self, fail(503))
        p = OpenAIProvider("m", url=url)
        p.sleep = lambda s: self.fail("ping не должен ждать повторов")
        self.assertEqual(connections.ping(p), (False, "ошибка сервиса (HTTP 503): x"))
        self.assertEqual(len(seen), 1)
        self.assertEqual(p.retries, 2)  # настройка вернулась на место


class TimeoutsTest(unittest.TestCase):
    def test_worker_waits_longer_than_conductor_and_translator(self):
        self.assertEqual(catalog.make_provider("opus", "k").timeout, 300)
        self.assertEqual(catalog.make_provider("opus", "k", role="conductor").timeout, 60)
        gw = "https://gw.example/v1"
        self.assertEqual([catalog.make_provider("sonnet", "k", gw, role=r).timeout for r in ("worker", "conductor", "translator")],
                         [300, 90, 60])
        self.assertEqual(catalog.make_provider("custom", "", "m | http://localhost:11434/v1").timeout, 300)


class ErrorDetailTest(unittest.TestCase):
    QUOTA = (403, "application/json", json.dumps({"error": {"message": "预扣费额度失败, 用户剩余额度: $0.0004",
                                                           "code": "insufficient_user_quota"}}, ensure_ascii=False).encode(), None)

    def test_error_carries_the_services_reason_and_keeps_its_type_and_code(self):
        url, _ = scripted(self, (403, "application/json", json.dumps({"error": {"message": "Quota exceeded for this key"}}).encode(), None))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            OpenAIProvider("m", "k", url=url).complete("S", MSG)
        self.assertEqual(cm.exception.code, 403)
        self.assertIn("Quota exceeded for this key", str(cm.exception))

    def test_ping_says_money_ran_out_instead_of_a_vague_403(self):
        url, _ = scripted(self, (403, "application/json", json.dumps({"error": {"message": "insufficient_user_quota"}}).encode(), None))
        ok_, text = connections.ping(OpenAIProvider("m", "k", url=url))
        self.assertFalse(ok_)
        self.assertIn("закончились деньги или лимит", text)

    def test_a_real_403_without_a_quota_message_stays_a_403(self):
        url, _ = scripted(self, (403, "application/json", b'{"error":{"message":"invalid api key"}}', None))
        self.assertEqual(connections.ping(OpenAIProvider("m", "k", url=url)), (False, "доступ запрещён (HTTP 403): invalid api key"))

    def test_quota_words_in_several_languages(self):
        for text in ("insufficient_user_quota", "You exceeded your current quota", "Insufficient balance", "额度不足", "余额不足"):
            self.assertTrue(connections.is_quota_error(text), text)
        for text in ("invalid api key", "model not found", "rate limit"):
            self.assertFalse(connections.is_quota_error(text), text)


class PingReasonTest(unittest.TestCase):
    def test_ping_shows_why_the_service_refused(self):
        url, _ = scripted(self, (503, "application/json", json.dumps({"error": {"message": "system disk overloaded (current: 98%)"}}).encode(), None))
        p = OpenAIProvider("m", "k", url=url)
        p.retries = 0
        self.assertEqual(connections.ping(p), (False, "ошибка сервиса (HTTP 503): system disk overloaded (current: 98%)"))

    def test_bad_key_stays_short(self):
        url, _ = scripted(self, (401, "application/json", b'{"error":{"message":"bad key sk-secret"}}', None))
        self.assertEqual(connections.ping(OpenAIProvider("m", "k", url=url)), (False, "ключ не принят (HTTP 401)"))


class KeyIsNeverShownTest(unittest.TestCase):
    def test_key_echoed_by_the_service_is_scrubbed_from_the_error(self):
        secret = "sk-ABCDEF1234567890secret"
        url, _ = scripted(self, (403, "application/json", json.dumps({"error": {"message": f"bad key {secret}, token token_abcdefgh123 refused"}}).encode(), None))
        p = OpenAIProvider("m", secret, url=url)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            p.complete("S", MSG)
        shown = str(cm.exception)
        self.assertNotIn("ABCDEF1234567890", shown)
        self.assertNotIn("abcdefgh123", shown)
        self.assertIn("[key]", shown)
        self.assertNotIn("ABCDEF1234567890", connections.ping(p)[1])
