"""Build identification from retained ``md5sums`` manifests.

The on-call scenario: only a few lines copied from ``dpkg``'s md5sums are
available from an isolated host, and the operator must decide which build is
installed.  For a given ``(package, version)`` pair the API therefore returns

1. the six most recent successful builds, deduplicated by the SHA-256 of the
   ``.deb`` (one row per digest, using the last successful publication that
   carried it);
2. the smallest set of file paths whose MD5 values distinguish every distinct
   retained manifest;
3. per build the MD5 of each such path, or ``"未列出"`` when the manifest does
   not contain it.

Builds whose manifest is identical are reported side by side (same group);
builds without a usable manifest are marked ``证据不足``.

The minimum distinguishing set is a minimum set-cover problem.  At most six
builds participate, so an iterative-deepening search over the candidate paths
is trivially fast and returns an exact optimum (lexicographically smallest on
ties).
"""

from __future__ import annotations

from dataclasses import dataclass

#: Number of distinct builds (``.deb`` digests) a query may return.
BUILD_LIMIT = 6

#: Value reported for a path that the build's manifest does not list.
NOT_LISTED = "未列出"
#: Group marker for builds without a usable manifest.
INSUFFICIENT_EVIDENCE = "证据不足"


@dataclass(frozen=True)
class BuildRecord:
    """One deduplicated build row in a query response."""

    publication_id: int
    package: str
    version: str
    architecture: str
    digest: str
    #: Ordered ``(path, md5)`` pairs retained at publication time, or ``None``
    #: when no usable manifest was available.
    md5sums: tuple[tuple[str, str], ...] | None
    created_at: str


def minimum_distinguishing_paths(
    manifests: list[tuple[tuple[str, str], ...]],
) -> list[str]:
    """Return the smallest path set separating every manifest in *manifests*.

    Two manifests are distinguished by a path when their MD5 values for that
    path differ (a missing path is treated as :data:`NOT_LISTED`).  Empty or
    duplicated manifests are ignored: they cannot be told apart from anything
    by definition.  Among equally small sets the lexicographically smallest
    one is returned for stable output.
    """

    usable = [dict(manifest) for manifest in manifests if manifest]
    if len(usable) < 2:
        return []

    def signature(path: str) -> tuple[str, ...]:
        return tuple(mapping.get(path, NOT_LISTED) for mapping in usable)

    # A path can only help if at least two builds disagree on it.
    candidates = sorted(
        {
            path
            for manifest in usable
            for path in manifest
            if len(set(signature(path))) > 1
        }
    )
    if not candidates:
        return []

    # Pair signatures: every pair of builds must be split by some chosen path.
    pair_count = len(usable) * (len(usable) - 1) // 2
    pair_coverage: dict[str, int] = {}
    for path in candidates:
        values = signature(path)
        mask = 0
        bit = 0
        for left in range(len(usable)):
            for right in range(left + 1, len(usable)):
                if values[left] != values[right]:
                    mask |= 1 << bit
                bit += 1
        pair_coverage[path] = mask
    full_mask = (1 << pair_count) - 1

    def search(chosen: list[str], start: int, covered: int, depth_left: int) -> list[str] | None:
        if covered == full_mask:
            return chosen
        if depth_left == 0:
            return None
        remaining = candidates[start:]
        # Even taking every remaining path could not close the gap: prune.
        if covered | _union_masks(pair_coverage[p] for p in remaining) != full_mask:
            return None
        for offset, path in enumerate(remaining):
            new_covered = covered | pair_coverage[path]
            if new_covered == covered:
                continue
            result = search(
                chosen + [path],
                start + offset + 1,
                new_covered,
                depth_left - 1,
            )
            if result is not None:
                return result
        return None

    for depth in range(1, len(candidates) + 1):
        result = search([], 0, 0, depth)
        if result is not None:
            return result
    return candidates  # pragma: no cover - full coverage always exists


def _union_masks(masks) -> int:  # type: ignore[no-untyped-def]
    total = 0
    for mask in masks:
        total |= mask
    return total


def _manifest_identity(md5sums: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """Canonical identity of a manifest for equality grouping.

    Retained entries are already ordered, but normalise defensively so two
    manifests with the same content in different order still group together.
    """

    return tuple(sorted(md5sums))


def build_identification(records: list[BuildRecord]) -> dict[str, object]:
    """Compute the full response body for a deduplicated build query.

    *records* must already be deduplicated by digest and ordered newest first.
    """

    usable_manifests = [record.md5sums for record in records if record.md5sums]
    paths = minimum_distinguishing_paths(list(usable_manifests))

    # Group builds whose retained manifests are identical.
    identity_to_group: dict[tuple[tuple[str, str], ...], int] = {}
    builds: list[dict[str, object]] = []
    for record in records:
        if not record.md5sums:
            group = None
            note = INSUFFICIENT_EVIDENCE
        else:
            identity = _manifest_identity(record.md5sums)
            if identity not in identity_to_group:
                identity_to_group[identity] = len(identity_to_group) + 1
            group = identity_to_group[identity]
            note = None
        mapping = dict(record.md5sums) if record.md5sums else {}
        builds.append(
            {
                "publication_id": record.publication_id,
                "release_number": record.publication_id,
                "package": record.package,
                "version": record.version,
                "architecture": record.architecture,
                "sha256": record.digest,
                "created_at": record.created_at,
                "manifest_group": group,
                "note": note,
                "md5": {path: mapping.get(path, NOT_LISTED) for path in paths},
            }
        )

    groups: list[dict[str, object]] = []
    group_members: dict[int, list[int]] = {}
    for build in builds:
        if build["manifest_group"] is not None:
            group_members.setdefault(build["manifest_group"], []).append(
                build["publication_id"]
            )
    for group_id in sorted(group_members):
        groups.append(
            {
                "group": group_id,
                "publication_ids": group_members[group_id],
                "note": "清单相同，并列" if len(group_members[group_id]) > 1 else None,
            }
        )

    return {
        "query_limit": BUILD_LIMIT,
        "distinguishing_paths": paths,
        "not_listed_marker": NOT_LISTED,
        "insufficient_evidence_marker": INSUFFICIENT_EVIDENCE,
        "groups": groups,
        "builds": builds,
    }
