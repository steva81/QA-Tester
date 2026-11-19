"""Fail-closed Windows path policy shared by installation and removal."""
import ctypes
import os
from pathlib import Path
import stat

MAX_WIN32_PATH_LENGTH = 259


def _nt_fs(clean_local_path: str) -> str:
    if os.name == 'nt' and not clean_local_path.startswith('\\\\?\\'):
        return '\\\\?\\' + clean_local_path
    return clean_local_path


def strip_extended_prefix(path_value: str) -> str:
    raw = os.fspath(path_value)
    if os.name == 'nt' and raw.startswith('\\\\?\\'):
        rest = raw[4:]
        if rest.upper().startswith('UNC\\') or rest.startswith(('\\', '/')):
            raise ValueError('An absolute local path is required')
        return rest
    return raw


def known_folder(local=True):
    # Shell folder API, never LOCALAPPDATA/APPDATA supplied by the caller.
    if os.name != 'nt':
        raise RuntimeError('Windows is required')
    buf = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 28 if local else 26, None, 0, buf):
        raise RuntimeError('Cannot resolve Windows user folder')
    return buf.value


def checked_path(value):
    raw = os.fspath(value)
    if not os.path.isabs(raw) or raw.startswith(('\\\\', '//')):
        raise ValueError('An absolute local path is required')
    p = Path(os.path.abspath(raw))
    if p == p.parent:
        raise ValueError('A filesystem root is not an application directory')
    for part in p.parts[1:]:
        if ':' in part or part.endswith((' ', '.')):
            raise ValueError('Ambiguous Windows path rejected')
    for item in (*reversed(p.parents), p):
        item_fs = _nt_fs(str(item))
        if os.path.lexists(item_fs):
            st = os.lstat(item_fs)
            if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
                raise ValueError(f'Linked/reparse path rejected: {item}')
            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                raise ValueError(f'Hard-linked file rejected: {item}')
    return str(p)


def fs_path(value) -> str:
    """Return extended-length local path ONLY after full checked_path validation."""
    return _nt_fs(checked_path(value))


def validate_win32_max_path(value, label: str = 'Destination path', max_len: int = MAX_WIN32_PATH_LENGTH) -> str:
    clean = checked_path(value)
    if os.name == 'nt' and len(clean) > max_len:
        raise ValueError(
            f'{label} exceeds Windows MAX_PATH limit ({len(clean)} > {max_len} characters): '
            'choose a shorter local folder.'
        )
    return clean


def check_tree(root):
    clean_root = checked_path(root)
    root_fs = _nt_fs(clean_root)
    if os.path.isdir(root_fs):
        for base_fs, dirs, files in os.walk(root_fs, followlinks=False):
            rel = os.path.relpath(base_fs, root_fs)
            base_clean = clean_root if rel == '.' else os.path.join(clean_root, rel)
            for name in dirs + files:
                checked_path(os.path.join(base_clean, name))


def disjoint(a, b):
    a, b = os.path.normcase(checked_path(a)), os.path.normcase(checked_path(b))
    try:
        common = os.path.commonpath([a, b])
    except ValueError:
        return
    if common in (a, b):
        raise ValueError('Application, user data and shortcut directories must not overlap')


def member_path(root, name):
    name = name.replace('\\', '/')
    parts = name.split('/')
    if not name or any(p in ('', '.', '..') or ':' in p or p.endswith((' ', '.')) for p in parts):
        raise ValueError('Unsafe package member path')
    if any(p.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *(f'COM{i}' for i in range(1,10)), *(f'LPT{i}' for i in range(1,10))} for p in parts):
        raise ValueError('Windows device path rejected')
    return checked_path(os.path.join(root, *parts))
