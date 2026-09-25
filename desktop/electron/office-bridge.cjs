const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {render} = require('./office-citation.cjs');
const browserManifest = require('./browser-extension/manifest.json');

const PORT = 28886;
const MAX_BODY = 2 * 1024 * 1024;
const staticTypes = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.xml': 'application/xml; charset=utf-8', '.svg': 'image/svg+xml'};
function extensionId(bytes) {
  return [...crypto.createHash('sha256').update(bytes).digest().subarray(0, 16)]
    .map(byte => byte.toString(16).padStart(2, '0')).join('').replace(/[0-9a-f]/g, digit => 'abcdefghijklmnop'[parseInt(digit, 16)]);
}
const BROWSER_ORIGIN = `chrome-extension://${extensionId(Buffer.from(browserManifest.key, 'base64'))}`;
const extensionRoot = process.resourcesPath && fs.existsSync(path.join(process.resourcesPath, 'browser-extension', 'manifest.json'))
  ? path.join(process.resourcesPath, 'browser-extension') : path.join(__dirname, 'browser-extension');
const legacyPath = path.win32.resolve(extensionRoot).replace(/^[a-z]:/, prefix => prefix.toUpperCase());
const LEGACY_BROWSER_ORIGIN = `chrome-extension://${extensionId(Buffer.from(legacyPath, 'utf16le'))}`;
const BROWSER_ORIGINS = new Set([BROWSER_ORIGIN, LEGACY_BROWSER_ORIGIN]);

