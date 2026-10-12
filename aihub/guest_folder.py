"""Read-only file tools executed inside the guest for local inference."""
import json
import os
import sys
from pathlib import Path


def main():
    root = Path(sys.argv[1]).resolve()
    name, arguments = sys.argv[2], json.loads(sys.argv[3])
    def inside(value):
        path = (root / str(value or '.')).resolve()
        if not path.is_relative_to(root) or any(part.startswith('.') for part in path.relative_to(root).parts):
            raise ValueError('chemin hors des fichiers autorisés')
        return path
    def files(base):
        count = 0
        for folder, dirs, names in os.walk(base, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('.git', '.venv', 'node_modules')]
            for entry in names:
                count += 1
                if count > 5000:
                    return
                path = Path(folder, entry)
                if path.is_symlink() or entry.startswith('.'):
                    continue
                resolved = path.resolve()
                if resolved.is_relative_to(root) and resolved.is_file():
                    yield resolved
    def text(path):
        with path.open('rb') as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024 or b'\0' in data[:4096]:
            raise ValueError('fichier trop grand ou binaire')
        return data.decode('utf-8', errors='replace')
    if name == 'list_files':
        result = '\n'.join(p.relative_to(root).as_posix() for p in list(files(inside(arguments.get('path'))))[:500]) or 'Aucun fichier.'
    elif name == 'read_file':
        value = text(inside(arguments.get('path')))
        start = max(0, int(arguments.get('start') or 0))
        result = value[start:start + 12000]
        if start + 12000 < len(value):
            result += f'\n(suite : read_file avec start={start + 12000})'
    elif name == 'search_files':
        query = str(arguments.get('query') or '').strip().lower()
        if len(query) < 3 or len(query) > 500:
            raise ValueError('requête de recherche invalide')
        hits = []
        for path in files(root):
            try:
                lines = text(path).splitlines()
            except (OSError, ValueError):
                continue
            for number, line in enumerate(lines, 1):
                if query in line.lower():
                    hits.append(f'{path.relative_to(root).as_posix()}:{number}: {line[:200]}')
                    if len(hits) >= 50:
                        break
            if len(hits) >= 50:
                break
        result = '\n'.join(hits) or 'Rien trouvé.'
    else:
        raise ValueError('outil non autorisé')
    print(result)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, TypeError) as exc:
        print('refusé : ' + str(exc))
