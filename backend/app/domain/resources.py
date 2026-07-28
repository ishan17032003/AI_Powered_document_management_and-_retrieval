"""Immutable identities and resource hierarchy values for authorization.

These types deliberately contain no database or policy-evaluation behavior. They
form the validated boundary between ancestry/principal resolution and the pure
authorization evaluator.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum


class PrincipalKind(StrEnum):
    """Principal kinds that may receive resource access rules."""

    USER = "USER"
    GROUP = "GROUP"


class ResourceScope(StrEnum):
    """Supported authorization scopes, from broadest to most specific."""

    GLOBAL = "GLOBAL"
    CABINET = "CABINET"
    FOLDER = "FOLDER"
    DOC = "DOC"


_SCOPE_RANK = {
    ResourceScope.GLOBAL: 0,
    ResourceScope.CABINET: 1,
    ResourceScope.FOLDER: 2,
    ResourceScope.DOC: 3,
}


def _require_positive_id(value: object, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class PrincipalRef:
    """A validated USER or GROUP principal identifier."""

    kind: PrincipalKind
    principal_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PrincipalKind):
            raise ValueError("kind must be PrincipalKind.USER or PrincipalKind.GROUP")
        _require_positive_id(self.principal_id, field="principal_id")

    @classmethod
    def user(cls, user_id: int) -> PrincipalRef:
        return cls(PrincipalKind.USER, user_id)

    @classmethod
    def group(cls, group_id: int) -> PrincipalRef:
        return cls(PrincipalKind.GROUP, group_id)


@dataclass(frozen=True, slots=True, init=False)
class PrincipalSet:
    """One direct user principal plus their unique active group principals."""

    user: PrincipalRef
    groups: frozenset[PrincipalRef]

    def __init__(
        self,
        user: PrincipalRef,
        groups: Iterable[PrincipalRef] = (),
    ) -> None:
        if not isinstance(user, PrincipalRef) or user.kind is not PrincipalKind.USER:
            raise ValueError("user must be a USER PrincipalRef")

        provided_groups = tuple(groups)
        if any(
            not isinstance(group, PrincipalRef) or group.kind is not PrincipalKind.GROUP
            for group in provided_groups
        ):
            raise ValueError("groups must contain only GROUP PrincipalRef values")
        normalized_groups = frozenset(provided_groups)

        object.__setattr__(self, "user", user)
        object.__setattr__(self, "groups", normalized_groups)

    @classmethod
    def from_ids(
        cls,
        user_id: int,
        group_ids: Iterable[int] = (),
    ) -> PrincipalSet:
        return cls(
            user=PrincipalRef.user(user_id),
            groups=(PrincipalRef.group(group_id) for group_id in group_ids),
        )

    @property
    def ordered(self) -> tuple[PrincipalRef, ...]:
        """Return the direct user first, followed by groups in stable ID order."""

        return (self.user, *sorted(self.groups, key=lambda group: group.principal_id))

    def __contains__(self, principal: object) -> bool:
        return isinstance(principal, PrincipalRef) and (
            principal == self.user or principal in self.groups
        )

    def __iter__(self) -> Iterator[PrincipalRef]:
        return iter(self.ordered)

    def __len__(self) -> int:
        return 1 + len(self.groups)


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """A validated GLOBAL, CABINET, FOLDER, or DOC authorization target."""

    scope: ResourceScope
    resource_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ResourceScope):
            raise ValueError("scope must be a ResourceScope value")
        if self.scope is ResourceScope.GLOBAL:
            if self.resource_id is not None:
                raise ValueError("GLOBAL scope cannot have a resource_id")
            return
        _require_positive_id(self.resource_id, field="resource_id")

    @classmethod
    def global_scope(cls) -> ResourceRef:
        return cls(ResourceScope.GLOBAL)

    @classmethod
    def cabinet(cls, cabinet_id: int) -> ResourceRef:
        return cls(ResourceScope.CABINET, cabinet_id)

    @classmethod
    def folder(cls, folder_id: int) -> ResourceRef:
        return cls(ResourceScope.FOLDER, folder_id)

    @classmethod
    def document(cls, document_id: int) -> ResourceRef:
        return cls(ResourceScope.DOC, document_id)

    @property
    def specificity_rank(self) -> int:
        """Return the scope-level rank; ancestry position breaks same-scope ties."""

        return _SCOPE_RANK[self.scope]


@dataclass(frozen=True, slots=True, init=False)
class ResourceAncestry:
    """A complete resource path ordered from GLOBAL to the exact target.

    Cabinet and folder entries are ordered root-first, so the final matching
    entry at a scope is the nearest and therefore most specific ancestor.
    """

    resources: tuple[ResourceRef, ...]

    def __init__(self, resources: Iterable[ResourceRef]) -> None:
        normalized = tuple(resources)
        self._validate(normalized)
        object.__setattr__(self, "resources", normalized)

    @staticmethod
    def _validate(resources: tuple[ResourceRef, ...]) -> None:
        if not resources:
            raise ValueError("resource ancestry cannot be empty")
        if any(not isinstance(resource, ResourceRef) for resource in resources):
            raise ValueError("resource ancestry must contain only ResourceRef values")
        if resources[0] != ResourceRef.global_scope():
            raise ValueError("resource ancestry must start at GLOBAL")
        if len(set(resources)) != len(resources):
            raise ValueError("resource ancestry cannot contain duplicate resources")

        ranks = tuple(resource.specificity_rank for resource in resources)
        if any(
            current > following
            for current, following in zip(ranks, ranks[1:], strict=False)
        ):
            raise ValueError(
                "resource ancestry must be ordered GLOBAL/CABINET/FOLDER/DOC"
            )

        scopes = {resource.scope for resource in resources}
        if ResourceScope.FOLDER in scopes and ResourceScope.CABINET not in scopes:
            raise ValueError("folder ancestry requires a cabinet ancestor")
        if ResourceScope.DOC in scopes and ResourceScope.FOLDER not in scopes:
            raise ValueError("document ancestry requires a folder ancestor")
        if sum(resource.scope is ResourceScope.GLOBAL for resource in resources) != 1:
            raise ValueError("resource ancestry must contain exactly one GLOBAL")
        if sum(resource.scope is ResourceScope.DOC for resource in resources) > 1:
            raise ValueError("resource ancestry can contain at most one DOC")
        if ResourceScope.DOC in scopes and resources[-1].scope is not ResourceScope.DOC:
            raise ValueError("DOC must be the exact ancestry target")

    @property
    def target(self) -> ResourceRef:
        return self.resources[-1]

    @property
    def most_specific_first(self) -> tuple[ResourceRef, ...]:
        return tuple(reversed(self.resources))

    @property
    def cabinets(self) -> tuple[ResourceRef, ...]:
        return tuple(
            resource
            for resource in self.resources
            if resource.scope is ResourceScope.CABINET
        )

    @property
    def folders(self) -> tuple[ResourceRef, ...]:
        return tuple(
            resource
            for resource in self.resources
            if resource.scope is ResourceScope.FOLDER
        )

    @property
    def document(self) -> ResourceRef | None:
        if self.target.scope is ResourceScope.DOC:
            return self.target
        return None

    def specificity_of(self, resource: ResourceRef) -> int:
        """Return the path-specific rank, where a larger value is more specific."""

        try:
            return self.resources.index(resource)
        except ValueError as exc:
            raise ValueError("resource is not present in this ancestry") from exc

    def __contains__(self, resource: object) -> bool:
        return resource in self.resources

    def __iter__(self) -> Iterator[ResourceRef]:
        return iter(self.resources)

    def __len__(self) -> int:
        return len(self.resources)
