from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from muli.platforms import filesystem_identity as fs

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux mount identity admission")


def escaped(path: Path) -> bytes:
    return (
        os.fsencode(path)
        .replace(b"\\", b"\\134")
        .replace(b" ", b"\\040")
        .replace(b"\t", b"\\011")
        .replace(b"\n", b"\\012")
    )


def mount(
    identifier: int,
    path: Path,
    filesystem: bytes = b"ext4",
    *,
    root: bytes = b"/",
    options: bytes = b"rw,relatime",
    super_options: bytes = b"rw",
) -> bytes:
    return (
        str(identifier).encode()
        + b" 1 0:999 "
        + root
        + b" "
        + escaped(path)
        + b" "
        + options
        + b" shared:2 future-tag:value - "
        + filesystem
        + b" /dev/fake "
        + super_options
        + b"\n"
    )


@dataclass(slots=True)
class Host:
    root: Path
    proc: Path
    directories: tuple[Path, ...]
    identifiers: dict[Path, int]
    opened: list[int]

    def write(self, *rows: bytes) -> None:
        (self.proc / "mountinfo").write_bytes(b"".join(rows))

    def verify(self, limits: fs.FilesystemLimits | None = None) -> None:
        fs.verify_local_filesystems(self.directories, limits=limits, proc_root=self.proc)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    root, proc = tmp_path / "state", tmp_path / "fake-proc"
    root.mkdir()
    (proc / "fdinfo").mkdir(parents=True)
    paths = tuple(root / name for name in ("config", "database", "journal", "writer"))
    for path in paths:
        path.mkdir()
    value = Host(root, proc, paths, {}, [])
    value.write(mount(10, root))
    original = fs._open_directory

    def opened(path: Path) -> int:
        descriptor = original(path)
        value.opened.append(descriptor)
        (proc / "fdinfo" / str(descriptor)).write_bytes(
            f"pos:\t0\nflags:\t0100000\nmnt_id:\t{value.identifiers.get(path, 10)}\nino:\t1\n".encode()
        )
        return descriptor

    monkeypatch.setattr(fs, "_open_directory", opened)
    return value


@pytest.mark.parametrize("filesystem", [b"ext4", b"btrfs", b"xfs", b"f2fs"])
def test_existing_local_directories_are_checked_without_changes(
    host: Host, filesystem: bytes
) -> None:
    device = host.root.stat().st_dev
    host.write(
        mount(10, host.root, filesystem).replace(
            b"0:999", f"{os.major(device) + 1}:{os.minor(device)}".encode()
        )
    )
    before = {
        path: (path.stat().st_dev, path.stat().st_ino, path.stat().st_mtime_ns)
        for path in host.directories
    }
    host.verify()
    assert before == {
        path: (path.stat().st_dev, path.stat().st_ino, path.stat().st_mtime_ns)
        for path in host.directories
    }
    assert all(not tuple(path.iterdir()) for path in host.directories)
    # A Btrfs subvolume's st_dev need not equal mountinfo's superblock device.


@pytest.mark.parametrize(
    "filesystem", [b"tmpfs", b"overlay", b"nfs", b"nfs4", b"cifs", b"fuse.sshfs", b"unknown"]
)
def test_unknown_or_volatile_filesystems_fail_closed(host: Host, filesystem: bytes) -> None:
    host.write(mount(10, host.root, filesystem))
    with pytest.raises(fs.FilesystemError, match="^filesystem_unsupported$"):
        host.verify()


@pytest.mark.parametrize("reverse", [False, True])
def test_opaque_unrelated_mount_root_does_not_reject_local_directories(
    host: Host, reverse: bool
) -> None:
    rows = (
        mount(10, host.root, b"btrfs"),
        mount(20, Path("/run/netns/fixture"), b"nsfs", root=b"net:[4026532671]"),
    )
    host.write(*(reversed(rows) if reverse else rows))
    host.verify()
    assert len(host.opened) == 2 * len(host.directories)


def test_opaque_root_does_not_grant_filesystem_support(host: Host) -> None:
    host.write(mount(10, host.root, b"nsfs", root=b"net:[4026532671]"))
    with pytest.raises(fs.FilesystemError, match="^filesystem_unsupported$"):
        host.verify()


@pytest.mark.parametrize(
    ("root", "code"),
    [
        (b"net:[1]\\041", "filesystem_invalid_observation"),
        (b"net:[1]\\", "filesystem_invalid_observation"),
        (b"net:[1]\x00", "filesystem_invalid_observation"),
        (b"net:[1] extra", "filesystem_invalid_path"),
    ],
)
def test_unrelated_opaque_roots_still_reject_malformed_fields(
    host: Host, root: bytes, code: str
) -> None:
    host.write(mount(10, host.root), mount(20, Path("/run/netns/fixture"), b"nsfs", root=root))
    with pytest.raises(fs.FilesystemError, match=f"^{code}$"):
        host.verify()
    assert not host.opened


