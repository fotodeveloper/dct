"""Tests for dct.core.ids — ULID generation and validation."""

from dct.core.ids import new_ulid, is_valid_ulid


def test_new_ulid_is_26_chars():
    assert len(new_ulid()) == 26


def test_new_ulid_is_crockford_base32():
    value = new_ulid()
    assert is_valid_ulid(value)


def test_new_ulid_uppercase():
    value = new_ulid()
    assert value == value.upper()


def test_new_ulid_excludes_ambiguous_chars():
    """Crockford alphabet omits I, L, O, U to avoid OCR/human confusion."""
    value = new_ulid()
    for forbidden in "ILOU":
        assert forbidden not in value, f"{forbidden!r} present in {value!r}"


def test_new_ulid_uniqueness():
    """Two back-to-back ULIDs differ — low-entropy collision would be a bug."""
    first, second = new_ulid(), new_ulid()
    assert first != second


def test_new_ulid_monotonic_within_ms():
    """ULIDs sort in creation order (time-based prefix). Sequential calls
    within the same millisecond still preserve order through the randomness
    component; across milliseconds the timestamp drives the order."""
    # 100 ULIDs generated back-to-back should sort lexicographically to
    # creation order. This is the key property that makes ULIDs preferable
    # to UUID4 for our plan sections.
    values = [new_ulid() for _ in range(100)]
    assert values == sorted(values)


def test_is_valid_ulid_accepts_real_ulid():
    assert is_valid_ulid(new_ulid())


def test_is_valid_ulid_rejects_too_short():
    assert not is_valid_ulid("01HX9Z4K")


def test_is_valid_ulid_rejects_too_long():
    assert not is_valid_ulid("01HX9Z4K5M7PQRSTVWXYZ01HX9Z4K")


def test_is_valid_ulid_rejects_forbidden_chars():
    # 'I' is not in the Crockford alphabet
    assert not is_valid_ulid("01HX9Z4K5M7PQRSTVWXYZ0I001")
    # 'L' neither
    assert not is_valid_ulid("01HX9Z4K5M7PQRSTVWXYZ0L001")
    # lowercase rejected (we canonicalize to uppercase)
    assert not is_valid_ulid("01hx9z4k5m7pqrstvwxyz00001")


def test_is_valid_ulid_rejects_empty():
    assert not is_valid_ulid("")