function id() { return crypto.randomUUID(); }
function pairingCode() { return String(crypto.randomInt(100000, 1000000)); }
function json(res, status, value) {
  res.writeHead(status, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'});
  res.end(JSON.stringify(value));
}
function fail(res, status, message) { json(res, status, {error: message}); }
function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0, chunks = [];
    req.on('data', chunk => {size += chunk.length; if (size > MAX_BODY) {reject(Error('请求过大')); req.destroy();} else chunks.push(chunk);});
    req.on('end', () => {try {resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'));} catch {reject(Error('请求格式不正确'));}});
    req.on('error', reject);
  });
}

function startOfficeBridge({rpc, styles, styleXml, localeXml, hostStatus, onEvent = () => {}, port = PORT}) {
  let servers = [], currentCode = pairingCode();
  let listenPort = port;
  const pairs = new Map();
  const root = path.join(__dirname, 'office-addin');
  const rotatePairing = () => {currentCode = pairingCode(); onEvent('writing.status', status()); return currentCode;};
  const activePair = token => {
    const value = pairs.get(token);
    if (!value || value.expiresAt < Date.now()) {pairs.delete(token); return null;}
    return value;
  };
  const status = () => ({running: servers.some(server => server.listening), port: listenPort, pairingCode: currentCode, pairedClients: [...pairs.values()].filter(value => value.expiresAt >= Date.now()).length,
    hosts: hostStatus(), styles: styles().map(style => ({id: style.id, name: style.name}))});
  const browserStatus = () => ({running: servers.some(server => server.listening), port: listenPort});
  const staticFile = (req, res, pathname) => {
    const name = pathname === '/office/word' ? 'word.html' : pathname.replace('/office/', '');
    if (name === 'wps.html' || name === 'wps-addon.json') return fail(res, 404, 'WPS 写作适配器暂未开放');
    if (!/^[a-z0-9._-]+$/i.test(name)) return fail(res, 404, '未找到页面');
    const target = path.join(root, name);
    if (!target.startsWith(root + path.sep) || !fs.existsSync(target)) return fail(res, 404, '未找到页面');
    res.writeHead(200, {'Content-Type': staticTypes[path.extname(target)] || 'application/octet-stream', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'});
    fs.createReadStream(target).pipe(res);
  };
  const handler = async (req, res) => {
    const url = new URL(req.url || '/', `http://${req.headers.host || 'localhost'}`);
    if (url.pathname.startsWith('/api/browser/')) {
      const origin = String(req.headers.origin || '');
      const hasOrigin = !!origin;
      if (hasOrigin) {
        if (!/^chrome-extension:\/\/[a-p]{32}$/.test(origin)) return fail(res, 403, '仅接受浏览器扩展请求');
        res.setHeader('Access-Control-Allow-Origin', origin);
        res.setHeader('Vary', 'Origin');
        res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-Research-Browser');
        res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
        if (!BROWSER_ORIGINS.has(origin)) return fail(res, 403, '扩展版本不匹配，请从安装目录重新加载文献工作台扩展');
      }
      if (req.method === 'OPTIONS') {if (!hasOrigin) return fail(res, 403, '仅接受浏览器扩展请求'); res.writeHead(204); return res.end();}
      if (req.method === 'POST' && url.pathname === '/api/browser/connect') {
        if (!hasOrigin) return fail(res, 403, '仅接受浏览器扩展请求');
        try {
          const token = crypto.randomBytes(24).toString('base64url');
          pairs.set(token, {host: 'browser', origin, createdAt: Date.now(), expiresAt: Date.now() + 30 * 24 * 60 * 60 * 1000});
          return json(res, 200, {token});
        } catch (error) {return fail(res, 400, error.message);}
      }
      const browserPair = activePair(String(req.headers['x-research-browser'] || ''));
      if (!browserPair || browserPair.host !== 'browser' || (hasOrigin && browserPair.origin !== origin)) return fail(res, 401, '浏览器扩展需要重新连接');
      try {
        if (req.method === 'GET' && url.pathname === '/api/browser/collections')
          return json(res, 200, {collections: await rpc('collections.list', {})});
        if (req.method === 'POST' && url.pathname === '/api/browser/collections') {
          const body = await readBody(req);
          return json(res, 200, await rpc('collections.edit', {action: 'create', name: body.name}));
        }
        if (req.method === 'POST' && url.pathname === '/api/browser/capture') {
          const body = await readBody(req);
          return json(res, 200, await rpc('browser.capture', body));
        }
        if (req.method === 'POST' && url.pathname === '/api/browser/duplicates') {
          const body = await readBody(req);
          return json(res, 200, await rpc('browser.duplicates', body));
        }
        if (req.method === 'POST' && url.pathname === '/api/browser/importDownloaded') {
          const body = await readBody(req);
          return json(res, 200, await rpc('browser.importDownloaded', body));
        }
        return fail(res, 404, '接口不存在');
      } catch (error) {return fail(res, 400, error.message || '保存失败');}
    }
    if (req.method === 'GET' && url.pathname.startsWith('/office/')) return staticFile(req, res, url.pathname);
    if (req.method === 'GET' && url.pathname === '/api/status') return json(res, 200, {...status(), pairingCode: undefined});
    if (req.method === 'POST' && url.pathname === '/api/pair') {
      try {
        const body = await readBody(req), host = String(body.host || '').toLowerCase();
        if (host !== 'word' || String(body.code || '') !== currentCode) return fail(res, 401, '配对码无效、已更新或当前写作宿主暂未开放');
        const token = crypto.randomBytes(24).toString('base64url');
        pairs.set(token, {host, createdAt: Date.now(), expiresAt: Date.now() + 12 * 60 * 60 * 1000});
        onEvent('writing.status', status());
        return json(res, 200, {token, expiresAt: Date.now() + 12 * 60 * 60 * 1000});
      } catch (error) {return fail(res, 400, error.message);}
    }
    const pair = activePair(String(req.headers['x-research-writer'] || ''));
    if (!pair || pair.host !== 'word') return fail(res, 401, '请先在文献工作台中获取新的配对码');
    try {
      if (req.method === 'GET' && url.pathname === '/api/items') {
        const result = await rpc('items.list', {q: String(url.searchParams.get('q') || '').slice(0, 300), limit: 80, offset: 0, sort: 'title', direction: 'asc'});
        return json(res, 200, {items: result.items.map(item => ({id: item.id, title: item.title, authorText: item.authorText, year: item.year, citationKey: item.citationKey, DOI: item.DOI || ''}))});
      }
      if (req.method === 'GET' && url.pathname === '/api/annotations') {
        const itemId = String(url.searchParams.get('itemId') || '');
        if (!/^[0-9a-f-]{36}$/i.test(itemId)) return fail(res, 400, '文献标识不正确');
        return json(res, 200, await rpc('annotations.search', {itemIds: [itemId], limit: 200, offset: 0}));
      }
      if (req.method === 'POST' && url.pathname === '/api/render') {
        const body = await readBody(req), styleId = String(body.styleId || 'gb-t-7714-2015');
        const style = styles().find(value => value.id === styleId);
        if (!style) return fail(res, 400, '引用样式不存在，请在桌面端重新选择');
        const clusters = Array.isArray(body.clusters) ? body.clusters.slice(0, 5000) : null;
        if (!clusters) return fail(res, 400, '引文组格式不正确');
        const ids = [...new Set(clusters.flatMap(cluster => (cluster.items || []).map(item => item.itemId)).filter(Boolean))];
        if (!ids.length || ids.length > 10000) return fail(res, 400, '引用文献数量不正确');
        const records = await Promise.all(ids.map(itemId => rpc('items.get', {id: itemId})));
        return json(res, 200, render({clusters, records, styleXml: styleXml(styleId), localeXml, locale: body.locale === 'en-US' ? 'en-US' : 'zh-CN'}));
      }
      if (req.method === 'POST' && url.pathname === '/api/session') {
        const body = await readBody(req);
        if (body.host !== pair.host) return fail(res, 403, '写作宿主不匹配');
        return json(res, 200, await rpc('writing.session.save', body));
      }
      if (req.method === 'POST' && url.pathname === '/api/event') {
        const body = await readBody(req);
        return json(res, 200, await rpc('writing.event', body));
      }
      return fail(res, 404, '接口不存在');
    } catch (error) {return fail(res, 400, error.message || '请求失败');}
  };
  const close = () => Promise.all(servers.map(server => new Promise(done => server.close(() => done()))));
  const listen = host => new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {handler(req, res).catch(error => fail(res, 500, error.message || '本机写作服务失败'));});
    const onError = error => {server.close(() => {}); reject(error);};
    server.once('error', onError);
    server.listen(listenPort, host, () => {server.off('error', onError); servers.push(server); listenPort = server.address().port; resolve();});
  });
  return (async () => {
    // Word resolves localhost to IPv6 first on this machine, while the desktop
    // verifier intentionally uses IPv4. Listen on both loopback addresses and
    // never bind a LAN interface.
    await listen('127.0.0.1');
    try {await listen('::1');} catch (error) {if (error.code !== 'EADDRNOTAVAIL') {await close(); throw error;}}
    return {status, rotatePairing, browserStatus, close, manifestPath: path.join(root, 'word-manifest.xml')};
  })();
}

module.exports = {startOfficeBridge, PORT, BROWSER_ORIGIN, LEGACY_BROWSER_ORIGIN};
