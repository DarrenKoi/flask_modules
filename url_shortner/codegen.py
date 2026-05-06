"""Short-code generation and custom-alias validation."""

import re
import secrets
import string

ALPHABET = string.digits + string.ascii_letters
CODE_LENGTH = 7
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,32}$")


def generate_code(length: int = CODE_LENGTH) -> str:
    """Return a random base62 short code of the given length."""

    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def is_valid_alias(alias: str) -> bool:
    """Return True if the alias matches the allowed pattern."""

    return bool(ALIAS_PATTERN.fullmatch(alias))
