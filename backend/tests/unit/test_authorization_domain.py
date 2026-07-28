"""Unit matrix for immutable authorization domain values."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.domain.resources import (
    PrincipalKind,
    PrincipalRef,
    PrincipalSet,
    ResourceAncestry,
    ResourceRef,
    ResourceScope,
)


@pytest.mark.parametrize(
    ("factory", "kind", "principal_id"),
    [
        (PrincipalRef.user, PrincipalKind.USER, 1),
        (PrincipalRef.group, PrincipalKind.GROUP, 27),
    ],
)
def test_principal_ref_factories(
    factory: object,
    kind: PrincipalKind,
    principal_id: int,
) -> None:
    principal = factory(principal_id)  # type: ignore[operator]

    assert principal == PrincipalRef(kind, principal_id)


@pytest.mark.parametrize("principal_id", [None, True, False, 0, -1, 1.0, "1"])
def test_principal_ref_rejects_invalid_ids(principal_id: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        PrincipalRef(PrincipalKind.USER, principal_id)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["USER", "GROUP", "user", None, object()])
def test_principal_ref_rejects_untyped_kinds(kind: object) -> None:
    with pytest.raises(ValueError, match="PrincipalKind"):
        PrincipalRef(kind, 1)  # type: ignore[arg-type]


def test_principal_set_normalizes_duplicate_groups_and_has_stable_order() -> None:
    user = PrincipalRef.user(9)
    group_two = PrincipalRef.group(2)
    group_seven = PrincipalRef.group(7)

    principals = PrincipalSet(user, [group_seven, group_two, group_seven])

    assert principals.user == user
    assert principals.groups == frozenset({group_two, group_seven})
    assert principals.ordered == (user, group_two, group_seven)
    assert tuple(principals) == principals.ordered
    assert len(principals) == 3
    assert user in principals
    assert group_two in principals
    assert PrincipalRef.group(100) not in principals
    assert [] not in principals


def test_principal_set_from_ids_normalizes_generator_input() -> None:
    principals = PrincipalSet.from_ids(4, (group_id for group_id in [8, 3, 8]))

    assert principals == PrincipalSet(
        PrincipalRef.user(4),
        [PrincipalRef.group(3), PrincipalRef.group(8)],
    )


@pytest.mark.parametrize(
    ("user", "groups", "message"),
    [
        (PrincipalRef.group(1), [], "USER"),
        ("user-1", [], "USER"),
        (PrincipalRef.user(1), [PrincipalRef.user(2)], "GROUP"),
        (PrincipalRef.user(1), ["group-2"], "GROUP"),
        (PrincipalRef.user(1), [{}], "GROUP"),
    ],
)
def test_principal_set_rejects_invalid_principal_roles(
    user: object,
    groups: list[object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        PrincipalSet(user, groups)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("resource", "scope", "resource_id"),
    [
        (ResourceRef.global_scope(), ResourceScope.GLOBAL, None),
        (ResourceRef.cabinet(3), ResourceScope.CABINET, 3),
        (ResourceRef.folder(8), ResourceScope.FOLDER, 8),
        (ResourceRef.document(13), ResourceScope.DOC, 13),
    ],
)
def test_resource_ref_factories(
    resource: ResourceRef,
    scope: ResourceScope,
    resource_id: int | None,
) -> None:
    assert resource == ResourceRef(scope, resource_id)


@pytest.mark.parametrize(
    "scope", ["GLOBAL", "CABINET", "FOLDER", "DOC", None, object()]
)
def test_resource_ref_rejects_untyped_scope(scope: object) -> None:
    with pytest.raises(ValueError, match="ResourceScope"):
        ResourceRef(scope, None)  # type: ignore[arg-type]


@pytest.mark.parametrize("resource_id", [True, False, 0, -1, 1.0, "1", None])
@pytest.mark.parametrize(
    "scope",
    [ResourceScope.CABINET, ResourceScope.FOLDER, ResourceScope.DOC],
)
def test_scoped_resource_ref_rejects_invalid_ids(
    scope: ResourceScope,
    resource_id: object,
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ResourceRef(scope, resource_id)  # type: ignore[arg-type]


@pytest.mark.parametrize("resource_id", [0, 1, -1, True])
def test_global_resource_ref_rejects_every_id(resource_id: int) -> None:
    with pytest.raises(ValueError, match="cannot have"):
        ResourceRef(ResourceScope.GLOBAL, resource_id)


def test_resource_scope_ranks_follow_policy_specificity() -> None:
    resources = [
        ResourceRef.global_scope(),
        ResourceRef.cabinet(1),
        ResourceRef.folder(1),
        ResourceRef.document(1),
    ]

    assert [resource.specificity_rank for resource in resources] == [0, 1, 2, 3]


def test_complete_ancestry_preserves_root_to_target_order_and_specificity() -> None:
    global_ref = ResourceRef.global_scope()
    root_cabinet = ResourceRef.cabinet(10)
    nearest_cabinet = ResourceRef.cabinet(11)
    root_folder = ResourceRef.folder(20)
    nearest_folder = ResourceRef.folder(21)
    document = ResourceRef.document(30)
    resources = (
        global_ref,
        root_cabinet,
        nearest_cabinet,
        root_folder,
        nearest_folder,
        document,
    )

    ancestry = ResourceAncestry(resources)

    assert ancestry.resources == resources
    assert ancestry.target == document
    assert ancestry.document == document
    assert ancestry.cabinets == (root_cabinet, nearest_cabinet)
    assert ancestry.folders == (root_folder, nearest_folder)
    assert ancestry.most_specific_first == tuple(reversed(resources))
    assert tuple(ancestry) == resources
    assert len(ancestry) == 6
    assert nearest_folder in ancestry
    assert ancestry.specificity_of(global_ref) == 0
    assert ancestry.specificity_of(root_folder) == 3
    assert ancestry.specificity_of(nearest_folder) == 4
    assert ancestry.specificity_of(document) == 5


@pytest.mark.parametrize(
    ("resources", "target"),
    [
        ([ResourceRef.global_scope()], ResourceRef.global_scope()),
        (
            [ResourceRef.global_scope(), ResourceRef.cabinet(1)],
            ResourceRef.cabinet(1),
        ),
        (
            [
                ResourceRef.global_scope(),
                ResourceRef.cabinet(1),
                ResourceRef.folder(2),
            ],
            ResourceRef.folder(2),
        ),
    ],
)
def test_ancestry_supports_each_non_document_target(
    resources: list[ResourceRef],
    target: ResourceRef,
) -> None:
    ancestry = ResourceAncestry(resources)

    assert ancestry.target == target
    assert ancestry.document is None


@pytest.mark.parametrize(
    ("resources", "message"),
    [
        ([], "cannot be empty"),
        ([ResourceRef.cabinet(1)], "start at GLOBAL"),
        (
            [ResourceRef.global_scope(), ResourceRef.folder(1)],
            "requires a cabinet",
        ),
        (
            [
                ResourceRef.global_scope(),
                ResourceRef.cabinet(1),
                ResourceRef.document(3),
            ],
            "requires a folder",
        ),
        (
            [
                ResourceRef.global_scope(),
                ResourceRef.cabinet(1),
                ResourceRef.folder(2),
                ResourceRef.cabinet(3),
            ],
            "ordered",
        ),
        (
            [
                ResourceRef.global_scope(),
                ResourceRef.cabinet(1),
                ResourceRef.folder(2),
                ResourceRef.global_scope(),
            ],
            "duplicate",
        ),
        (
            [
                ResourceRef.global_scope(),
                ResourceRef.cabinet(1),
                ResourceRef.cabinet(1),
            ],
            "duplicate",
        ),
        (
            [
                ResourceRef.global_scope(),
                ResourceRef.cabinet(1),
                ResourceRef.folder(2),
                ResourceRef.document(3),
                ResourceRef.document(4),
            ],
            "at most one DOC",
        ),
    ],
)
def test_ancestry_rejects_invalid_paths(
    resources: list[ResourceRef],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ResourceAncestry(resources)


def test_ancestry_rejects_non_resource_members() -> None:
    with pytest.raises(ValueError, match="only ResourceRef"):
        ResourceAncestry([ResourceRef.global_scope(), "folder-2"])  # type: ignore[list-item]


def test_ancestry_specificity_rejects_unrelated_resource() -> None:
    ancestry = ResourceAncestry(
        [
            ResourceRef.global_scope(),
            ResourceRef.cabinet(1),
        ]
    )

    with pytest.raises(ValueError, match="not present"):
        ancestry.specificity_of(ResourceRef.cabinet(2))


def test_domain_values_are_immutable_hashable_and_compare_by_value() -> None:
    principal = PrincipalRef.user(1)
    principals = PrincipalSet.from_ids(1, [3, 2, 3])
    resource = ResourceRef.document(4)
    ancestry = ResourceAncestry(
        [
            ResourceRef.global_scope(),
            ResourceRef.cabinet(1),
            ResourceRef.folder(2),
            resource,
        ]
    )

    assert len({principal, PrincipalRef.user(1)}) == 1
    assert len({principals, PrincipalSet.from_ids(1, [2, 3])}) == 1
    assert len({resource, ResourceRef.document(4)}) == 1
    assert (
        len(
            {
                ancestry,
                ResourceAncestry(
                    [
                        ResourceRef.global_scope(),
                        ResourceRef.cabinet(1),
                        ResourceRef.folder(2),
                        ResourceRef.document(4),
                    ]
                ),
            }
        )
        == 1
    )

    with pytest.raises(FrozenInstanceError):
        principal.principal_id = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        principals.groups = frozenset()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        resource.resource_id = 5  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        ancestry.resources = ()  # type: ignore[misc]
