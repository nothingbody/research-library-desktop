from __future__ import annotations

import os
from pathlib import Path
import urllib.parse
import urllib.request

from .common import require, uid
from .fulltext import public_url


class Downloads:
    def __init__(self, library, attachments, jobs):
        self.library, self.attachments, self.jobs = library, attachments, jobs

    def pdf(self, payload, progress):
        require(self.library.get_settings().get('online', True), '联网已关闭')
        self.library.get(payload['itemId'])
        # public_url rejects credentials, non-http(s) schemes and local/intranet
        # targets; the redirect target below is validated the same way.
        url = public_url(payload['url'])
        temporary = self.library.root / 'downloads'
        temporary.mkdir(exist_ok=True)
        target = temporary / (uid() + '.pdf')
        limit = 512 * 1024 * 1024
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'ResearchLibrary/0.1', 'Accept': 'application/pdf'})
            with urllib.request.urlopen(request, timeout=30) as response, target.open('wb') as output:
                public_url(response.geturl())
                length = int(response.headers.get('Content-Length') or 0)
                require(length <= limit, '在线下载超过512MB，请通过浏览器保存后导入')
                total = 0
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    if total == 0:
                        require(b'%PDF-' in chunk[:1024], '该地址未返回PDF；若需要登录，请在浏览器获取后导入')
                    total += len(chunk)
                    require(total <= limit, '下载超过512MB限制')
                    output.write(chunk)
                    progress(min(.95, total / length) if length else .1, f'已下载 {total / 1024 ** 2:.1f} MB')
                output.flush()
                os.fsync(output.fileno())
            require(total > 0, '下载内容为空')
            result = self.attachments.add(payload['itemId'], target)
            name = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
            if not name.lower().endswith('.pdf'):
                name = '在线全文.pdf'
            with self.library.db(True) as db:
                db.execute('UPDATE attachments SET name=? WHERE id=?', (name[:240], result['id']))
            self.jobs.create('pdf.index', {'attachmentId': result['id']})
            return {'attachmentId': result['id'], 'bytes': total, 'source': url}
        finally:
            target.unlink(missing_ok=True)
