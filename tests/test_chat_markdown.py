"""Ответы ИИ в чате показываются с форматированием, а чужой HTML остаётся текстом."""
import unittest

from tests.test_gui import QApplication

from aiedit import chat


class MarkdownHtmlTest(unittest.TestCase):
    def test_bold_italic_and_code(self):
        h = chat.markdown_html("**Структура:** и *важно*, вызови `f(x)`")
        self.assertIn("<b>Структура:</b>", h)
        self.assertIn("<i>важно</i>", h)
        self.assertIn("f(x)</span>", h)
        self.assertNotIn("**", h)

    def test_nothing_is_formatted_inside_code(self):
        h = chat.markdown_html("`**не жирный**` и `a * b * c`")
        self.assertNotIn("<b>", h)
        self.assertNotIn("<i>", h)
        self.assertIn("**не жирный**", h)

    def test_lists_headings_and_nested_bullets(self):
        h = chat.markdown_html("# Итог\n- первый\n  - вложенный\n* второй")
        self.assertIn("<b>Итог</b>", h)
        self.assertIn("• первый", h)
        self.assertIn("&nbsp;&nbsp;• вложенный", h)
        self.assertIn("• второй", h)

    def test_code_block_keeps_text_and_whitespace_untouched(self):
        h = chat.markdown_html("вот:\n```python\nx = '**a**'\nif 1 < 2:\n    pass\n```\nвсё")
        self.assertIn("<pre", h)
        self.assertIn("x = '**a**'", h)
        self.assertIn("1 &lt; 2", h)
        self.assertIn("    pass", h)
        self.assertNotIn("```", h)
        self.assertTrue(h.rstrip().endswith("всё"))

    def test_unclosed_code_block_runs_to_the_end(self):
        self.assertIn("<pre", chat.markdown_html("```\nsomething"))

    def test_foreign_html_stays_text(self):
        h = chat.markdown_html("<script>alert(1)</script> **<b>x</b>**")
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)
        self.assertIn("<b>&lt;b&gt;x&lt;/b&gt;</b>", h)

    def test_plain_asterisks_and_math_are_left_alone(self):
        for text in ("2 * 3 * 4", "a*b", "5 ** 2 = 25", "** не закрыто"):
            self.assertNotIn("<i>", chat.markdown_html(text), text)
        self.assertIn("a*b", chat.markdown_html("a*b"))

    def test_pasted_answer_from_the_user_renders_without_stars(self):
        text = ("**Структура:**\n- Строка 1: Скрытый код (2 строки), определяющий переменную `ars` — список слов\n\n"
                "**Наблюдения:**\n1. **Весь код скрыт** — я вижу только сигнатуры")
        h = chat.markdown_html(text)
        self.assertNotIn("**", h)
        self.assertEqual(h.count("<b>"), 3)


@unittest.skipUnless(QApplication, "нет Qt")
class ChatRenderTest(unittest.TestCase):
    def test_ai_bubble_is_formatted_but_the_users_is_literal(self):
        ai = chat.bubble_html("ai", "**жирно**")
        user = chat.bubble_html("user", "**жирно** <b>")
        self.assertIn("<b>жирно</b>", ai)
        self.assertIn("**жирно**", user)
        self.assertIn("&lt;b&gt;", user)

    def test_panel_shows_no_stars_but_keeps_the_raw_text_for_saving(self):
        panel = chat.ChatPanel()
        panel.add_ai("**Структура:**\n- пункт `x`")
        shown = panel.history.toPlainText()
        self.assertNotIn("**", shown)
        self.assertNotIn("`", shown)
        self.assertIn("Структура:", shown)
        self.assertIn("• пункт x", shown)
        self.assertEqual(panel.messages[-1][1], "**Структура:**\n- пункт `x`")  # в файле чата - как написала модель
        panel.reset(panel.messages)  # перерисовка после загрузки даёт то же
        self.assertNotIn("**", panel.history.toPlainText())
        panel.deleteLater()
