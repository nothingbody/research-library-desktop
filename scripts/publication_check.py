"""Preflight the files that Git would publish; never print possible secret values."""

from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PREFIXES = (
    'library/', 'data/', 'backups/', 'app/', 'dist/', 'build/',
    'desktop/node_modules/', 'desktop/dist/', 'docs/verification/', 'docs/internal/',
)
FORBIDDEN_SUFFIXES = (
    '.sqlite3', '.db', '.pdf', '.docx', '.pptx', '.xlsx', '.exe', '.dll',
    '.token', '.pem', '.p12', '.key',
)
TEXT_SUFFIXES = {'.py', '.js', '.cjs', '.mjs', '.ts', '.tsx', '.json', '.md',
                 '.txt', '.ps1', '.html', '.css', '.xml', '.yml', '.yaml', '.toml'}
SECRET = re.compile(r'(?:sk-[A-Za-z0-9_-]{16,}|gh[opsu]_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)')
LOCAL_PATH = re.compile(r'(?i)(?:[A-Z]:[\\/]Users[\\/]|[A-Z]:[\\/]keyan(?:[\\/]|$))')


def candidates():
    paths = set()
    for args in (('git', 'ls-files', '--cached', '-z'),
                 ('git', 'ls-files', '--others', '--exclude-standard', '-z')):
        for raw in subprocess.check_output(args, cwd=ROOT).split(b'\0'):
            if raw:
                paths.add(raw.decode('utf-8').replace('\\', '/'))
    return sorted(paths)


def main():
    problems = []
    files = candidates()
    for name in files:
        path = ROOT / name
        if not path.is_file():
            continue
        lower = name.lower()
        if lower.startswith(FORBIDDEN_PREFIXES) or lower.endswith(FORBIDDEN_SUFFIXES) or Path(name).name == 'assistant-key.bin':
            problems.append((name, 'private data or binary'))
        if path.stat().st_size > 25 * 1024 * 1024:
            problems.append((name, 'over 25 MiB'))
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {'.gitignore', '.gitattributes'}:
            continue
        if name == 'scripts/publication_check.py':
            continue
        try:
            content = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            problems.append((name, 'text file is not UTF-8'))
            continue
        if SECRET.search(content):
            problems.append((name, 'possible credential'))
        if LOCAL_PATH.search(content.replace('\\\\', '\\')):
            problems.append((name, 'machine-specific path'))
    if not (ROOT / 'LICENSE').is_file():
        problems.append(('LICENSE', 'open-source license missing'))
    if problems:
        for name, reason in problems:
            print(f'{name}: {reason}')
        return 1
    print(f'Publication preflight passed: {len(files)} candidate files; no known private paths, credentials or large files.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
