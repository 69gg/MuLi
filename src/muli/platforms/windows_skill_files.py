"""Local NTFS Skills reads bound to native directory/file handles.

This deliberately supports one product use case, not a general Windows
filesystem abstraction. DLLs are loaded only when the Windows path is used.
"""

from __future__ import annotations

import ctypes
import os
import re
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PureWindowsPath

_U16 = ctypes.c_uint16
_U32 = ctypes.c_uint32
_I32 = ctypes.c_int32
_I64 = ctypes.c_int64
_HANDLE = ctypes.c_void_p
_POINTER = ctypes.c_void_p
_DIRECTORY = 0x10
_REPARSE = 0x400
_DEVICE = 0x40
_OFFLINE = 0x1000
_SHARE_READ = 1
_SHARE_WRITE = 2
_READ_ATTRIBUTES = 0x80
_SYNCHRONIZE = 0x100000
_OPEN_REPARSE_POINT = 0x200000
_SYNCHRONOUS_IO_NONALERT = 0x20
_BACKUP_SEMANTICS = 0x02000000
_OBJ_CASE_INSENSITIVE = 0x40
_OBJ_DONT_REPARSE = 0x1000
_ENUM_BYTES = 32768
_READ_BYTES = 65536
_MAX_PATH_UNITS = 32766
_MISSING = frozenset({0xC0000034, 0xC000003A})
_GUID_ROOT = re.compile(
    r"\\\\\?\\Volume\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}\\\Z"
)
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³"}
)


class WindowsSkillError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", _U16), ("MaximumLength", _U16), ("Buffer", _POINTER)]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", _U32),
        ("RootDirectory", _HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", _U32),
        ("SecurityDescriptor", _POINTER),
        ("SecurityQualityOfService", _POINTER),
    ]


class _StatusUnion(ctypes.Union):
    _fields_ = [("Status", _I32), ("Pointer", _POINTER)]


class _IoStatus(ctypes.Structure):
    _fields_ = [("Value", _StatusUnion), ("Information", ctypes.c_size_t)]


class _BasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", _I64),
        ("LastAccessTime", _I64),
        ("LastWriteTime", _I64),
        ("ChangeTime", _I64),
        ("FileAttributes", _U32),
    ]


class _StandardInfo(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", _I64),
        ("EndOfFile", _I64),
        ("NumberOfLinks", _U32),
        ("DeletePending", ctypes.c_ubyte),
        ("Directory", ctypes.c_ubyte),
    ]


class _TagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", _U32), ("ReparseTag", _U32)]


class _IdInfo(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_uint64), ("FileId", ctypes.c_ubyte * 16)]


class _DirectoryEntry(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", _U32),
        ("FileIndex", _U32),
        ("CreationTime", _I64),
        ("LastAccessTime", _I64),
        ("LastWriteTime", _I64),
        ("ChangeTime", _I64),
        ("EndOfFile", _I64),
        ("AllocationSize", _I64),
        ("FileAttributes", _U32),
        ("FileNameLength", _U32),
        ("EaSize", _U32),
        ("ShortNameLength", ctypes.c_byte),
        ("ShortName", _U16 * 12),
        ("FileId", _I64),
        ("FileName", _U16 * 1),
    ]


@dataclass(frozen=True, slots=True)
class Source:
    name: str
    identity: tuple[int, ...]
    directory_identity: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _Info:
    identity: tuple[int, ...]
    directory: bool

    @property
    def directory_identity(self) -> tuple[int, ...]:
        return (*self.identity[:2], _DIRECTORY if self.directory else 0)


def validate_component(name: str) -> None:
    if (
        not name
        or len(name) > 255
        or name in {".", ".."}
        or name.endswith((".", " "))
        or any(ord(character) < 32 or character in '<>:"/\\|?*' for character in name)
        or name.split(".", 1)[0].upper() in _RESERVED
    ):
        raise WindowsSkillError("resource_path_invalid")
    try:
        if len(name.encode("utf-16-le")) > 510:
            raise WindowsSkillError("resource_path_invalid")
    except UnicodeError:
        raise WindowsSkillError("resource_path_invalid") from None


