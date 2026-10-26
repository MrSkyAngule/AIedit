import unittest

from aiedit import translator
from aiedit.translator import Translator, chunks, has_cyrillic, mask, unmask

RU_EN = {"добавь": "add", "проверку": "a check", "в": "in", "функцию": "function", "и": "and",
         "исправь": "fix", "ошибку": "the bug", "файле": "file", "что": "what", "делает": "does"}
EN_RU = {"Added": "Добавил", "a": "", "check": "проверку", "to": "в", "the": "", "function": "функцию",
         "Fixed": "Исправил", "bug": "ошибку", "in": "в", "file": "файле"}


def word_swap(table):
    def reply(text: str) -> str:
        return " ".join(table.get(w, w) for w in text.split(" ") if table.get(w, w) != "")
    return reply


class Fake:
    """Транспорт без сети: записывает запросы и 'переводит' подстановкой слов."""
    def __init__(self, ru_en=None, en_ru=None, fail=None):
        self.calls, self.fail = [], fail
        self.swap = {"ru-en": word_swap(ru_en or RU_EN), "en-ru": word_swap(en_ru or EN_RU)}

    def __call__(self, payload):
        if self.fail:
            raise self.fail
        system, user = payload["messages"][0]["content"], payload["messages"][1]["content"]
        direction = "ru-en" if "from Russian" in system else "en-ru"
        self.calls.append((direction, user, payload))
        return self.swap[direction](user)


