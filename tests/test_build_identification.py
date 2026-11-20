"""Tests for build identification via retained md5sums manifests."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from debbuild import build_deb  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import Settings  # noqa: E402
from app.evidence import (  # noqa: E402
    INSUFFICIENT_EVIDENCE,
    NOT_LISTED,
    BuildRecord,
    build_identification,
    minimum_distinguishing_paths,
)
from app.main import create_app  # noqa: E402
from app.storage import Repository, StagedUpload  # noqa: E402

PKG = "smoke-one"
VERSION = "1.0.0"


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def manifest_line(path: str, text: str) -> bytes:
    return f"{md5(text)}  {path}\n".encode()  # dpkg format: md5, two spaces, path


def make_settings(tmp_path: Path, retention: int = 2) -> Settings:
    return Settings(data_dir=tmp_path / "data", retention=retention)


@pytest.fixture()
def client(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client, settings


def publish(test_client: TestClient, path: Path):
    response = test_client.post(
        "/api/publish",
        files=[("files", (path.name, path.read_bytes(), "application/vnd.debian.binary-package"))],
    )
    assert response.status_code == 201, response.text
    return response.json()["published"]["id"]


def build(
    directory: Path,
    tag: str,
    manifest: bytes | None,
    *,
    package: str = PKG,
    version: str = VERSION,
    payload: bytes | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{package}_{version}_{tag}.deb"
    build_deb(
        path,
        control={"Package": package, "Version": version},
        payload=payload if payload is not None else f"payload-{tag}\n".encode(),
        md5sums=manifest,
    )
    return path


def identify(test_client: TestClient, package: str = PKG, version: str = VERSION):
    response = test_client.get(
        "/api/builds/identify", params={"package": package, "version": version}
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------- unit


def test_minimum_distinguishing_paths_picks_smallest_set():
    m_a = (("/etc/a", "1"), ("/etc/b", "1"), ("/etc/c", "1"))
    m_b = (("/etc/a", "1"), ("/etc/b", "2"), ("/etc/c", "1"))
    m_c = (("/etc/a", "1"), ("/etc/b", "1"), ("/etc/c", "2"))
    paths = minimum_distinguishing_paths([m_a, m_b, m_c])
    # /etc/b and /etc/c are each needed; /etc/a never separates anyone.
    assert paths == ["/etc/b", "/etc/c"]


def test_minimum_distinguishing_paths_prefers_lexicographic_tie():
    m_a = (("/etc/a", "1"), ("/etc/z", "1"))
    m_b = (("/etc/a", "2"), ("/etc/z", "2"))
    # Either single path distinguishes; lexicographically smallest wins.
    assert minimum_distinguishing_paths([m_a, m_b]) == ["/etc/a"]


def test_identical_manifests_need_no_paths():
    manifest = (("/etc/a", "1"), ("/etc/b", "2"))
    assert minimum_distinguishing_paths([manifest, manifest]) == []


def test_not_listed_counts_as_distinguishing_value():
    m_a = (("/etc/a", "1"), ("/etc/b", "1"))
    m_b = (("/etc/a", "1"),)  # /etc/b missing -> 未列出
    paths = minimum_distinguishing_paths([m_a, m_b])
    assert paths == ["/etc/b"]


def test_build_identification_groups_and_markers():
    common = (("/etc/a", "1"),)
    other = (("/etc/a", "2"),)
    records = [
        BuildRecord(3, "p", "1", "amd64", "d3", common, "t3"),
        BuildRecord(2, "p", "1", "amd64", "d2", common, "t2"),
        BuildRecord(1, "p", "1", "amd64", "d1", other, "t1"),
        BuildRecord(0, "p", "1", "amd64", "d0", None, "t0"),
    ]
    result = build_identification(records)
    assert result["distinguishing_paths"] == ["/etc/a"]
    by_id = {build["publication_id"]: build for build in result["builds"]}
    assert by_id[3]["manifest_group"] == by_id[2]["manifest_group"]
    assert by_id[1]["manifest_group"] != by_id[3]["manifest_group"]
    assert by_id[3]["md5"] == {"/etc/a": "1"}
    assert by_id[1]["md5"] == {"/etc/a": "2"}
    assert by_id[0]["manifest_group"] is None
    assert by_id[0]["note"] == INSUFFICIENT_EVIDENCE
    assert by_id[0]["md5"] == {"/etc/a": NOT_LISTED}
    group_notes = {
        tuple(group["publication_ids"]): group["note"] for group in result["groups"]
    }
    assert group_notes[(3, 2)] == "清单相同，并列"


# --------------------------------------------------------------------- API


def test_identify_returns_md5s_and_not_listed(client):
    test_client, _ = client
    settings: Settings = test_client.app.state.settings
    src = settings.data_dir.parent / "src"

    m1 = manifest_line("/etc/app.conf", "v1") + manifest_line("/usr/bin/app", "bin1")
    m2 = manifest_line("/etc/app.conf", "v2") + manifest_line("/usr/bin/app", "bin1")
    id1 = publish(test_client, build(src, "b1", m1))
    id2 = publish(test_client, build(src, "b2", m2))

    result = identify(test_client)
    assert result["distinguishing_paths"] == ["/etc/app.conf"]
    by_pub = {entry["publication_id"]: entry for entry in result["builds"]}
    assert set(by_pub) == {id1, id2}
    newest_first = [entry["publication_id"] for entry in result["builds"]]
    assert newest_first == sorted(newest_first, reverse=True)
    assert by_pub[id2]["md5"]["/etc/app.conf"] == md5("v2")
    assert by_pub[id1]["md5"]["/etc/app.conf"] == md5("v1")
    for entry in result["builds"]:
        assert entry["sha256"]
        assert entry["package"] == PKG and entry["version"] == VERSION


def test_identical_manifests_are_grouped_side_by_side(client):
    test_client, _ = client
    settings: Settings = test_client.app.state.settings
    src = settings.data_dir.parent / "src"

    manifest = manifest_line("/etc/app.conf", "same")
    # Different .deb bytes (different payloads) but the same md5sums content.
    id1 = publish(test_client, build(src, "b1", manifest, payload=b"payload-one\n"))
    id2 = publish(test_client, build(src, "b2", manifest, payload=b"payload-two\n"))

    result = identify(test_client)
    assert result["distinguishing_paths"] == []
    assert len(result["builds"]) == 2
    groups = result["groups"]
    assert len(groups) == 1
    assert sorted(groups[0]["publication_ids"]) == sorted([id1, id2])
    assert groups[0]["note"] == "清单相同，并列"
    assert all(entry["manifest_group"] == groups[0]["group"] for entry in result["builds"])
    assert all(entry["md5"] == {} for entry in result["builds"])


def test_missing_manifest_publishes_and_is_marked_insufficient(client):
    test_client, _ = client
    settings: Settings = test_client.app.state.settings
    src = settings.data_dir.parent / "src"

    # md5sums member absent entirely.
    id_missing = publish(test_client, build(src, "no-manifest", None))
    # Empty member: exists but no usable entries.
    id_empty = publish(test_client, build(src, "empty-manifest", b""))
    # Malformed lines: retained as unusable too.
    id_bad = publish(test_client, build(src, "bad-manifest", b"this is not md5sums\n"))

    result = identify(test_client)
    assert len(result["builds"]) == 3
    by_pub = {entry["publication_id"]: entry for entry in result["builds"]}
    for pub_id in (id_missing, id_empty, id_bad):
        entry = by_pub[pub_id]
        assert entry["note"] == INSUFFICIENT_EVIDENCE
        assert entry["manifest_group"] is None
        assert entry["md5"] == {}
    assert result["distinguishing_paths"] == []


def test_same_digest_keeps_only_last_successful_publication(client):
    test_client, _ = client
    settings: Settings = test_client.app.state.settings
    src = settings.data_dir.parent / "src"

    manifest = manifest_line("/etc/app.conf", "v1")
    deb_path = build(src, "repeated", manifest)
    first_id = publish(test_client, deb_path)
    # Re-publish the exact same file (same SHA-256).
    second_id = publish(test_client, deb_path)
    assert second_id > first_id

    result = identify(test_client)
    assert len(result["builds"]) == 1
    entry = result["builds"][0]
    assert entry["publication_id"] == second_id
    assert entry["release_number"] == second_id


def test_failed_publication_is_excluded_from_query(client):
    test_client, _ = client
    settings: Settings = test_client.app.state.settings
    src = settings.data_dir.parent / "src"

    good_id = publish(test_client, build(src, "good", manifest_line("/etc/a", "1")))
    bad = src / "broken.deb"
    bad.write_bytes(b"not a deb")
    response = test_client.post(
        "/api/publish",
        files=[("files", (bad.name, bad.read_bytes(), "application/octet-stream"))],
    )
    assert response.status_code == 400

    result = identify(test_client)
    assert [entry["publication_id"] for entry in result["builds"]] == [good_id]


def test_six_most_recent_distinct_digests_only(client):
    test_client, _ = client
    settings: Settings = test_client.app.state.settings
    src = settings.data_dir.parent / "src"

    # Seven distinct builds; oldest must drop out of the six-build window.
    ids = []
    for index in range(7):
        pub_id = publish(
            test_client,
            build(src, f"b{index}", manifest_line("/etc/a", f"v{index}")),
        )
        ids.append(pub_id)

    result = identify(test_client)
    assert result["query_limit"] == 6
    assert len(result["builds"]) == 6
    returned = {entry["publication_id"] for entry in result["builds"]}
    assert ids[0] not in returned
    assert set(ids[1:]) == returned


def test_recycled_package_still_identifiable_from_retained_manifest(client):
    test_client, _ = client
    settings: Settings = test_client.app.state.settings
    src = settings.data_dir.parent / "src"

    manifest = manifest_line("/etc/app.conf", "v1")
    first_path = build(src, "b1", manifest)
    first_id = publish(test_client, first_path)
    first_digest = hashlib.sha256(first_path.read_bytes()).hexdigest()

    # A newer build of the same package gives the manifest path something to
    # distinguish; extra publications of other packages trigger GC
    # (retention=2) so the original pool file is recycled.
    publish(test_client, build(src, "b2", manifest_line("/etc/app.conf", "v2")))
    for index in range(3):
        publish(
            test_client,
            build(
                src,
                f"other{index}",
                manifest_line("/etc/app.conf", f"o{index}"),
                package=f"other-pkg-{index}",
            ),
        )

    # The original pool file has been garbage collected...
    repository: Repository = test_client.app.state.repository
    pool_files = list((repository.export / "pool").rglob("*.deb"))
    assert all(first_digest not in pool.name for pool in pool_files)

    # ...but the retained manifest still produces full evidence.
    result = identify(test_client)
    entry = next(build_row for build_row in result["builds"] if build_row["sha256"] == first_digest)
    assert entry["publication_id"] == first_id
    assert "/etc/app.conf" in result["distinguishing_paths"]
    assert entry["md5"]["/etc/app.conf"] == md5("v1")


def test_recovered_publication_keeps_its_manifest(tmp_path):
    settings = make_settings(tmp_path)
    repo = Repository(settings)
    src = tmp_path / "src"
    deb_path = build(src, "b1", manifest_line("/etc/a", "v1"))
    staged = StagedUpload(path=deb_path, original_name=deb_path.name)

    prepared = repo._prepare([staged])
    with repo._connect() as db:
        cursor = db.execute(
            "INSERT INTO publications (created_at, state, release_sha256, "
            "index_sha256, plain_index_sha256, index_size, package_count, "
            "packages_json) VALUES ('now', 'staged', ?, ?, ?, 0, 0, '[]')",
            (
                hashlib.sha256(prepared.release).hexdigest(),
                prepared.index_sha256,
                prepared.plain_index_sha256,
            ),
        )
        staged_id = cursor.lastrowid
        # Mirror what Repository._commit writes in the same journal transaction.
        info, entry = prepared.pairs[0]
        db.execute(
            "INSERT INTO package_builds (package, version, architecture, sha256, "
            "publication_id, created_at, md5sums_json) VALUES (?, ?, ?, ?, ?, 'now', ?)",
            (
                info.package,
                info.version,
                info.architecture,
                entry.sha256,
                staged_id,
                json.dumps(info.md5sums) if info.md5sums else None,
            ),
        )
    # Simulate the commit point having happened (Release rename) then a crash.
    repo.release_abspath.write_bytes(prepared.release)

    recovered = Repository(settings)
    with recovered._connect() as db:
        row = db.execute(
            "SELECT state FROM publications WHERE id=?", (staged_id,)
        ).fetchone()
    assert row["state"] == "committed"

    result = recovered.identify_builds(PKG, VERSION)
    assert len(result["builds"]) == 1
    # A single retained manifest needs no distinguishing path, but the stored
    # entries survive the restart and are surfaced via the raw manifest.
    with recovered._connect() as db:
        row = db.execute(
            "SELECT md5sums_json FROM package_builds WHERE publication_id=?",
            (staged_id,),
        ).fetchone()
    assert json.loads(row["md5sums_json"]) == [["/etc/a", md5("v1")]]
    assert result["builds"][0]["note"] is None
    assert result["builds"][0]["manifest_group"] == 1


def test_unknown_package_returns_empty_builds(client):
    test_client, _ = client
    result = identify(test_client, package="ghost", version="9.9.9")
    assert result["builds"] == []
    assert result["distinguishing_paths"] == []
    assert result["groups"] == []


def test_version_is_matched_verbatim(client):
    test_client, _ = client
    settings: Settings = test_client.app.state.settings
    src = settings.data_dir.parent / "src"
    publish(test_client, build(src, "b1", manifest_line("/etc/a", "1"), version="1.0.0"))
    assert identify(test_client, version="1.0.0")["builds"]
    assert identify(test_client, version="1.0")["builds"] == []


def test_endpoint_is_documented_in_openapi_and_swagger_served(client):
    test_client, _ = client
    schema = test_client.get("/openapi.json").json()
    assert "/api/builds/identify" in schema["paths"]
    params = {p["name"] for p in schema["paths"]["/api/builds/identify"]["get"]["parameters"]}
    assert params == {"package", "version"}
    swagger = test_client.get("/swagger-ui")
    assert swagger.status_code == 200
    assert b"swagger" in swagger.content.lower()


def test_minimum_set_cover_is_optimal_on_large_manifests():
    # Six builds over 60 paths. 57 paths are constant noise; the three varying
    # paths each only carry a binary split, so all three are required to tell
    # the six builds apart (2**3 = 8 signatures >= 6).
    paths = [f"/opt/app/f{i}.conf" for i in range(60)]
    manifests = []
    for build_index in range(6):
        entries = []
        for path_index, path in enumerate(paths):
            if path_index < 3:
                value = str((build_index >> path_index) & 1)
            else:
                value = "constant"
            entries.append((path, value))
        manifests.append(tuple(entries))
    chosen = minimum_distinguishing_paths(manifests)
    assert chosen == paths[:3]

    # When a single path already splits every build, the answer must be that
    # one path regardless of how many other paths vary in lockstep.
    wide = []
    for build_index in range(6):
        entries = [(path, str(build_index)) for path in paths[:10]]
        wide.append(tuple(entries))
    assert minimum_distinguishing_paths(wide) == [paths[0]]


def test_relative_and_absolute_path_spellings_group_together(client):
    test_client, _ = client
    settings: Settings = test_client.app.state.settings
    src = settings.data_dir.parent / "src"

    digest_value = md5("v1")
    absolute = f"{digest_value}  /etc/app.conf\n".encode()
    dotted = f"{digest_value}  ./etc/app.conf\n".encode()
    id1 = publish(test_client, build(src, "abs", absolute, payload=b"one\n"))
    id2 = publish(test_client, build(src, "dot", dotted, payload=b"two\n"))

    result = identify(test_client)
    # Same content despite different spelling: the builds cannot be told apart
    # from their manifests and must share a group with no distinguishing paths.
    assert result["distinguishing_paths"] == []
    assert len(result["groups"]) == 1
    assert sorted(result["groups"][0]["publication_ids"]) == sorted([id1, id2])
    assert all(build_row["sha256"] for build_row in result["builds"])