def test_opaque_mount_root_remains_bounded(host: Host) -> None:
    host.write(
        mount(10, host.root),
        mount(20, Path("/run/netns/fixture"), b"nsfs", root=b"n" * 129),
    )
    with pytest.raises(fs.FilesystemError, match="^filesystem_invalid_path$"):
        host.verify(fs.FilesystemLimits(path_bytes=128))
    assert not host.opened


@pytest.mark.parametrize("path", [Path("net:[1]"), Path("/bad/../directory"), Path("//bad")])
def test_mountpoints_still_require_absolute_paths_without_traversal(host: Host, path: Path) -> None:
    host.write(mount(10, path, root=b"net:[4026532671]"))
    with pytest.raises(fs.FilesystemError, match="^filesystem_invalid_path$"):
        host.verify()
    assert not host.opened


@pytest.mark.parametrize("reverse", [False, True])
def test_fd_mount_id_decides_overmount_even_with_same_path_and_device(
    host: Host, reverse: bool
) -> None:
    first, second = mount(10, host.root), mount(20, host.root, b"tmpfs")
    host.write(*(reversed((first, second)) if reverse else (first, second)))
    host.identifiers.update(dict.fromkeys(host.directories, 20))
    with pytest.raises(fs.FilesystemError, match="^filesystem_unsupported$"):
        host.verify()
    host.write(mount(10, host.root, b"tmpfs"), mount(20, host.root))
    host.verify()


@pytest.mark.parametrize("field", ["options", "super_options"])
@pytest.mark.parametrize(
    "option",
    [
        b"nobarrier",
        b"barrier=0",
        b"fsync_mode=nobarrier",
        b"norecovery",
        b"noload",
        b"disable_roll_forward",
        b"nologreplay",
        b"rescue=nologreplay",
        b"checkpoint=disable",
        b"checkpoint=disable:100%",
    ],
)
def test_known_unsafe_persistence_modes_are_rejected(host: Host, field: str, option: bytes) -> None:
    arguments = {field: b"rw," + option}
    host.write(mount(10, host.root, **arguments))
    with pytest.raises(fs.FilesystemError, match="^filesystem_durability_unsupported$"):
        host.verify()


@pytest.mark.parametrize("field", ["options", "super_options"])
def test_readonly_bind_or_superblock_is_rejected(host: Host, field: str) -> None:
    host.write(mount(10, host.root, **{field: b"ro,relatime"}))
    with pytest.raises(fs.FilesystemError, match="^filesystem_readonly$"):
        host.verify()


@pytest.mark.parametrize("character", [" ", "\t", "\n", "\\", "\\040"])
def test_kernel_path_escapes_are_decoded_once(host: Host, character: str) -> None:
    path = host.root / ("special" + character + "directory")
    path.mkdir()
    host.directories = (path,)
    host.write(mount(10, path))
    host.verify()


@pytest.mark.parametrize(
    "rows",
    [
        b"",
        b"10 1 0:1 / / rw ext4 x rw\n",
        b"10 1 0:1 / / rw - ext4 x rw",
        b"10 1 0:1 / / rw - ext4 x rw extra\n",
        b"0 1 0:1 / / rw - ext4 x rw\n",
        b"10 1 broken / / rw - ext4 x rw\n",
        b"10 1 0:1 / /bad\\041 rw - ext4 x rw\n",
        b"10 1 0:1 / /bad\\ rw - ext4 x rw\n",
        b"10 1 0:1 / / rw - ext4 x rw\x00\n",
    ],
)
def test_malformed_mountinfo_never_means_local_storage(host: Host, rows: bytes) -> None:
    host.write(rows)
    with pytest.raises(fs.FilesystemError, match="^filesystem_invalid_observation$"):
        host.verify()


def test_duplicate_ids_and_missing_or_unrelated_mounts_are_rejected(host: Host) -> None:
    for rows in (
        (mount(10, host.root), mount(10, Path("/"))),
        (mount(20, host.root),),
        (mount(10, host.root / "conf"),),
    ):
        host.write(*rows)
        with pytest.raises(fs.FilesystemError):
            host.verify()


