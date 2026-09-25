from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path


class AppError(Exception):
    def __init__(self, code, message, details=None, retryable=False):
        super().__init__(message)
        self.code, self.details, self.retryable = code, details, retryable


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def uid():
    return str(uuid.uuid4())


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def norm(value):
    return unicodedata.normalize('NFKC', str(value or '')).casefold().strip()


def doi(value):
    value = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', str(value or '').strip(), flags=re.I)
    return value.casefold().rstrip(' .')


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uid() + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        stream.write(dumps(data))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def require(value, message='参数不正确'):
    if not value:
        raise AppError('INVALID_ARGUMENT', message)


def within(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    require(path.is_relative_to(root) and path != root, '路径超出管理目录')
    return path


def bounded_int(value, default, minimum, maximum):
    try:
        result = int(value if value is not None else default)
    except (ValueError, TypeError):
        raise AppError('INVALID_ARGUMENT', '数值参数不正确')
    require(minimum <= result <= maximum, f'数值范围应为 {minimum}–{maximum}')
    return result
