from __future__ import annotations

import ctypes
import errno
import os
import struct
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from muli.platforms import windows_skill_files as module
from muli.platforms.windows_skill_files import WindowsSkillError


def _page(*names: str) -> bytes:
    result = bytearray(module._ENUM_BYTES)
    position = 0
    for index, name in enumerate(names):
        encoded = name.encode("utf-16-le")
        header = module._DirectoryEntry()
        size = (module._DirectoryEntry.FileName.offset + len(encoded) + 7) // 8 * 8
        header.NextEntryOffset = size if index < len(names) - 1 else 0
        header.FileNameLength = len(encoded)
        raw = bytes(header)
        result[position : position + len(raw)] = raw
        start = position + module._DirectoryEntry.FileName.offset
        result[start : start + len(encoded)] = encoded
        position += size
    return bytes(result)


def test_native_layout_uses_windows_widths_on_every_import_host() -> None:
    assert ctypes.sizeof(module._U16) == 2
    assert ctypes.sizeof(module._U32) == ctypes.sizeof(module._I32) == 4
    assert ctypes.sizeof(module._BasicInfo) == 40
    assert ctypes.sizeof(module._StandardInfo) == 24
    assert ctypes.sizeof(module._IdInfo) == 24
    assert module._DirectoryEntry.FileName.offset == 104
    pointer = ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(module._UnicodeString) == (16 if pointer == 8 else 8)
    assert ctypes.sizeof(module._ObjectAttributes) == (48 if pointer == 8 else 24)
    assert ctypes.sizeof(module._IoStatus) == pointer * 2
    assert module._ObjectAttributes.RootDirectory.offset == pointer


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "a/b",
        "a\\b",
        "a:b",
        "NUL.txt",
        "LPT¹",
        "x.",
        "x ",
        "a\x00b",
        "a\nb",
        "x" * 256,
        "🙂" * 128,
        "\ud800",
    ],
)
def test_components_reject_aliases_devices_ads_and_invalid_utf16(name: str) -> None:
    with pytest.raises(WindowsSkillError, match="resource_path_invalid"):
        module.validate_component(name)


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "C:relative",
        "\\\\server\\share",
        "\\\\?\\C:\\skills",
        "C:\\a\\..\\b",
        "C:\\a\\",
        "C:\\a:stream",
        pytest.param("C:\\" + "🙂" * 16382, id="utf16-path-over-limit"),
    ],
)
def test_root_requires_unambiguous_absolute_disk_components(path: str) -> None:
    with pytest.raises(WindowsSkillError):
        module.path_parts(path)


def test_unicode_components_and_exact_guid_volume_root() -> None:
    assert module.path_parts("D:/指导/example") == ("D:\\", ("指导", "example"))
    root = "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\"
    assert module._GUID_ROOT.fullmatch(root)
    for invalid in (root + "subdir\\", "\\\\server\\share\\", "C:\\", root.rstrip("\\")):
        assert module._GUID_ROOT.fullmatch(invalid) is None


def test_directory_page_validates_lengths_and_offsets_before_decoding() -> None:
    assert list(module._names(_page(".", "..", "指导", "example"))) == [
        ".",
        "..",
        "指导",
        "example",
    ]
    for value in (0, 1, 512, module._ENUM_BYTES):
        invalid = bytearray(_page("example"))
        struct.pack_into("<I", invalid, module._DirectoryEntry.FileNameLength.offset, value)
        with pytest.raises(WindowsSkillError, match="source_changed"):
            list(module._names(bytes(invalid)))
    for following in (8, 113, module._ENUM_BYTES, 0xFFFFFFF8):
        invalid = bytearray(_page("example"))
        struct.pack_into("<I", invalid, 0, following)
        with pytest.raises(WindowsSkillError, match="source_changed"):
            list(module._names(bytes(invalid)))
    invalid = bytearray(_page("a"))
    struct.pack_into("<H", invalid, module._DirectoryEntry.FileName.offset, 0xD800)
    with pytest.raises(WindowsSkillError, match="source_changed"):
        list(module._names(bytes(invalid)))


