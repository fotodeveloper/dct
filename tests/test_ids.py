"""Tests for dct.core.ids — ULID generation and validation."""

from dct.core.ids import is_valid_ulid, new_ulid


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


def test_new_ulid_time_ordered_across_milliseconds():
    """ULIDs are time-sortable: the 10-char timestamp prefix is non-decreasing
    in generation order. Within one millisecond the ULID spec leaves ordering
    to the random component — python-ulid does NOT guarantee strict
    monotonicity there (measured ~2 inversions per 10k adjacent pairs), and
    dct does not rely on it: explicit sort_order columns drive intra-plan
    ordering. Asserting full lexicographic order made this test flaky."""
    values = [new_ulid() for _ in range(100)]
    stamps = [v[:10] for v in values]
    assert stamps == sorted(stamps)
    assert len(set(values)) == len(values)


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
