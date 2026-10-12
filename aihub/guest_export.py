"""Bounded export of one guest working directory; only regular files."""
import os
import stat
import sys
import tarfile
from pathlib import Path


def main():
    root = Path(sys.argv[1]).resolve()
    blocked = set(sys.argv[2].split(','))
    total, count = 0, 0
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if name.lower() not in blocked
                       and not name.lower().startswith('.env') and not Path(directory, name).is_symlink()]
            for name in files:
                if name.lower() in blocked or name.lower().startswith('.env') or name.lower().endswith(('.key', '.pem', '.p12', '.pfx')):
                    continue
                path = Path(directory, name)
                if path.is_symlink():
                    continue
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, 'rb') as stream:
                    actual = Path(os.readlink(f'/proc/self/fd/{stream.fileno()}'))
                    info = os.fstat(stream.fileno())
                    if not actual.is_relative_to(root) or not stat.S_ISREG(info.st_mode):
                        raise ValueError('unexpected export file')
                    total += info.st_size; count += 1
                    if total > 128 * 1024 * 1024 or count > 5000:
                        raise ValueError('export too large')
                    entry = tarfile.TarInfo(path.relative_to(root).as_posix())
                    entry.size = info.st_size; entry.mode = 0o600
                    archive.addfile(entry, stream)


if __name__ == '__main__':
    main()
