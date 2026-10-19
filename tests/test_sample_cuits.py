"""Every Argentine CUIT in the sample corpus must fail its check digit.

A CUIT that passes the modulo-11 check is a well-formed identifier, and a
well-formed identifier in a public repository can collide with a real company.
Making them all fail the check keeps the samples obviously synthetic without
making them look wrong to a reader.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: Weights applied to the first ten digits, from the AFIP specification.
CUIT_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)

#: Matches a CUIT as printed: two digits, eight digits, one check digit.
CUIT_PATTERN = re.compile(r"\b(\d{2})-(\d{8})-(\d)\b")

GENERATORS = (
    Path(__file__).resolve().parent.parent / "scripts" / "generate_hard_samples.py",
    Path(__file__).resolve().parent.parent / "scripts" / "generate_samples.py",
)


def check_digit(first_ten: str) -> int:
    """The check digit a real CUIT would carry for these ten digits."""
    total = sum(int(digit) * weight for digit, weight in zip(first_ten, CUIT_WEIGHTS))
    remainder = total % 11
    if remainder == 0:
        return 0
    if remainder == 1:
        return 9
    return 11 - remainder


def is_valid(cuit: str) -> bool:
    """True when the printed check digit matches the computed one."""
    digits = cuit.replace("-", "")
    return int(digits[10]) == check_digit(digits[:10])


def find_cuits() -> list[tuple[Path, int, str]]:
    """Every CUIT-shaped string in the generators, with where it came from."""
    found: list[tuple[Path, int, str]] = []
    for path in GENERATORS:
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in CUIT_PATTERN.finditer(line):
                found.append((path, number, match.group(0)))
    return found


def test_the_check_digit_algorithm_is_right():
    """Guard the guard: a wrong algorithm would pass everything silently."""
    # 30-71234567-1 is the well-formed form of one of the sample prefixes.
    assert check_digit("3071234567") == 1
    assert is_valid("30-71234567-1")
    assert not is_valid("30-71234567-8")


def test_the_corpus_actually_contains_cuits():
    """If the pattern stops matching, the test below would pass vacuously."""
    assert len(find_cuits()) >= 3


@pytest.mark.parametrize(
    "path, line, cuit",
    find_cuits(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_sample_cuit_fails_its_check_digit(path: Path, line: int, cuit: str):
    assert not is_valid(cuit), (
        f"{path.name}:{line} prints {cuit}, which passes the modulo-11 check and "
        f"could therefore belong to a real company. Change the last digit to "
        f"anything other than {check_digit(cuit.replace('-', '')[:10])}."
    )
