"""Tests for user_slug derivation from UUID."""

import pytest
from src.provisioning.schema_manager import derive_user_slug_from_uuid


def test_derive_user_slug_from_uuid_standard_format():
    """Test UUID with hyphens → underscores."""
    result = derive_user_slug_from_uuid("921514dd-512e-44e1-8f0c-f58090027bd9")
    assert result == "921514dd_512e_44e1_8f0c_f58090027bd9"
    assert len(result) == 36
    assert "-" not in result
    assert result.count("_") == 4


def test_derive_user_slug_from_uuid_all_zeros():
    """Test edge case: all zeros."""
    result = derive_user_slug_from_uuid("00000000-0000-0000-0000-000000000000")
    assert result == "00000000_0000_0000_0000_000000000000"


def test_derive_user_slug_from_uuid_all_fs():
    """Test edge case: all Fs."""
    result = derive_user_slug_from_uuid("ffffffff-ffff-ffff-ffff-ffffffffffff")
    assert result == "ffffffff_ffff_ffff_ffff_ffffffffffff"


def test_derive_user_slug_from_uuid_uppercase():
    """Test that uppercase UUIDs are lowercased."""
    result = derive_user_slug_from_uuid("921514DD-512E-44E1-8F0C-F58090027BD9")
    assert result == "921514dd_512e_44e1_8f0c_f58090027bd9"
    assert result.islower()


def test_derive_user_slug_from_uuid_empty_raises():
    """Test that empty UUID raises ValueError."""
    with pytest.raises(ValueError, match="cannot be empty or None"):
        derive_user_slug_from_uuid("")


def test_derive_user_slug_from_uuid_none_raises():
    """Test that None raises ValueError."""
    with pytest.raises(ValueError, match="cannot be empty or None"):
        derive_user_slug_from_uuid(None)


def test_derive_user_slug_immutability():
    """Test that schema name doesn't change across calls."""
    user_id = "550e8400-e29b-41d4-a716-446655440000"
    result1 = derive_user_slug_from_uuid(user_id)
    result2 = derive_user_slug_from_uuid(user_id)
    assert result1 == result2  # Deterministic
    assert result1 == "550e8400_e29b_41d4_a716_446655440000"


def test_derive_user_slug_collision_resistance():
    """Test that different UUIDs produce different slugs."""
    uuid1 = "921514dd-512e-44e1-8f0c-f58090027bd9"
    uuid2 = "921514dd-512e-44e1-8f0c-f58090027bd8"
    slug1 = derive_user_slug_from_uuid(uuid1)
    slug2 = derive_user_slug_from_uuid(uuid2)
    assert slug1 != slug2


def test_derive_user_slug_multiple_uuids():
    """Test multiple different UUIDs produce expected formats."""
    test_cases = [
        ("550e8400-e29b-41d4-a716-446655440000", "550e8400_e29b_41d4_a716_446655440000"),
        ("6ba7b810-9dad-11d1-80b4-00c04fd430c8", "6ba7b810_9dad_11d1_80b4_00c04fd430c8"),
        ("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "a0eebc99_9c0b_4ef8_bb6d_6bb9bd380a11"),
    ]
    for input_uuid, expected_slug in test_cases:
        result = derive_user_slug_from_uuid(input_uuid)
        assert result == expected_slug, f"Failed for {input_uuid}: got {result}, expected {expected_slug}"


def test_derive_user_slug_no_hyphens_in_output():
    """Test that output contains NO hyphens."""
    result = derive_user_slug_from_uuid("921514dd-512e-44e1-8f0c-f58090027bd9")
    assert "-" not in result
    assert "_" in result


def test_derive_user_slug_schema_name_format():
    """Test that derived slug is suitable for PostgreSQL schema names."""
    result = derive_user_slug_from_uuid("921514dd-512e-44e1-8f0c-f58090027bd9")
    # PostgreSQL identifiers must be alphanumeric + underscore
    assert all(c.isalnum() or c == "_" for c in result)
    # Schema prefix + underscore + slug should work
    schema_name = f"faultline_{result}"
    assert schema_name == "faultline_921514dd_512e_44e1_8f0c_f58090027bd9"
