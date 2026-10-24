import unittest

from aiedit import outbound, secrets_scan
from aiedit.outbound import Gate, SendCancelled
from aiedit.roster import Roster, Worker
from aiedit.translator import Translator
from tests.test_conductor_run import PLAN, RunCase, Script

KEY = "sk-ant-api03-AbCdEf0123456789xyz"


class Asker:
    """Отвечает за человека и запоминает, о чём спросили."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, findings, model):
        self.asked.append((model, findings))
        return self.answers.pop(0) if self.answers else "mask"


class SpansTest(unittest.TestCase):
    def test_marks_only_the_value_not_the_whole_line(self):
        text = f"почему не работает ключ {KEY} в запросе?"
        (a, b, kind), = secrets_scan.spans(text)
        self.assertEqual(text[a:b], KEY)
        self.assertEqual(kind, "ключ API (sk-...)")

    def test_password_value_and_offsets_on_later_lines(self):
        text = 'x = 1\ndb_password = "hunter2hunter2"\n'
        (a, b, _), = secrets_scan.spans(text)
        self.assertEqual(text[a:b], "hunter2hunter2")

    def test_private_key_block_and_unfinished_block(self):
        text = "a\n-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\nb"
        (a, b, kind), = secrets_scan.spans(text)
        self.assertTrue(text[a:b].startswith("-----BEGIN") and text[a:b].endswith("KEY-----"))
        self.assertEqual(kind, "приватный ключ")
        cut = "a\n-----BEGIN PRIVATE KEY-----\nMIIE"
        (a, b, _), = secrets_scan.spans(cut)
        self.assertEqual(b, len(cut))

    def test_placeholders_are_not_secrets(self):
        self.assertEqual(secrets_scan.spans('api_key = "<your-key>"\npassword = "changeme"'), [])


class GateTest(unittest.TestCase):
    def test_masks_what_goes_out_and_restores_the_reply(self):
        inner = Script("m", ["use <<masked-1>> as before"])
        gate = Gate(decide=Asker("mask"))
        reply = gate.wrap(inner).complete("sys", [{"role": "user", "content": f"key {KEY} fails"}])
        self.assertNotIn(KEY, inner.sent())
        self.assertIn("key <<masked-1>> fails", inner.sent())
        self.assertEqual(reply, f"use {KEY} as before")

    def test_decision_is_remembered_and_asked_once(self):
        ask = Asker("mask")
        gate = Gate(decide=ask)
        p = gate.wrap(Script("m", []))
        for _ in range(3):
            p.complete("s", [{"role": "user", "content": f"k={KEY}"}])
        self.assertEqual(len(ask.asked), 1)
        gate.reset()
        p.complete("s", [{"role": "user", "content": f"k={KEY}"}])
        self.assertEqual(len(ask.asked), 2)

    def test_send_as_is_passes_untouched(self):
        inner = Script("m", [])
        Gate(decide=Asker("send")).wrap(inner).complete("s", [{"role": "user", "content": KEY}])
        self.assertIn(KEY, inner.sent())

    def test_cancel_stops_before_sending_and_is_not_remembered(self):
        ask = Asker("cancel", "mask")
        inner = Script("m", [])
        p = Gate(decide=ask).wrap(inner)
        with self.assertRaises(SendCancelled):
            p.complete("s", [{"role": "user", "content": KEY}])
        self.assertEqual(inner.seen, [])
        p.complete("s", [{"role": "user", "content": KEY}])  # спросили снова, теперь скрыли
        self.assertEqual(len(ask.asked), 2)
        self.assertNotIn(KEY, inner.sent())

    def test_unknown_answer_counts_as_cancel(self):
        with self.assertRaises(SendCancelled):
            Gate(decide=lambda f, m: None).wrap(Script("m", [])).complete("s", [{"role": "user", "content": KEY}])

    def test_words_are_case_insensitive_and_keep_original_spelling(self):
        inner = Script("m", ["<<masked-1>> and <<masked-2>>"])
        gate = Gate(["Acme"], decide=Asker("mask"))
        reply = gate.wrap(inner).complete("s", [{"role": "user", "content": "ACME pays AcmeClient"}])
        self.assertNotIn("acme", inner.sent().lower())
        self.assertEqual(sorted(gate.labels), ["ACME", "Acme"])
        self.assertEqual(reply, f"{gate.values['<<masked-1>>']} and {gate.values['<<masked-2>>']}")

    def test_too_short_words_are_dropped(self):
        self.assertEqual(Gate(["a", " ", "ab", "Acme"]).words, ["Acme", "ab"])

    def test_closed_code_only_when_pasted_by_human(self):
        gate = Gate()
        closed = ["    return amount * 0.07  # fee", "x = 1", "    pass"]
        self.assertEqual(gate.watch_closed("почему тут\n    return amount * 0.07  # fee\nне так?", closed), 1)
        self.assertEqual([f.kind for f in gate.find("return amount * 0.07  # fee")], ["строка закрытого кода"])
        self.assertEqual(gate.find("x = 1"), [])  # короткие строки не следим

    def test_disabled_gate_checks_nothing(self):
        ask = Asker()
        inner = Script("m", [])
        Gate(decide=ask, enabled=False).wrap(inner).complete("s", [{"role": "user", "content": KEY}])
        self.assertEqual(ask.asked, [])
        self.assertIn(KEY, inner.sent())

    def test_log_has_kinds_but_no_values(self):
        entries = []
        gate = Gate(["Acme"], decide=Asker("mask"), log=entries.append)
        gate.wrap(Script("model-x", [])).complete("s", [{"role": "user", "content": f"Acme {KEY}"}])
        self.assertEqual(entries, [{"mode": "filter", "model": "model-x", "decision": "mask",
                                    "found": {"ключ API (sk-...)": 1, "слово из списка": 1}}])
        self.assertNotIn(KEY, str(entries))

    def test_preview_never_shows_whole_secret(self):
        f = outbound.Finding("ключ API (sk-...)", KEY)
        self.assertNotIn(KEY, f.preview())
        self.assertTrue(f.preview().startswith("sk-ant"))
        self.assertEqual(outbound.Finding("слово из списка", "Acme").preview(), "Acme")

    def test_wrapper_looks_like_the_provider(self):
        gate = Gate()
        inner = Script("model-x", [])
        p = gate.wrap(inner)
        self.assertEqual(p.name, "model-x")
        self.assertIs(gate.wrap(p), p)  # дважды не оборачиваем

    def test_wrap_roster_keeps_workers_and_guards_providers(self):
        a, b = Script("a", []), Script("b", [])
        roster = Roster([Worker("sonnet", a, "x", 1, 1, "mid"), Worker("glm", b, "x", 1, 1, "mid", "security")])
        guarded = Gate(decide=Asker("mask")).wrap_roster(roster)
        self.assertEqual(guarded.keys(), ["sonnet", "glm"])
        self.assertIsNotNone(guarded.reviewer())
        guarded.get("glm").provider.complete("s", [{"role": "user", "content": KEY}])
        self.assertNotIn(KEY, b.sent())


class TranslatorGateTest(unittest.TestCase):
    def test_translator_gets_masked_text(self):
        seen = []

        def transport(payload):
            seen.append(payload["messages"][-1]["content"])
            return "pay <<masked-1>> today"
        gate = Gate(["Acme"], decide=Asker("mask"))
        res = Translator(gate.wrap_transport(transport, "gpt-4o-mini")).ru_to_en("заплати Acme сегодня")
        self.assertNotIn("Acme", seen[0])
        self.assertEqual(res.text, "pay Acme today")

    def test_cancel_in_translator_is_not_swallowed(self):
        gate = Gate(["Acme"], decide=Asker("cancel"))
        tr = Translator(gate.wrap_transport(lambda p: "x", "gpt-4o-mini"))
        with self.assertRaises(SendCancelled):
            tr.ru_to_en("заплати Acme сегодня")


class ConductorGateTest(RunCase):
    def test_cancel_in_a_worker_stops_the_whole_plan(self):
        roster = self.roster(sonnet=[SendCancelled("no")], haiku=["hello"])
        with self.assertRaises(SendCancelled):
            self.conduct(PLAN, roster)
        self.assertEqual(self.p["haiku"].seen, [])


if __name__ == "__main__":
    unittest.main()
