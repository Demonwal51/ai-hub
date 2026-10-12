"""Build the one file to download: dist/ai-hub-<version>.zip.

Only what runs the app (the aihub package, its requirements, the licence, the
notes) and the two Windows scripts: installer.bat (a double-click: Python's
environment, the dependencies, a shortcut on the Desktop) and
« Lancer AI Hub.bat ». No tests, no development tools. Taken from the last
commit (git archive), never from the working tree: what is shipped is what is
committed.

    python -B tools/build_release.py
"""

import io
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEPT_FILES = {"requirements.txt", "LICENSE", "README.md", "CHANGELOG.md", "installer.bat", "Lancer AI Hub.bat",
              "installer.sh", "lancer.sh"}
KEPT_DIRS = ("aihub/",)


def version():
    text = subprocess.check_output(["git", "show", "HEAD:aihub/__init__.py"], cwd=ROOT).decode('utf-8')
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def wanted(name):
    return (name in KEPT_FILES or name.startswith(KEPT_DIRS)) and "__pycache__" not in name


def build(out_dir=None):
    out_dir = Path(out_dir or ROOT / "dist")
    out_dir.mkdir(parents=True, exist_ok=True)
    folder = f"ai-hub-{version()}"
    target = out_dir / f"{folder}.zip"
    linux_target = out_dir / f"{folder}-linux.tar.gz"
    archive = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT, capture_output=True,
                             check=True).stdout
    names = []
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar, \
            zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as out, \
            tarfile.open(linux_target, 'w:gz') as linux:
        for member in tar.getmembers():
            if not member.isfile() or not wanted(member.name):
                continue
            data = tar.extractfile(member).read()
            if member.name.endswith(".bat"):
                # cmd needs CRLF (labels and goto break with LF alone), whatever the checkout did.
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            info = zipfile.ZipInfo(f"{folder}/{member.name}")
            info.create_system = 3
            info.external_attr = (0o100000 | member.mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            out.writestr(info, data)
            entry = tarfile.TarInfo(info.filename)
            entry.size, entry.mode = len(data), member.mode
            # Reproducible metadata, no local username or group in the package.
            linux.addfile(entry, io.BytesIO(data))
            names.append(member.name)
    missing = sorted(KEPT_FILES - set(names))
    if missing:
        target.unlink()
        linux_target.unlink(missing_ok=True)
        raise SystemExit(f"absents du dernier commit : {', '.join(missing)}")
    return target, names


def main():
    target, names = build(sys.argv[1] if len(sys.argv) > 1 else None)
    size = target.stat().st_size
    print(f"{target} : {len(names)} fichiers, {size / 1024:.0f} Ko")


if __name__ == "__main__":
    main()