def path_parts(path: str) -> tuple[str, tuple[str, ...]]:
    if len(path) > _MAX_PATH_UNITS:
        raise WindowsSkillError("secure_read_unavailable")
    value = path.replace("/", "\\")
    try:
        units = len(value.encode("utf-16-le")) // 2
    except UnicodeError:
        raise WindowsSkillError("resource_path_invalid") from None
    if (
        units > _MAX_PATH_UNITS
        or len(value) < 3
        or value[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        or value[1:3] != ":\\"
    ):
        raise WindowsSkillError("secure_read_unavailable")
    parts = tuple(value[3:].split("\\")) if len(value) > 3 else ()
    for part in parts:
        validate_component(part)
    return value[:3], parts


def normalize_root(path: Path) -> Path:
    # Path.resolve() would consume a junction before our no-follow traversal.
    path_parts(str(path))
    return Path(PureWindowsPath(path))


def _names(data: bytes) -> Iterator[str]:
    if len(data) > _ENUM_BYTES:
        raise WindowsSkillError("source_changed")
    offset = 0
    header_size = _DirectoryEntry.FileName.offset
    while True:
        if offset < 0 or offset + ctypes.sizeof(_DirectoryEntry) > len(data):
            raise WindowsSkillError("source_changed")
        header = _DirectoryEntry.from_buffer_copy(
            data[offset : offset + ctypes.sizeof(_DirectoryEntry)]
        )
        length = int(header.FileNameLength)
        following = int(header.NextEntryOffset)
        end = offset + header_size + length
        if (
            length == 0
            or length > 510
            or length % 2
            or end > len(data)
            or (following and (following % 8 or following < header_size + length))
            or (following and offset + following >= len(data))
        ):
            raise WindowsSkillError("source_changed")
        try:
            name = data[offset + header_size : end].decode("utf-16-le")
        except UnicodeError:
            raise WindowsSkillError("source_changed") from None
        yield name
        if not following:
            return
        offset += following


class _Api:
    def __init__(self) -> None:
        loader = getattr(ctypes, "WinDLL", None)
        if os.name != "nt" or loader is None:
            raise WindowsSkillError("secure_read_unavailable")
        kernel = loader("kernel32", use_last_error=True)
        native = loader("ntdll", use_last_error=True)
        error = getattr(ctypes, "get_last_error", None)
        if not callable(error):
            raise WindowsSkillError("secure_read_unavailable")
        self.error = error
        self.create = kernel.CreateFileW
        self.create.argtypes = [ctypes.c_wchar_p, _U32, _U32, _POINTER, _U32, _U32, _HANDLE]
        self.create.restype = _HANDLE
        self.close = kernel.CloseHandle
        self.close.argtypes = [_HANDLE]
        self.close.restype = _I32
        self.kind = kernel.GetFileType
        self.kind.argtypes = [_HANDLE]
        self.kind.restype = _U32
        self.information = kernel.GetFileInformationByHandleEx
        self.information.argtypes = [_HANDLE, _I32, _POINTER, _U32]
        self.information.restype = _I32
        self.volume = kernel.GetVolumeInformationByHandleW
        self.volume.argtypes = [
            _HANDLE,
            ctypes.c_wchar_p,
            _U32,
            _POINTER,
            _POINTER,
            _POINTER,
            ctypes.c_wchar_p,
            _U32,
        ]
        self.volume.restype = _I32
        self.final_name = kernel.GetFinalPathNameByHandleW
        self.final_name.argtypes = [_HANDLE, ctypes.c_wchar_p, _U32, _U32]
        self.final_name.restype = _U32
        self.read = kernel.ReadFile
        self.read.argtypes = [_HANDLE, _POINTER, _U32, ctypes.POINTER(_U32), _POINTER]
        self.read.restype = _I32
        self.open = native.NtCreateFile
        self.open.argtypes = [
            ctypes.POINTER(_HANDLE),
            _U32,
            ctypes.POINTER(_ObjectAttributes),
            ctypes.POINTER(_IoStatus),
            _POINTER,
            _U32,
            _U32,
            _U32,
            _U32,
            _POINTER,
            _U32,
        ]
        self.open.restype = _I32

    def info(self, handle: int) -> _Info:
        if int(self.kind(handle)) != 1:  # FILE_TYPE_DISK
            raise WindowsSkillError("source_not_regular")
        basic, standard, tag, identity = _BasicInfo(), _StandardInfo(), _TagInfo(), _IdInfo()
        for number, target in ((0, basic), (1, standard), (9, tag), (18, identity)):
            if not self.information(handle, number, ctypes.byref(target), ctypes.sizeof(target)):
                raise WindowsSkillError("secure_read_unavailable")
        attributes = int(basic.FileAttributes) | int(tag.FileAttributes)
        if attributes & _REPARSE or tag.ReparseTag:
            raise WindowsSkillError("symlink_forbidden")
        if attributes & (_DEVICE | _OFFLINE) or standard.DeletePending:
            raise WindowsSkillError("source_not_regular")
        directory = bool(standard.Directory)
        if bool(attributes & _DIRECTORY) != directory or standard.EndOfFile < 0:
            raise WindowsSkillError("source_changed")
        return _Info(
            (
                int(identity.VolumeSerialNumber),
                int.from_bytes(bytes(identity.FileId), "little"),
                attributes,
                int(standard.EndOfFile),
                int(basic.LastWriteTime),
                int(basic.ChangeTime),
            ),
            directory,
        )

    def anchor(self, path: str, *, listing: bool = False) -> int:
        handle = self.create(
            path,
            _READ_ATTRIBUTES | _SYNCHRONIZE | (1 if listing else 0),
            _SHARE_READ | _SHARE_WRITE,
            None,
            3,
            _BACKUP_SEMANTICS | _OPEN_REPARSE_POINT,
            None,
        )
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise WindowsSkillError("secure_read_unavailable")
        opened = int(handle)
        try:
            if not self.info(opened).directory:
                raise WindowsSkillError("secure_read_unavailable")
            filesystem = ctypes.create_unicode_buffer(32)
            if not self.volume(opened, None, 0, None, None, None, filesystem, len(filesystem)):
                raise WindowsSkillError("secure_read_unavailable")
            name = ctypes.create_unicode_buffer(64)
            length = int(self.final_name(opened, name, len(name), 1))  # VOLUME_NAME_GUID
            # Network shares have no volume GUID (GetFinalPathNameByHandleW),
            # and SMB does not support GetVolumeInformationByHandleW. Accept
            # exactly a volume root, not a GUID prefix followed by a SUBST path.
            if (
                filesystem.value != "NTFS"
                or not 0 < length < len(name)
                or not _GUID_ROOT.fullmatch(name.value)
            ):
                raise WindowsSkillError("secure_read_unavailable")
            return opened
        except BaseException:
            self.close(opened)
            raise

    def component(
        self,
        parent: int,
        name: str,
        *,
        data: bool = False,
        listing: bool = False,
    ) -> int:
        validate_component(name)
        encoded = name.encode("utf-16-le")
        buffer = (_U16 * (len(encoded) // 2 + 1)).from_buffer_copy(encoded + b"\0\0")
        text = _UnicodeString(len(encoded), len(encoded) + 2, ctypes.addressof(buffer))
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            parent,
            ctypes.pointer(text),
            _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE,
            None,
            None,
        )
        handle, status = _HANDLE(), _IoStatus()
        result = int(
            self.open(
                ctypes.byref(handle),
                _READ_ATTRIBUTES | _SYNCHRONIZE | (1 if data or listing else 0),
                ctypes.byref(attributes),
                ctypes.byref(status),
                None,
                0,
                _SHARE_READ if data else _SHARE_READ | _SHARE_WRITE,
                1,  # FILE_OPEN
                _OPEN_REPARSE_POINT | _SYNCHRONOUS_IO_NONALERT,
                None,
                0,
            )
        )
        if result != 0 or not handle.value:
            if handle.value is not None:
                self.close(handle.value)
            code = result & 0xFFFFFFFF
            if code in _MISSING:
                raise FileNotFoundError
            if code == 0xC000050B:  # STATUS_REPARSE_POINT_ENCOUNTERED
                raise WindowsSkillError("symlink_forbidden")
            raise WindowsSkillError("source_changed")
        return int(handle.value)

    def names(self, handle: int, limit: int) -> Iterator[str]:
        count = 0
        raw_count = 0
        # Two dot entries are permitted in addition to the original entry
        # budget. Every returned record and every page consumes that budget,
        # including ignored names; repeated dot-only pages cannot spin.
        for _ in range(limit + 3):
            buffer = ctypes.create_string_buffer(_ENUM_BYTES)
            if not self.information(handle, 10, buffer, len(buffer)):
                if int(self.error()) == 18:  # ERROR_NO_MORE_FILES
                    return
                raise WindowsSkillError("source_changed")
            for name in _names(buffer.raw):
                raw_count += 1
                if raw_count > limit + 2:
                    raise WindowsSkillError("scan_budget")
                if name in {".", ".."}:
                    continue
                count += 1
                if count > limit:
                    raise WindowsSkillError("scan_budget")
                yield name
        raise WindowsSkillError("scan_budget")

    def bytes(self, handle: int, limit: int) -> bytes:
        if limit < 0:
            raise WindowsSkillError("source_budget")
        pieces: list[bytes] = []
        total = 0
        while total <= limit:
            size = min(_READ_BYTES, limit + 1 - total)
            buffer = ctypes.create_string_buffer(size)
            count = _U32()
            if not self.read(handle, buffer, size, ctypes.byref(count), None):
                raise WindowsSkillError("source_changed")
            if count.value > size:
                raise WindowsSkillError("source_changed")
            total += count.value
            if total > limit:
                raise WindowsSkillError("source_budget")
            pieces.append(buffer.raw[: count.value])
            if count.value < size:
                return b"".join(pieces)
        raise WindowsSkillError("source_budget")


@lru_cache(maxsize=1)
def _api() -> _Api:
    try:
        return _Api()
    except (AttributeError, OSError):
        raise WindowsSkillError("secure_read_unavailable") from None


def available() -> bool:
    try:
        _api()
    except WindowsSkillError:
        return False
    return True


@contextmanager
def _directory(path: Path, *, listing: bool = False) -> Iterator[tuple[_Api, int, _Info]]:
    anchor, components = path_parts(str(path))
    api = _api()
    parent = api.anchor(anchor, listing=listing and not components)
    try:
        for index, name in enumerate(components):
            following = api.component(
                parent,
                name,
                listing=listing and index == len(components) - 1,
            )
            try:
                if not api.info(following).directory:
                    raise WindowsSkillError("directory_not_regular")
            except BaseException:
                api.close(following)
                raise
            api.close(parent)
            parent = following
        yield api, parent, api.info(parent)
    finally:
        api.close(parent)


def scan(
    root: Path,
    max_entries: int,
    max_skills: int,
) -> tuple[tuple[int, ...] | None, tuple[Source, ...]]:
    sources: list[Source] = []
    with ExitStack() as stack:
        try:
            api, directory, before = stack.enter_context(_directory(root, listing=True))
        except FileNotFoundError:
            return None, ()
        try:
            for name in api.names(directory, max_entries):
                child = api.component(directory, name)
                try:
                    info = api.info(child)
                    if not info.directory:
                        continue
                    try:
                        source = api.component(child, "SKILL.md")
                    except FileNotFoundError:
                        continue
                    try:
                        document = api.info(source)
                        if document.directory:
                            raise WindowsSkillError("source_not_regular")
                        sources.append(Source(name, document.identity, info.directory_identity))
                        if len(sources) > max_skills:
                            raise WindowsSkillError("skill_count_budget")
                    finally:
                        api.close(source)
                    if api.info(child).identity != info.identity:
                        raise WindowsSkillError("source_changed")
                finally:
                    api.close(child)
            if api.info(directory).identity != before.identity:
                raise WindowsSkillError("source_changed")
            return before.identity, tuple(sources)
        except FileNotFoundError:
            # A vanished enumerated child is not an empty/missing root.
            raise WindowsSkillError("source_changed") from None


def read_regular(
    path: Path,
    limit: int,
    root: Path,
    root_identity: tuple[int, ...],
    expected: tuple[int, ...] | None = None,
) -> bytes:
    path_parts(str(path))
    if not path.is_relative_to(root):
        raise WindowsSkillError("resource_path_invalid")
    relative = path.relative_to(root).parts
    if not relative:
        raise WindowsSkillError("source_not_regular")
    with _directory(root) as (api, directory, info):
        if info.directory_identity != root_identity:
            raise WindowsSkillError("source_changed")
        parent = directory
        try:
            for name in relative[:-1]:
                following = api.component(parent, name)
                try:
                    if not api.info(following).directory:
                        raise WindowsSkillError("directory_not_regular")
                except BaseException:
                    api.close(following)
                    raise
                if parent != directory:
                    api.close(parent)
                parent = following
            file = api.component(parent, relative[-1], data=True)
            try:
                before = api.info(file)
                if before.directory:
                    raise WindowsSkillError("source_not_regular")
                if expected is not None and before.identity != expected:
                    raise WindowsSkillError("source_changed")
                if before.identity[3] > limit:
                    raise WindowsSkillError("source_budget")
                data = api.bytes(file, limit)
                if len(data) != before.identity[3] or api.info(file) != before:
                    raise WindowsSkillError("source_changed")
            finally:
                api.close(file)
            reopened = api.component(parent, relative[-1])
            try:
                if api.info(reopened) != before:
                    raise WindowsSkillError("source_changed")
            finally:
                api.close(reopened)
            return data
        finally:
            if parent != directory:
                api.close(parent)