@pytest.mark.parametrize("names", [(".", ".."), ("ignored.txt",)])
def test_every_raw_record_and_page_is_charged_to_scan_budget(names: tuple[str, ...]) -> None:
    api = object.__new__(module._Api)
    calls = 0

    def information(handle: int, kind: int, buffer: Any, size: int) -> int:
        nonlocal calls
        assert handle == 123 and kind == 10 and size == module._ENUM_BYTES
        calls += 1
        ctypes.memmove(buffer, _page(*names), size)
        return 1

    api.information = information
    with pytest.raises(WindowsSkillError, match="scan_budget"):
        list(api.names(123, 2))
    assert calls == 3


def test_relative_native_open_has_fixed_parent_flags_and_utf16_byte_length() -> None:
    api = object.__new__(module._Api)
    closed: list[int] = []

    def native_open(
        handle: Any,
        access: int,
        attributes: Any,
        status: Any,
        allocation: Any,
        file_attributes: int,
        share: int,
        disposition: int,
        options: int,
        ea: Any,
        ea_size: int,
    ) -> int:
        value = ctypes.cast(attributes, ctypes.POINTER(module._ObjectAttributes)).contents
        text = value.ObjectName.contents
        assert value.RootDirectory == 123
        assert value.Attributes == module._OBJ_CASE_INSENSITIVE | module._OBJ_DONT_REPARSE
        assert text.Length == len("指导".encode("utf-16-le"))
        assert text.MaximumLength == text.Length + 2
        assert ctypes.string_at(text.Buffer, text.Length).decode("utf-16-le") == "指导"
        assert access == module._READ_ATTRIBUTES | module._SYNCHRONIZE | 1
        assert share == module._SHARE_READ | module._SHARE_WRITE
        assert disposition == 1 and file_attributes == 0
        assert options == module._OPEN_REPARSE_POINT | module._SYNCHRONOUS_IO_NONALERT
        assert allocation is None and ea is None and ea_size == 0
        ctypes.cast(handle, ctypes.POINTER(ctypes.c_void_p)).contents.value = 456
        return 0

    api.open = native_open
    api.close = lambda handle: closed.append(handle)
    assert api.component(123, "指导", listing=True) == 456
    assert closed == []


