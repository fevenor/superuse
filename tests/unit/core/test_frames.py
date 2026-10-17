"""V01 coordinate basis mapping and lifecycle."""

from __future__ import annotations

import math
import uuid

import pytest

from superuse.core.errors import ControlError, ErrorCode
from superuse.core.frames import BasisRegistry, map_point


def make_registry() -> BasisRegistry:
    return BasisRegistry("11111111-1111-1111-1111-111111111111")


def register(registry: BasisRegistry, *, width=1280, height=720, generation=1, now=0.0):
    return registry.register(
        capture_generation=generation,
        geometry_id=str(uuid.uuid4()),
        image_width=width,
        image_height=height,
        native_width=1920,
        native_height=1080,
        now=now,
    )


def test_v01_point_mapping_and_boundaries():
    registry = make_registry()
    basis = register(registry)
    assert map_point(basis, 0, 0) == (0, 0)
    assert map_point(basis, 1279, 719) == (4092, 4090)  # floor(1279*4096/1280), floor(719*4096/720)
    assert map_point(basis, 640, 360) == (2048, 2048)
    assert map_point(basis, 1, 1) == (3, 5)  # floor(4096/1280)=3, floor(4096/720)=5

    with pytest.raises(ControlError) as ei:
        map_point(basis, 1280, 0)
    assert ei.value.code is ErrorCode.COORDINATE_OUT_OF_BOUNDS
    with pytest.raises(ControlError):
        map_point(basis, 0, 720)
    with pytest.raises(ControlError):
        map_point(basis, -1, 0)
    with pytest.raises(ControlError):
        map_point(basis, math.nan, 0)
    with pytest.raises(ControlError):
        map_point(basis, math.inf, 0)


def test_v01_basis_identity_reuse_and_stale():
    registry = make_registry()
    basis = register(registry, generation=3)
    same = registry.register(
        capture_generation=3,
        geometry_id=basis.geometry_id,
        image_width=1280,
        image_height=720,
        native_width=1920,
        native_height=1080,
        now=1.0,
    )
    assert same.basis_id == basis.basis_id  # same key -> same basis

    with pytest.raises(ControlError) as stale:
        registry.resolve("22222222-2222-2222-2222-222222222222")
    assert stale.value.code is ErrorCode.STALE_BASIS

    registry.invalidate_generation(3)
    with pytest.raises(ControlError):
        registry.resolve(basis.basis_id, now=1.0)


def test_v01_lru_capacity_and_ttl():
    registry = make_registry()
    first = register(registry, generation=1, now=0.0)
    # Fill beyond capacity: the first (least recently used) basis is evicted.
    for gen in range(2, 70):
        register(registry, generation=gen, now=0.0)
    assert len(registry) == 64
    with pytest.raises(ControlError):
        registry.resolve(first.basis_id, now=0.0)

    keep = register(registry, generation=100, now=10.0)
    assert registry.resolve(keep.basis_id, now=10.0).basis_id == keep.basis_id
    dropped = registry.sweep(now=10.0 + 61.0)
    assert dropped >= 1
    with pytest.raises(ControlError):
        registry.resolve(keep.basis_id, now=10.0 + 61.0)