class MaskTest(unittest.TestCase):
    def roundtrip(self, text):
        masked, saved = mask(text)
        return masked, saved, unmask(masked, saved)

    def test_code_paths_identifiers_urls_are_masked_and_restored(self):
        text = ("Поправь `charge(user)` в pkg/pay.py и функцию make_order, смотри https://x.io/a?b=1 "
                "и вызов pay.charge(), флаг --force, класс OrderItem и getUser\n```py\nx = 'привет'\n```")
        masked, saved, back = self.roundtrip(text)
        self.assertEqual(back, text)
        for piece in ("charge(user)", "pkg/pay.py", "make_order", "https://x.io/a?b=1", "pay.charge()", "--force",
                      "OrderItem", "getUser", "привет"):
            self.assertNotIn(piece, masked, piece)
        self.assertIn("Поправь", masked)

    def test_secret_lines_never_stay_in_masked_text(self):
        text = 'Почини вот это:\nKEY = "sk-ant-api03-AbCdEf0123456789xyz"\nи ещё тест'
        masked, saved, back = self.roundtrip(text)
        self.assertNotIn("sk-ant", masked)
        self.assertEqual(back, text)

    def test_lost_or_duplicated_token_is_detected(self):
        masked, saved = mask("сделай `a` и `b`")
        self.assertIsNone(unmask(masked.replace("{{2}}", ""), saved))
        self.assertIsNone(unmask(masked + " {{1}}", saved))
        self.assertIsNone(unmask(masked.replace("{{1}}", "{{3}}"), saved))

    def test_plain_text_is_unchanged(self):
        self.assertEqual(mask("просто текст без кода"), ("просто текст без кода", []))

    def test_chunks_respect_limit_and_keep_text(self):
        text = "\n\n".join(f"абзац {i} " + "x" * 400 for i in range(20))
        parts = chunks(text, 1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual("\n\n".join(parts), text)
        long_line = "y" * 5000
        self.assertEqual("".join(chunks(long_line, 1000)), long_line)

    def test_language_detection(self):
        self.assertTrue(has_cyrillic("привет"))
        self.assertFalse(has_cyrillic("hello"))
        self.assertFalse(has_cyrillic("hello", 0.2) or has_cyrillic("a" * 100 + "я", 0.2))


class TranslateTest(unittest.TestCase):
    def test_ru_to_en_sends_only_prose(self):
        fake = Fake()
        t = Translator(fake)
        res = t.ru_to_en("добавь проверку в функцию `charge` и файле pkg/pay.py")
        self.assertTrue(res.translated)
        self.assertEqual(res.text, "add a check in function `charge` and file pkg/pay.py")
        sent = fake.calls[0][1]
        self.assertNotIn("charge", sent)
        self.assertNotIn("pay.py", sent)
        self.assertEqual(fake.calls[0][2]["model"], "gpt-4o-mini")
        self.assertEqual(fake.calls[0][2]["temperature"], 0)

    def test_en_to_ru_keeps_code_blocks_local(self):
        fake = Fake()
        text = "Added a check to the function.\n```py\ndef f(): return 'secret_value_xyz'\n```"
        res = Translator(fake).en_to_ru(text)
        self.assertTrue(res.translated)
        self.assertIn("```py\ndef f(): return 'secret_value_xyz'\n```", res.text)
        self.assertNotIn("secret_value_xyz", fake.calls[0][1])
        self.assertIn("Добавил", res.text)

    def test_secret_line_never_reaches_translator(self):
        fake = Fake()
        text = 'добавь проверку\nKEY = "sk-ant-api03-AbCdEf0123456789xyz"'
        res = Translator(fake).ru_to_en(text)
        self.assertIn("sk-ant-api03-AbCdEf0123456789xyz", res.text)
        self.assertNotIn("sk-ant", fake.calls[0][1])

    def test_no_call_when_not_needed(self):
        fake = Fake()
        t = Translator(fake)
        for res in (t.ru_to_en("already english text"), t.ru_to_en(""), t.en_to_ru("уже по-русски"),
                    t.ru_to_en("`только_код()`"), t.en_to_ru("```py\nx = 1\n```")):
            self.assertFalse(res.translated)
        self.assertEqual(fake.calls, [])

    def test_failure_returns_original_with_note(self):
        res = Translator(Fake(fail=OSError("offline"))).ru_to_en("добавь проверку")
        self.assertFalse(res.translated)
        self.assertEqual(res.text, "добавь проверку")
        self.assertIn("недоступен", res.note)
        self.assertIn("offline", res.note)

    def test_lost_placeholder_falls_back_to_original(self):
        bad = Fake(ru_en={"добавь": "add", "{{1}}": ""})
        res = Translator(bad).ru_to_en("добавь `x`")
        self.assertFalse(res.translated)
        self.assertEqual(res.text, "добавь `x`")
        self.assertIn("отброшен", res.note)

    def test_absurd_length_is_rejected(self):
        res = Translator(lambda p: "x" * 5000).ru_to_en("добавь")
        self.assertFalse(res.translated)

    def test_cache_avoids_second_call(self):
        fake = Fake()
        t = Translator(fake)
        t.ru_to_en("добавь проверку")
        t.ru_to_en("добавь проверку")
        self.assertEqual(len(fake.calls), 1)

    def test_failure_is_not_cached(self):
        flaky = {"n": 0}

        def transport(payload):
            flaky["n"] += 1
            if flaky["n"] == 1:
                raise OSError("once")
            return "add a check"
        t = Translator(transport)
        self.assertFalse(t.ru_to_en("добавь проверку").translated)
        self.assertTrue(t.ru_to_en("добавь проверку").translated)

    def test_text_with_braces_tokens_is_not_translated(self):
        fake = Fake()
        res = Translator(fake).ru_to_en("добавь {{1}} проверку")
        self.assertFalse(res.translated)
        self.assertEqual(fake.calls, [])

    def test_too_long_text_is_skipped(self):
        fake = Fake()
        res = Translator(fake).ru_to_en("слово " * (translator.MAX_TEXT // 5))
        self.assertFalse(res.translated)
        self.assertEqual(fake.calls, [])

    def test_long_text_goes_in_several_requests(self):
        fake = Fake()
        text = "\n\n".join("добавь проверку в файле " + "и " * 300 for _ in range(5))
        res = Translator(fake).ru_to_en(text)
        self.assertTrue(res.translated)
        self.assertGreater(len(fake.calls), 1)

    def test_log_callback_gets_direction_and_size_not_text(self):
        seen = []
        Translator(Fake(), log=lambda d, n: seen.append((d, n))).ru_to_en("добавь проверку")
        self.assertEqual(seen, [("ru-en", len("добавь проверку"))])

    def test_from_env(self):
        import os
        old = os.environ.pop("OPENAI_API_KEY", None)
        try:
            self.assertIsNone(Translator.from_env())
            os.environ["OPENAI_API_KEY"] = "sk-test"
            self.assertIsNotNone(Translator.from_env())
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
            if old:
                os.environ["OPENAI_API_KEY"] = old


if __name__ == "__main__":
    unittest.main()


class TransportTest(unittest.TestCase):
    """Проверка HTTP-транспорта на локальном сервере, который отвечает как chat/completions."""

    def serve(self, status=200, body=None):
        import http.server
        import json
        import threading
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["path"] = self.path
                seen["auth"] = self.headers.get("Authorization")
                seen["type"] = self.headers.get("Content-Type")
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                data = json.dumps(body if body is not None else
                                  {"choices": [{"message": {"content": "add a check"}}]}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/v1/chat/completions", seen

    def test_request_shape_and_response_parsing(self):
        url, seen = self.serve()
        tr = Translator(translator.openai_transport("sk-test-key", url))
        res = tr.ru_to_en("добавь проверку")
        self.assertTrue(res.translated)
        self.assertEqual(res.text, "add a check")
        self.assertEqual(seen["auth"], "Bearer sk-test-key")
        self.assertEqual(seen["type"], "application/json")
        self.assertEqual(seen["path"], "/v1/chat/completions")
        body = seen["body"]
        self.assertEqual((body["model"], body["temperature"]), ("gpt-4o-mini", 0))
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][1]["content"], "добавь проверку")

    def test_http_error_falls_back_to_original(self):
        url, _ = self.serve(status=401, body={"error": {"message": "bad key"}})
        res = Translator(translator.openai_transport("sk-bad", url)).ru_to_en("добавь проверку")
        self.assertFalse(res.translated)
        self.assertEqual(res.text, "добавь проверку")
        self.assertIn("401", res.note)

    def test_unexpected_json_falls_back(self):
        url, _ = self.serve(body={"unexpected": True})
        res = Translator(translator.openai_transport("sk-x", url)).ru_to_en("добавь проверку")
        self.assertFalse(res.translated)
        self.assertIn("недоступен", res.note)

    def test_connection_refused_falls_back(self):
        res = Translator(translator.openai_transport("sk-x", "http://127.0.0.1:9/v1/chat/completions", timeout=2)
                         ).ru_to_en("добавь проверку")
        self.assertFalse(res.translated)


class QuotedLiteralsTest(unittest.TestCase):
    def test_quoted_strings_are_protected_but_apostrophes_are_not(self):
        from aiedit import translator as t
        masked, saved = t.mask("выведи результат с подписью 'Сумма:' и \"Привет\" и «Готово»")
        self.assertEqual(saved, ["'Сумма:'", '"Привет"', "«Готово»"])
        masked, saved = t.mask("he said it's fine and we don't know why")
        self.assertEqual(saved, [])  # апостроф в слове не открывает кавычку

    def test_quoted_label_survives_translation_unchanged(self):
        from aiedit.translator import Translator
        sent = []

        def transport(payload):
            sent.append(payload["messages"][-1]["content"])
            return "print the result with the label {{1}}"
        res = Translator(transport).ru_to_en("выведи результат с подписью 'Сумма:'")
        self.assertTrue(res.translated)
        self.assertEqual(res.text, "print the result with the label 'Сумма:'")
        self.assertNotIn("Сумма", sent[0])  # переводчик строку не видел
