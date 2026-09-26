"""Validated outbound requests for full-text retrieval.

The target is resolved and checked immediately before connecting. Redirects
are checked before a request is sent to the next location.
"""
from __future__ import annotations

import email.message
import http.client
import ipaddress
import json
import os
import shutil
import socket
import tempfile
from urllib import error, parse, request

from .common import AppError, require

_FAKE_IP = ipaddress.ip_network('198.18.0.0/15')


def _public_address(value):
    address = ipaddress.ip_address(str(value).split('%', 1)[0])
    return (address.ipv4_mapped or address).is_global if isinstance(address, ipaddress.IPv6Address) else address.is_global


def _fake_address(value):
    try:
        return ipaddress.ip_address(str(value)) in _FAKE_IP
    except ValueError:
        return False


def _public_dns(host, port):
    """Resolve through HTTPS when a local proxy supplies synthetic DNS IPs.

    We connect to the returned public IP, never to the unverified synthetic IP.
    """
    host = host.encode('idna').decode('ascii')
    last_error = None
    for service in ('https://dns.google/resolve', 'https://cloudflare-dns.com/dns-query'):
        try:
            addresses = []
            for kind in ('A', 'AAAA'):
                url = service + '?' + parse.urlencode({'name': host, 'type': kind})
                req = request.Request(url, headers={'Accept': 'application/dns-json'})
                with request.urlopen(req, timeout=8) as response:
                    data = json.loads(response.read(200000))
                require(data.get('Status') == 0, '公共 DNS 未找到全文站点')
                for answer in data.get('Answer') or []:
                    if answer.get('type') in (1, 28):
                        address = str(answer.get('data') or '')
                        require(_public_address(address), '全文站点解析到内网地址，已阻止访问')
                        addresses.append(address)
            require(addresses, '公共 DNS 未返回全文站点地址')
            result = []
            for address in dict.fromkeys(addresses):
                family = socket.AF_INET6 if ':' in address else socket.AF_INET
                result.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '',
                               (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)))
            return result
        except AppError as exc:
            if '内网地址' in str(exc):
                raise
            last_error = exc
        except (OSError, ValueError, KeyError) as exc:
            last_error = exc
    raise AppError('FULLTEXT_NETWORK', '无法验证代理 DNS 返回的地址，请检查网络或使用浏览器下载', retryable=True) from last_error


def public_url(value):
    url = str(value or '').strip()
    parts = parse.urlsplit(url)
    require(len(url) <= 8000 and parts.scheme in ('https', 'http') and parts.hostname and
            not parts.username and not parts.password, '请输入有效的 http(s) 文献地址')
    try:
        parts.port
    except ValueError as exc:
        raise AppError('INVALID_ARGUMENT', '文献地址中的端口号不正确') from exc
    host = parts.hostname.rstrip('.').lower()
    require(host and host not in ('localhost', 'localhost.localdomain') and not host.endswith('.localhost'),
            '不允许访问本机地址')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # inet_aton recognizes legacy IPv4 notations such as 127.1 and
        # 2130706433, which urlsplit/ip_address otherwise treat as DNS names.
        try:
            address = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            address = None
    if address is not None:
        require(_public_address(address), '不允许访问本机或内网地址')
    return url


