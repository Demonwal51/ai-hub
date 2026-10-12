"""Explicit empty file/folder creation, separate from an AI's permissions."""
import os
import re
from contextlib import ExitStack
from pathlib import Path

from .store import StoreError


def target(path, roots=(), base=None):
    if not isinstance(path, str) or not path.strip() or len(path) > 1000 or re.search(r'[\x00-\x1f]', path):
        raise StoreError('indiquez un chemin valide')
    path = path.strip()
    p = Path(path)
    if base is not None:
        if p.is_absolute() or p.drive:
            raise StoreError('indiquez un nom relatif au dossier de cette conversation')
        p = Path(base) / p
    if not p.is_absolute() or '..' in p.parts:
        raise StoreError('indiquez un chemin complet, sans remontée vers un dossier parent')
    if os.name == 'nt' and not re.match(r'^[A-Za-z]:[\\/]', str(p)):
        raise StoreError('choisissez un disque local du serveur')
    for name in p.parts[1:]:
        if (name.endswith((' ', '.')) or re.search(r'[:*?"<>|\\]', name)
                or re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name, re.I)):
            raise StoreError('ce nom de fichier ou dossier n’est pas pris en charge')
    if len(p.parts) > 64:
        raise StoreError('chemin trop profond')
    p = Path(os.path.abspath(p))
    allowed = [Path(r).resolve() for r in roots]
    if allowed and not any(p.is_relative_to(r) for r in allowed):
        raise StoreError('création hors des dossiers autorisés')
    if base is not None and not p.is_relative_to(Path(base).resolve()):
        raise StoreError('création hors du dossier de cette conversation')
    return p


def _windows_directory(path):
    """Pin each parent without DELETE sharing; reject every reparse point."""
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    open_dir = api.CreateFileW
    open_dir.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    open_dir.restype = wintypes.HANDLE
    close = api.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = open_dir(str(path), 0x80, 0x3, None, 3, 0x02000000 | 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    class Attributes(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('tag', wintypes.DWORD)]
    info = Attributes()
    query = api.GetFileInformationByHandleEx
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    query.restype = wintypes.BOOL
    try:
        if not query(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & 0x400 or not info.attributes & 0x10:
            raise StoreError('un lien ou un fichier occupe le chemin : choisissez le dossier réel')
    except BaseException:
        close(handle)
        raise
    return lambda: close(handle)


def create(path, kind, *, roots=(), base=None, parents=True):
    if kind not in ('file', 'directory'):
        raise StoreError('choisissez un fichier ou un dossier')
    p = target(path, roots, base)
    created = False
    try:
        with ExitStack() as stack:
            walk = Path(p.anchor)
            if os.name == 'nt':
                stack.callback(_windows_directory(walk))
                fd = None
            else:
                fd = os.open(walk, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stack.callback(os.close, fd)
            for index, name in enumerate(p.parts[1:]):
                leaf = index == len(p.parts) - 2
                walk = walk / name
                if leaf and kind == 'file':
                    if os.name == 'nt':
                        file_fd = os.open(walk, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    else:
                        file_fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                          0o600, dir_fd=fd)
                    os.close(file_fd)
                    created = True
                    break
                if leaf or parents:
                    try:
                        if os.name == 'nt':
                            os.mkdir(walk, 0o700)
                        else:
                            os.mkdir(name, 0o700, dir_fd=fd)
                        if leaf:
                            created = True
                    except FileExistsError:
                        pass
                if os.name == 'nt':
                    stack.callback(_windows_directory(walk))
                else:
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    stack.callback(os.close, fd)
            if kind == 'file' and not created:
                raise StoreError('un fichier doit avoir un nom')
    except FileExistsError:
        raise StoreError('ce chemin existe déjà : aucun fichier n’a été écrasé') from None
    except OSError:
        raise StoreError('création impossible : vérifiez les droits, les dossiers parents et l’absence de liens') from None
    return {'path': str(p), 'kind': kind, 'created': created}