@pytest.mark.parametrize(
    ("filesystem", "final"),
    [
        ("NTFS", "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\subdir\\"),
        ("NTFS", "\\\\server\\share\\"),
        ("ReFS", "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\"),
    ],
)
def test_anchor_rejects_nonlocal_or_nonroot_handle_evidence(
    filesystem: str,
    final: str,
) -> None:
    api = object.__new__(module._Api)
    opened: list[str] = []
    closed: list[int] = []

    def create(path: str, *arguments: object) -> int:
        opened.append(path)
        return 123

    def volume(
        handle: int,
        name: object,
        size: int,
        serial: object,
        components: object,
        flags: object,
        kind: Any,
        length: int,
    ) -> int:
        kind.value = filesystem
        return 1

    def final_name(handle: int, buffer: Any, length: int, flags: int) -> int:
        assert flags == 1
        buffer.value = final
        return len(final)

    api.create = create
    api.close = lambda handle: closed.append(handle)
    api.info = lambda handle: module._Info((1, 2, module._DIRECTORY, 0, 0, 0), True)
    api.volume = volume
    api.final_name = final_name
    with pytest.raises(WindowsSkillError, match="secure_read_unavailable"):
        api.anchor("C:\\")
    assert opened == ["C:\\"] and closed == [123]


@pytest.mark.parametrize(
    ("status", "error"),
    [(0xC0000034, FileNotFoundError), (0xC000050B, WindowsSkillError)],
)
def test_failed_native_open_closes_any_returned_handle(
    status: int,
    error: type[Exception],
) -> None:
    api = object.__new__(module._Api)
    closed: list[int] = []

    def native_open(handle: Any, *arguments: object) -> int:
        ctypes.cast(handle, ctypes.POINTER(ctypes.c_void_p)).contents.value = 456
        return ctypes.c_int32(status).value

    api.open = native_open
    api.close = lambda handle: closed.append(handle)
    with pytest.raises(error):
        api.component(123, "example")
    assert closed == [456]


@pytest.fixture
def native() -> module._Api:
    if os.name != "nt":
        pytest.skip("Requires real Windows NTFS handles; Linux parser tests are not this proof")
    assert module.available(), "Required native API unavailable on Windows test runner"
    return module._api()


def _junction(api: module._Api, path: Path, target: Path, *, existing: bool = False) -> None:
    """Create a real mount-point reparse record without shell or symlink privilege."""
    if not existing:
        path.mkdir()
    substitute = ("\\??\\" + str(target)).encode("utf-16-le")
    printed = str(target).encode("utf-16-le")
    names = substitute + b"\0\0" + printed + b"\0\0"
    payload = (
        struct.pack(
            "<IHHHHHH",
            0xA0000003,
            len(names) + 8,
            0,
            0,
            len(substitute),
            len(substitute) + 2,
            len(printed),
        )
        + names
    )
    handle = api.create(
        str(path),
        0x40000000,
        7,
        None,
        3,
        module._BACKUP_SEMANTICS | module._OPEN_REPARSE_POINT,
        None,
    )
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        control = kernel.DeviceIoControl
        control.argtypes = [
            ctypes.c_void_p,
            module._U32,
            ctypes.c_void_p,
            module._U32,
            ctypes.c_void_p,
            module._U32,
            ctypes.POINTER(module._U32),
            ctypes.c_void_p,
        ]
        control.restype = module._I32
        buffer = ctypes.create_string_buffer(payload)
        returned = module._U32()
        if not control(
            handle, 0x900A4, buffer, len(payload), None, 0, ctypes.byref(returned), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        api.close(handle)


@pytest.fixture
def package(native: module._Api, tmp_path: Path) -> Iterator[tuple[Path, Path, module.Source]]:
    root = tmp_path / "指导" / "skills"
    path = root / "example" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_bytes("---\nname: example\ndescription: 本地指导\n---\n正文🙂".encode())
    _, sources = module.scan(root, 16, 4)
    assert len(sources) == 1
    yield root, path, sources[0]


def _read(path: Path, source: module.Source, *, expected: bool = False) -> bytes:
    return module.read_regular(
        path,
        1024,
        path.parent,
        source.directory_identity,
        source.identity if expected else None,
    )


def test_real_windows_files_update_replace_and_budget(
    native: module._Api,
    package: tuple[Path, Path, module.Source],
) -> None:
    root, path, source = package
    assert _read(path, source, expected=True) == path.read_bytes()
    path.write_bytes(b"new body")
    with pytest.raises(WindowsSkillError, match="source_changed"):
        _read(path, source, expected=True)
    _, (updated,) = module.scan(root, 16, 4)
    assert _read(path, updated, expected=True) == b"new body"
    replacement = path.with_suffix(".new")
    replacement.write_bytes(b"replacement")
    replacement.replace(path)
    with pytest.raises(WindowsSkillError, match="source_changed"):
        _read(path, updated, expected=True)
    with pytest.raises(WindowsSkillError, match="source_budget"):
        module.read_regular(path, 2, path.parent, source.directory_identity)


def test_real_windows_scan_rejects_preexisting_junction_and_counts_ignored_files(
    native: module._Api,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_bytes(b"external")
    root = tmp_path / "skills"
    root.mkdir()
    link = root / "example"
    _junction(native, link, outside)
    try:
        with pytest.raises(WindowsSkillError, match="symlink_forbidden|source_changed"):
            module.scan(root, 16, 4)
    finally:
        link.rmdir()
    for index in range(3):
        (root / f"ignored-{index}.txt").write_bytes(b"")
    with pytest.raises(WindowsSkillError, match="scan_budget"):
        module.scan(root, 2, 4)


@pytest.mark.parametrize("source_read", [False, True])
def test_real_windows_next_component_swap_never_reads_junction_target(
    native: module._Api,
    package: tuple[Path, Path, module.Source],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_read: bool,
) -> None:
    root, path, source = package
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_bytes(b"external")
    (outside / "note.txt").write_bytes(b"external")
    victim = path.parent if source_read else path.parent / "references"
    if not source_read:
        victim.mkdir()
        (victim / "note.txt").write_bytes(b"inside")
    retired = tmp_path / "retired"
    original = native.component
    entered = False
    reads: list[int] = []
    original_read = native.read

    def component(parent: int, name: str, *, data: bool = False, listing: bool = False) -> int:
        nonlocal entered
        if name == victim.name and not entered:
            entered = True
            victim.rename(retired)
            _junction(native, victim, outside)
        return original(parent, name, data=data, listing=listing)

    def read(handle: int, buffer: Any, size: int, count: Any, overlap: Any) -> int:
        reads.append(handle)
        return int(original_read(handle, buffer, size, count, overlap))

    monkeypatch.setattr(native, "component", component)
    monkeypatch.setattr(native, "read", read)
    try:
        with pytest.raises(WindowsSkillError, match="symlink_forbidden|source_changed"):
            if source_read:
                module.scan(root, 16, 4)
            else:
                module.read_regular(
                    victim / "note.txt",
                    1024,
                    path.parent,
                    source.directory_identity,
                )
        assert entered and reads == []
    finally:
        if entered:
            victim.rmdir()


def test_real_windows_inplace_parent_reparse_cannot_redirect_read(
    native: module._Api,
    package: tuple[Path, Path, module.Source],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, path, source = package
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_bytes(b"external")
    # Prove this test host can actually create the junction used by the attack.
    baseline = tmp_path / "baseline-junction"
    _junction(native, baseline, outside)
    baseline.rmdir()
    references = path.parent / "references"
    references.mkdir()
    note = references / "note.txt"
    note.write_bytes(b"inside")
    original = native.component
    attempted = False
    mutated = False
    reads: list[int] = []
    original_read = native.read

    def component(parent: int, name: str, *, data: bool = False, listing: bool = False) -> int:
        nonlocal attempted, mutated
        if name == "note.txt" and data:
            attempted = True
            note.rename(tmp_path / "saved-note.txt")
            try:
                _junction(native, references, outside, existing=True)
                mutated = True
            except OSError as exc:
                assert exc.winerror in {5, 32}
                (tmp_path / "saved-note.txt").rename(note)
        return original(parent, name, data=data, listing=listing)

    def read(handle: int, buffer: Any, size: int, count: Any, overlap: Any) -> int:
        reads.append(handle)
        return int(original_read(handle, buffer, size, count, overlap))

    monkeypatch.setattr(native, "component", component)
    monkeypatch.setattr(native, "read", read)
    try:
        try:
            content = module.read_regular(note, 1024, path.parent, source.directory_identity)
        except (WindowsSkillError, FileNotFoundError):
            assert mutated and reads == []
        else:
            assert not mutated and content == b"inside"
        assert attempted
    finally:
        if mutated:
            references.rmdir()


def test_real_windows_leaf_read_blocks_edit_and_releases_every_handle(
    native: module._Api,
    package: tuple[Path, Path, module.Source],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, path, source = package
    original = native.bytes
    content = path.read_bytes()
    attempted = False

    def read(handle: int, limit: int) -> bytes:
        nonlocal attempted
        attempted = True
        with pytest.raises(PermissionError) as write_error:
            path.write_bytes(b"external update")
        assert write_error.value.errno == errno.EACCES
        # CRT file opening can report EACCES without a native winerror.
        assert getattr(write_error.value, "winerror", None) in (None, 32)
        with pytest.raises(PermissionError) as rename_error:
            path.parent.rename(tmp_path / "renamed")
        assert rename_error.value.errno == errno.EACCES
        assert rename_error.value.winerror in (5, 32)
        actual = original(handle, limit)
        assert actual == content
        return actual

    monkeypatch.setattr(native, "bytes", read)
    assert _read(path, source, expected=True) == content and attempted
    path.write_bytes(b"normal edit after read")
    path.parent.rename(tmp_path / "renamed")