@pytest.mark.parametrize("kind", ["symlink", "parent-link", "missing", "file"])
def test_paths_must_be_existing_directories_without_links(host: Host, kind: str) -> None:
    target = host.directories[0]
    target.rmdir()
    if kind == "file":
        target.write_text("do not read")
    elif kind == "symlink":
        target.symlink_to(host.directories[1], target_is_directory=True)
    elif kind == "parent-link":
        target.mkdir()
        alias = host.root / "alias"
        alias.symlink_to(host.root, target_is_directory=True)
        host.directories = (alias / target.name,)
    with pytest.raises(fs.FilesystemError, match="^filesystem_observation_unavailable$"):
        host.verify()


@pytest.mark.parametrize("kind", ["mount-table", "directory", "mount-id"])
def test_mount_and_directory_changes_in_observation_window_are_rejected(
    host: Host, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    original = fs._open_directory
    calls = 0

    def changed(path: Path) -> int:
        nonlocal calls
        calls += 1
        if calls == len(host.directories) + 1:
            if kind == "mount-table":
                host.write(mount(10, host.root, b"xfs"))
            elif kind == "directory":
                path.rename(path.with_name("previous"))
                path.mkdir()
            else:
                host.identifiers[path] = 20
        return original(path)

    monkeypatch.setattr(fs, "_open_directory", changed)
    with pytest.raises(fs.FilesystemError, match="^filesystem_observation_changed$"):
        host.verify()


@pytest.mark.parametrize(
    "kind", ["mount-bytes", "fd-bytes", "mount-count", "directory-count", "path"]
)
def test_all_observations_have_small_budgets(
    host: Host, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    limits = fs.FilesystemLimits(
        mountinfo_bytes=1024,
        mount_entries=1,
        fdinfo_bytes=128,
        path_bytes=128 if kind == "path" else 4096,
    )
    if kind == "mount-bytes":
        host.write(b"x" * 1025)
    elif kind == "fd-bytes":
        original = fs._open_directory

        def big_fdinfo(path: Path) -> int:
            descriptor = original(path)
            (host.proc / "fdinfo" / str(descriptor)).write_bytes(b"x" * 129)
            return descriptor

        monkeypatch.setattr(fs, "_open_directory", big_fdinfo)
    elif kind == "mount-count":
        host.write(mount(10, host.root), mount(20, Path("/")))
    elif kind == "directory-count":
        host.directories *= 3
    else:
        host.directories = (Path("/" + "x" * 129),)
    with pytest.raises(fs.FilesystemError, match="budget|invalid_path"):
        host.verify(limits)


@pytest.mark.parametrize(
    "payload",
    [b"", b"mnt_id:\t0\n", b"mnt_id:\t1\nmnt_id:\t2\n", b"mnt_id:\tbad\n", b"mnt_id:\t10"],
)
def test_missing_or_malformed_fd_mount_id_fails_closed(
    host: Host, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    original = fs._read_bounded

    def fdinfo(path: Path, limit: int) -> bytes:
        return payload if path.parent.name == "fdinfo" else original(path, limit)

    monkeypatch.setattr(fs, "_read_bounded", fdinfo)
    with pytest.raises(fs.FilesystemError, match="^filesystem_invalid_observation$"):
        host.verify()


@pytest.mark.parametrize("kind", ["symlink", "fifo", "missing"])
def test_proc_sources_are_nonblocking_and_do_not_follow_final_links(host: Host, kind: str) -> None:
    path = host.proc / "mountinfo"
    path.unlink()
    if kind == "symlink":
        other = host.proc / "other"
        other.write_bytes(mount(10, host.root))
        path.symlink_to(other)
    elif kind == "fifo":
        os.mkfifo(path)
    with pytest.raises(fs.FilesystemError, match="unavailable|not_regular"):
        host.verify()


def test_failure_closes_owned_directory_fds_and_hides_exception_text(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = fs._read_bounded

    def failure(path: Path, limit: int) -> bytes:
        if path.parent.name == "fdinfo":
            raise PermissionError("private path and credential")
        return original(path, limit)

    monkeypatch.setattr(fs, "_read_bounded", failure)
    with pytest.raises(fs.FilesystemError, match="^filesystem_observation_unavailable$"):
        host.verify()
    assert len(host.opened) == 1
    with pytest.raises(OSError):
        os.fstat(host.opened[0])


@pytest.mark.parametrize("value", [0, True, 17])
def test_budget_bounds_are_validated(value: int) -> None:
    with pytest.raises(fs.FilesystemError, match="^filesystem_invalid_limits$"):
        fs.FilesystemLimits(directories=value)


def test_other_platform_has_no_unprotected_fallback(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs.sys, "platform", "darwin")
    with pytest.raises(fs.FilesystemError, match="^filesystem_platform_unsupported$"):
        host.verify()
    assert not host.opened
