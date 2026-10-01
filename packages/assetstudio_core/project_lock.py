"""`assetstudio.lock.json` (ProjectAssetLockV1, INT-SPEC-1.0 §7 "Minimum lock structure")."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .delivery import (
    MAX_FILES,
    AssetRef,
    DeliveryId,
    Frozen,
    Representation,
    Sha256Hex,
    Slug,
    VersionText,
    document_bytes,
    strict_loads,
    unique_values,
)


class Generator(Frozen):
    addon_version: VersionText
    installer_version: VersionText


class LockDelivery(Frozen):
    delivery_id: DeliveryId
    manifest_sha256: Sha256Hex
    profile_id: Slug
    profile_version: Slug


class LockDependency(Frozen):
    asset_ref: AssetRef
    descriptor_sha256: Sha256Hex
    deliveries: Annotated[dict[Representation, LockDelivery], Field(min_length=1)]
    requires: list[Sha256Hex]


class MaterialPolicy(Frozen):
    mode: Literal["preserve", "project_mapping", "override"]
    profile_id: Slug | None
    profile_sha256: Sha256Hex | None

    @model_validator(mode="after")
    def _profile(self) -> MaterialPolicy:
        has_id, has_sha = self.profile_id is not None, self.profile_sha256 is not None
        if has_id != has_sha:
            raise ValueError("profile_id and profile_sha256 are both set or both null")
        if self.mode == "preserve" and has_id:
            raise ValueError("preserve takes no profile")
        if self.mode == "project_mapping" and not has_id:
            raise ValueError("project_mapping needs a profile")
        return self


class Binding(Frozen):
    asset_key: Sha256Hex
    representation: Representation
    material_policy: MaterialPolicy
    update_policy: Literal["prompt"]


class Root(Frozen):
    owner_kind: Literal["scene_binding", "world_generation"]
    owner_id: Slug
    asset_keys: Annotated[list[Sha256Hex], Field(min_length=1, max_length=MAX_FILES)]


class ProjectAssetLockV1(Frozen):
    schema_version: Literal[1]
    generator: Generator
    dependencies: dict[Sha256Hex, LockDependency]
    bindings: dict[Slug, Binding]
    roots: list[Root]

    @model_validator(mode="after")
    def _semantics(self) -> ProjectAssetLockV1:
        for key, dep in self.dependencies.items():
            if key != dep.asset_ref.key():
                raise ValueError(f"dependency key {key} does not match its asset_ref")
            unique_values(dep.requires, f"requires of {key}")
            missing = [r for r in dep.requires if r not in self.dependencies]
            if missing:
                raise ValueError(f"{key} requires keys missing from the lock: {missing}")
        self._check_acyclic()
        for bid, b in self.bindings.items():
            dep = self.dependencies.get(b.asset_key)
            if dep is None or b.representation not in dep.deliveries:
                raise ValueError(f"binding {bid} points at a missing dependency or representation")
        for root in self.roots:
            unique_values(root.asset_keys, f"root {root.owner_id}")
            if any(k not in self.dependencies for k in root.asset_keys):
                raise ValueError(f"root {root.owner_id} references unknown asset keys")
        return self

    def _check_acyclic(self) -> None:
        state: dict[str, int] = {}  # 1 = on stack, 2 = done

        def visit(key: str) -> None:
            if state.get(key) == 2:
                return
            if state.get(key) == 1:
                raise ValueError(f"dependency cycle through {key}")
            state[key] = 1
            for r in self.dependencies[key].requires:
                visit(r)
            state[key] = 2

        for key in self.dependencies:
            visit(key)


def lock_bytes(lock: ProjectAssetLockV1) -> bytes:
    return document_bytes(lock)


def parse_lock(raw: bytes) -> ProjectAssetLockV1:
    return ProjectAssetLockV1.model_validate(strict_loads(raw))
