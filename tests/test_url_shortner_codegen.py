import unittest

from url_shortner.codegen import (
    ALPHABET,
    CODE_LENGTH,
    generate_code,
    is_valid_alias,
)


class GenerateCodeTests(unittest.TestCase):
    def test_default_length(self) -> None:
        code = generate_code()
        self.assertEqual(len(code), CODE_LENGTH)

    def test_custom_length(self) -> None:
        code = generate_code(length=12)
        self.assertEqual(len(code), 12)

    def test_uses_only_alphabet_characters(self) -> None:
        for _ in range(100):
            code = generate_code()
            for ch in code:
                self.assertIn(ch, ALPHABET)


class IsValidAliasTests(unittest.TestCase):
    def test_accepts_letters_digits_dash_underscore(self) -> None:
        self.assertTrue(is_valid_alias("q3-roadmap"))
        self.assertTrue(is_valid_alias("team_offsite"))
        self.assertTrue(is_valid_alias("ab"))
        self.assertTrue(is_valid_alias("A" * 32))

    def test_rejects_too_short(self) -> None:
        self.assertFalse(is_valid_alias("a"))

    def test_rejects_too_long(self) -> None:
        self.assertFalse(is_valid_alias("a" * 33))

    def test_rejects_disallowed_characters(self) -> None:
        self.assertFalse(is_valid_alias("hello world"))
        self.assertFalse(is_valid_alias("hello/world"))
        self.assertFalse(is_valid_alias("hello.world"))
        self.assertFalse(is_valid_alias(""))


if __name__ == "__main__":
    unittest.main()
