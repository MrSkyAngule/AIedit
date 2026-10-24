import unittest

from aiedit.guard import Region
from aiedit.secrets_scan import scan, suspicious_file, to_ranges, uncovered


def lines_of(text):
    return [f.line for f in scan(text)]


class SecretsTest(unittest.TestCase):
    def test_known_token_formats(self):
        text = "\n".join([
            'a = "sk-ant-api03-AbCdEf0123456789xyz"',
            "gh = ghp_" + "A1b2C3d4E5" * 4,
            "aws = AKIAABCDEFGHIJKLMNOP",
            "slack = xoxb-1234567890-abcdef",
            "jwt = eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4",
        ])
        self.assertEqual(lines_of(text), [0, 1, 2, 3, 4])

    def test_quoted_assignments(self):
        text = 'password = "hunter2hunter2"\nDB_TOKEN: "abc123456"\nmy_api_key="zzzzzz1234"'
        self.assertEqual(lines_of(text), [0, 1, 2])

    def test_dotenv_style(self):
        self.assertEqual(lines_of("STRIPE_SECRET=abcdef123456\nDEBUG=1\nexport API_TOKEN=tok_123456"), [0, 2])

    def test_private_key_block_marks_every_line(self):
        text = "x = 1\n-----BEGIN RSA PRIVATE KEY-----\nMIIE...\nabc\n-----END RSA PRIVATE KEY-----\ny = 2"
        self.assertEqual(lines_of(text), [1, 2, 3, 4])

    def test_no_false_positives_on_normal_code(self):
        text = "\n".join([
            'password = os.environ["PASSWORD"]',
            "token = get_token()",
            'secret = "<your secret>"',
            'api_key = "${API_KEY}"',
            'password = ""',
            "def check_password(user, password):",
            "tokens = len(text)",
            'x = "sk-short"',
        ])
        self.assertEqual(lines_of(text), [])

    def test_uncovered_ignores_hidden_and_interface(self):
        text = 'a = "sk-ant-api03-AbCdEf0123456789xyz"\nb = "sk-ant-api03-ZyXwVu9876543210abc"\nc = 1'
        found = scan(text)
        self.assertEqual([f.line for f in uncovered(found, [Region("hidden", 0, 0)], 3)], [1])
        self.assertEqual([f.line for f in uncovered(found, [Region("interface", 1, 1)], 3)], [0])
        self.assertEqual(len(uncovered(found, [Region("readonly", 0, 1)], 3)), 2)

    def test_ranges_merge_neighbours(self):
        text = 'password = "aaaaaaaa"\npassword2 = "bbbbbbbb"\nx = 1\ntoken = "cccccccc"'
        self.assertEqual(to_ranges(scan(text)), [(0, 1), (3, 3)])

    def test_suspicious_file_names(self):
        for name in (".env", ".env.local", "server.pem", "id_rsa", "credentials-prod.json", "a.key"):
            self.assertTrue(suspicious_file(name), name)
        for name in (".env.example", "main.py", "keyboard.py", "envelope.txt"):
            self.assertFalse(suspicious_file(name), name)


if __name__ == "__main__":
    unittest.main()


class PerformanceTest(unittest.TestCase):
    def test_long_lines_scan_fast(self):
        import time
        for line in ("x" * 200_000, "a=1;" * 50_000, "password" * 20_000, "A" * 100_000 + "=1",
                     '"token":' * 20_000):
            start = time.monotonic()
            scan(line)
            self.assertLess(time.monotonic() - start, 2.0, line[:20])

    def test_secret_deep_inside_long_line_still_found(self):
        line = "var a=1;" * 5000 + 'password="hunter2hunter2";' + "var b=2;" * 5000
        self.assertEqual([f.line for f in scan(line)], [0])
