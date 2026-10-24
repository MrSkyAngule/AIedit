import base64
import tempfile
import unittest
from pathlib import Path

from aiedit import agent, images, prompts
from aiedit.policy import Rules
from aiedit.providers import AnthropicProvider, OpenAIProvider
from aiedit.workspace import Workspace
from tests.test_agent import Script, tool

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 10


class Seeing(Script):
    vision = True


class ImagesTest(unittest.TestCase):
    def test_types_and_limits(self):
        self.assertEqual([images.media_type(d) for d in (PNG, JPG, WEBP, b"GIF89a..", b"hello")],
                         ["image/png", "image/jpeg", "image/webp", "image/gif", None])
        with self.assertRaises(ValueError):
            images.register(b"not an image", "x.txt")
        with self.assertRaises(ValueError):
            images.register(PNG + b"\x00" * images.MAX_BYTES, "big.png")

    def test_split_and_strip(self):
        mark = images.register(PNG, "shot.png")
        parts = images.split(f"look {mark} here <<image:99999>>")
        self.assertEqual(parts[0], ("text", "look "))
        self.assertEqual(parts[1], ("image", ("image/png", base64.b64encode(PNG).decode())))
        self.assertEqual(parts[2], ("text", " here <<image:99999>>"))  # чужая метка - просто текст
        self.assertEqual(images.strip(f"a {mark}"), "a [image: shot.png]")
        self.assertEqual(images.marks_in(f"{mark} <<image:99999>>"), [mark])


class ProviderBodyTest(unittest.TestCase):
    def test_anthropic_blocks_only_in_user_messages(self):
        mark = images.register(PNG, "a.png")
        body = AnthropicProvider("claude-sonnet-5-5", "k").request_body("s", [
            {"role": "user", "content": f"see {mark}"}, {"role": "assistant", "content": f"echo {mark}"},
            {"role": "user", "content": "plain"}])
        blocks = body["messages"][0]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": "see "})
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["source"]["media_type"], "image/png")
        self.assertEqual(body["messages"][1]["content"], f"echo {mark}")
        self.assertEqual(body["messages"][2]["content"], "plain")

    def test_openai_vision_and_not(self):
        mark = images.register(JPG, "b.jpg")
        body = OpenAIProvider("gpt-4o").request_body("s", [{"role": "user", "content": mark}])
        (block,) = body["messages"][1]["content"]
        self.assertTrue(block["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        body = OpenAIProvider("glm-5.3").request_body("s", [{"role": "user", "content": f"x {mark}"}])
        self.assertEqual(body["messages"][1]["content"], "x [image: b.jpg]")

    def test_vision_guess_by_name(self):
        self.assertTrue(AnthropicProvider("claude-opus-5-5", "k").vision)
        self.assertEqual([OpenAIProvider(m).vision for m in ("gpt-4o-mini", "o3-mini", "o4-mini", "glm-4.5v", "llama3")],
                         [True, False, True, True, False])


class AgentImageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "p"
        (self.root / "assets").mkdir(parents=True)
        (self.root / "closed").mkdir()
        (self.root / "assets" / "logo.png").write_bytes(PNG)
        (self.root / "closed" / "plan.png").write_bytes(PNG)
        (self.root / "notes.txt").write_text("text")
        Rules(self.root).set_denied(self.root / "closed", True)
        self.ws = Workspace(self.root)

    def tearDown(self):
        self.tmp.cleanup()


class ViewImageTest(AgentImageCase):
    def test_view_image_returns_a_mark(self):
        out = self.ws.view_image("assets/logo.png")
        self.assertTrue(out.startswith("image assets/logo.png:\n<<image:"))
        self.assertEqual(images.name_of(images.marks_in(out)[0]), "assets/logo.png")

    def test_closed_missing_and_non_images(self):
        self.assertTrue(self.ws.view_image("closed/plan.png").startswith("ERROR: file not found"))
        self.assertTrue(self.ws.view_image("nope.png").startswith("ERROR: file not found"))
        self.assertIn("not a PNG", self.ws.view_image("notes.txt"))
        (self.root / "huge.png").write_bytes(PNG + b"\x00" * images.MAX_BYTES)
        self.assertIn("too large", self.ws.view_image("huge.png"))

    def test_agent_tool_only_for_models_that_see(self):
        prov = Seeing([tool("view_image", path="assets/logo.png"), "a red logo"])
        res = agent.run("what is the logo", self.ws, prov)
        self.assertIn("view_image", prov.seen[0][0])
        self.assertTrue(images.marks_in(prov.everything()))
        self.assertIn("смотрю картинку assets/logo.png", res.calls)
        prov = Script([tool("view_image", path="assets/logo.png"), "ok"])
        agent.run("t", Workspace(self.root), prov)
        self.assertNotIn("view_image", prov.seen[0][0])
        self.assertIn("ERROR: this model cannot see images", prov.everything())

    def test_attached_images_in_first_message(self):
        mark = images.register(PNG, "error.png")
        prov = Seeing(["ok"])
        agent.run("fix this", self.ws, prov, attached=[mark])
        first = prov.seen[0][1][0]["content"]
        self.assertIn(f"{prompts.SEC_IMAGES}\n{mark}", first)
        prov = Script(["ok"])
        res = agent.run("fix this", Workspace(self.root), prov, attached=[mark])
        self.assertIn("[image: error.png]", prov.seen[0][1][0]["content"])
        self.assertNotIn(mark, prov.everything())
        self.assertIn("не принимает картинки", res.notes[0])


if __name__ == "__main__":
    unittest.main()