def _checked_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *args, **kwargs):
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if not infos:
        raise AppError('FULLTEXT_NETWORK', '无法解析全文站点')
    if all(_fake_address(info[4][0]) for info in infos):
        infos = _public_dns(host, port)
    if not all(_public_address(info[4][0]) for info in infos):
        raise AppError('FULLTEXT_BLOCKED', '全文地址指向本机或内网，已阻止访问')
    last_error = None
    for family, kind, proto, _, sockaddr in infos:
        sock = socket.socket(family, kind, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error or OSError('无法连接全文站点')


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _checked_connection


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _checked_connection


class _HTTPHandler(request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


class _RedirectHandler(request.HTTPRedirectHandler):
    max_redirections = 6

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = request.build_opener(request.ProxyHandler({}), _HTTPHandler(), _HTTPSHandler(), _RedirectHandler())


# Many publisher CDNs answer HTTP 403 to clients that do not look like a
# browser, open-access PDFs included. Use a browser-compatible User-Agent that
# still names the app (as Zotero does), plus the headers a browser sends.
_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
               'Chrome/152.0.0.0 Safari/537.36 ResearchLibrary/0.9')


_host_bridge = None


def set_host_bridge(bridge):
    """Route browser-type fetches through the Electron main process (Chromium's network stack)."""
    global _host_bridge
    _host_bridge = bridge


def _http_error(status, url):
    host = parse.urlsplit(url).hostname or ''
    if status in (401, 403):
        return AppError('FULLTEXT_FORBIDDEN', f'{host} 拒绝程序直接下载（HTTP {status}），常见于出版社的防爬限制，与是否开放获取无关。'
                        '可在浏览器中打开后用扩展保存，或手动导入 PDF', details={'host': host})
    if status == 404:
        return AppError('FULLTEXT_NOT_FOUND', '全文链接不存在（HTTP 404）')
    if status == 429:
        return AppError('FULLTEXT_RATE_LIMITED', '网站限制访问频率（HTTP 429），请稍后重试', retryable=True)
    return AppError('FULLTEXT_HTTP', f'网站返回 HTTP {status}', retryable=status >= 500)


class _HostResponse:
    """A body the main process already downloaded, read like an urllib response."""

    def __init__(self, folder, final_url, content_type, size):
        self._folder, self._path, self._url = folder, os.path.join(folder, 'body.part'), final_url
        self._file = open(self._path, 'rb')
        self.headers = email.message.Message()
        self.headers['Content-Type'] = content_type or 'application/octet-stream'
        self.headers['Content-Length'] = str(size)

    def geturl(self):
        return self._url

    def read(self, amount=-1):
        return self._file.read(amount)

    def keep(self, target):
        """Move the complete body to target; the caller then owns that file."""
        self._file.close()
        shutil.move(self._path, target)
        return str(target)

    def close(self):
        self._file.close()
        shutil.rmtree(self._folder, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _open_via_host(url, accept, timeout, referer, max_bytes, progress):
    folder = tempfile.mkdtemp(prefix='rl-fetch-')
    try:
        result = _host_bridge.call('fulltext.fetch', {
            'url': url, 'accept': accept, 'referer': referer or '', 'maxBytes': int(max_bytes),
            'savePath': os.path.join(folder, 'body.part'), 'timeoutMs': int(timeout * 1000)},
            timeout=timeout + 60, progress=progress)
        final_url = public_url(result.get('finalUrl') or url)
        status = int(result.get('status') or 0)
        if not 200 <= status < 300:
            raise _http_error(status, final_url)
        return _HostResponse(folder, final_url, result.get('contentType'), int(result.get('bytes') or 0))
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def open_url(url, accept='application/pdf,text/html', timeout=30, referer='', browser=False,
             max_bytes=512 * 1024 * 1024, progress=None):
    """browser=True: fetch like a browser would (full-text pages and PDFs). Inside the
    desktop app this runs on Chromium's network stack in the main process, which
    passes the CDN checks urllib fails; the response is then file-backed."""
    url = public_url(url)
    if browser and _host_bridge is not None:
        return _open_via_host(url, accept, max(timeout, 180), referer, max_bytes, progress)
    headers = {'User-Agent': _USER_AGENT, 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
               'Accept': accept if 'json' in accept else accept + ',*/*;q=0.8'}
    if referer:
        try:
            headers['Referer'] = public_url(referer)
        except AppError:
            pass
    req = request.Request(url, headers=headers)
    try:
        return _OPENER.open(req, timeout=timeout)
    except error.HTTPError as exc:
        status, final = exc.code, exc.geturl() or url
        exc.close()
        raise _http_error(status, final) from exc
    except error.URLError as exc:
        if isinstance(exc.reason, AppError):
            raise exc.reason from exc
        raise AppError('FULLTEXT_NETWORK', '无法连接全文网站，请检查网络或代理设置', retryable=True) from exc
    except (TimeoutError, OSError) as exc:
        raise AppError('FULLTEXT_NETWORK', '连接全文网站失败，请稍后重试', retryable=True) from exc
