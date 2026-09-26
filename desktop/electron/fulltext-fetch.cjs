'use strict';
// Full-text downloads through Chromium's network stack instead of Python's
// urllib. Publisher CDNs (Cloudflare, Akamai, ...) fingerprint TLS/HTTP2 and
// answer HTTP 403 to non-browser clients even for open-access PDFs; Chromium's
// own stack passes those checks. Pages that still demand a JavaScript
// challenge are opened once in a hidden, sandboxed window.
//
// Every request of the dedicated session -- redirects and the hidden window's
// subresources included -- goes through the same address policy as
// client_backend/netsafe.py: http(s) only, no credentials, no localhost, and
// every resolved address must be public.
const {app, BrowserWindow, net, session} = require('electron');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {once} = require('node:events');

const MAX_REDIRECTS = 6;
const ACCEPT_LANGUAGE = 'zh-CN,zh;q=0.9,en;q=0.8';
const HOST_CACHE_MS = 60 * 1000;
const DOWNLOAD_START_GRACE_MS = 500;

function fail(code, message) {
  return Object.assign(new Error(message), {code});
}

// ---------------------------------------------------------------- addresses
function parseIPv4(text) {
  const parts = String(text).split('.');
  if (parts.length !== 4 || !parts.every(part => /^\d{1,3}$/.test(part))) return null;
  const bytes = parts.map(Number);
  return bytes.every(value => value <= 255) ? bytes : null;
}

function parseIPv6(text) {
  let value = String(text).replace(/^\[|\]$/g, '').split('%')[0].toLowerCase();
  const dotted = value.match(/^(.*:)(\d+\.\d+\.\d+\.\d+)$/);
  if (dotted) {
    const v4 = parseIPv4(dotted[2]);
    if (!v4) return null;
    value = dotted[1] + ((v4[0] << 8) | v4[1]).toString(16) + ':' + ((v4[2] << 8) | v4[3]).toString(16);
  }
  const halves = value.split('::');
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(':') : [];
  const tail = halves.length === 2 && halves[1] ? halves[1].split(':') : [];
  const fill = halves.length === 2 ? 8 - head.length - tail.length : 0;
  if (fill < 0 || (halves.length === 1 && head.length !== 8)) return null;
  const words = [...head, ...Array(fill).fill('0'), ...tail].map(word => (/^[0-9a-f]{1,4}$/.test(word) ? parseInt(word, 16) : NaN));
  return words.length === 8 && words.every(Number.isInteger) ? words : null;
}

function ipv4Public([a, b, c]) {
  return !(a === 0 || a === 10 || a === 127 || a >= 224 ||
    (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) || (a === 192 && b === 0 && (c === 0 || c === 2)) ||
    (a === 192 && b === 88 && c === 99) ||
    (a === 198 && (b === 18 || b === 19)) || (a === 198 && b === 51 && c === 100) || (a === 203 && b === 0 && c === 113));
}

function embeddedIPv4(high, low) {
  return [high >> 8, high & 255, low >> 8, low & 255];
}

function ipv6Public(words) {
  if (words.slice(0, 5).every(word => word === 0) && words[5] === 0xffff) return ipv4Public(embeddedIPv4(words[6], words[7]));
  if (words[0] === 0x64 && words[1] === 0xff9b && words.slice(2, 6).every(word => word === 0)) return ipv4Public(embeddedIPv4(words[6], words[7]));
  if (words[0] === 0x2002) return ipv4Public(embeddedIPv4(words[1], words[2]));      // 6to4
  if ((words[0] & 0xe000) !== 0x2000) return false;                                   // global unicast is 2000::/3
  if (words[0] === 0x2001 && (words[1] === 0x0db8 || words[1] < 0x0200)) return false; // documentation, 2001::/23
  return true;
}

function isPublicAddress(address) {
  const v4 = parseIPv4(address);
  if (v4) return ipv4Public(v4);
  const v6 = parseIPv6(address);
  return v6 ? ipv6Public(v6) : false;
}

// Clash/Surge "fake-ip" mode answers every name with 198.18.0.0/15.
function isFakeAddress(address) {
  const v4 = parseIPv4(address);
  return Boolean(v4 && v4[0] === 198 && (v4[1] === 18 || v4[1] === 19));
}

function checkUrl(value, isPublic) {
  const text = String(value || '');
  let url;
  try {url = new URL(text);} catch {throw fail('INVALID_ARGUMENT', '请输入有效的 http(s) 文献地址');}
  if (text.length > 8000 || !['http:', 'https:'].includes(url.protocol) || url.username || url.password || !url.hostname) {
    throw fail('INVALID_ARGUMENT', '请输入有效的 http(s) 文献地址');
  }
  const host = url.hostname.replace(/\.$/, '').toLowerCase();
  if (host === 'localhost' || host === 'localhost.localdomain' || host.endsWith('.localhost')) throw fail('FULLTEXT_BLOCKED', '不允许访问本机地址');
  // WHATWG URL parsing already turned legacy forms such as 127.1 or 2130706433 into dotted quads.
  const literal = host.replace(/^\[|\]$/g, '');
  if ((parseIPv4(literal) || parseIPv6(literal)) && !isPublic(literal)) throw fail('FULLTEXT_BLOCKED', '不允许访问本机或内网地址');
  return url;
}

// ------------------------------------------------------------------ fetcher
function createFulltextFetcher({partition = 'fulltext', isPublic = isPublicAddress, log = () => {}, browserTimeoutMs = 45000} = {}) {
  const ses = session.fromPartition(partition);   // in-memory: nothing is kept after the app quits
  const dohSession = session.fromPartition(partition + '-doh');
  const platform = (ses.getUserAgent().match(/\(([^)]+)\)/) || [])[1] || 'Windows NT 10.0; Win64; x64';
  const userAgent = `Mozilla/5.0 (${platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${process.versions.chrome.split('.')[0]}.0.0.0 Safari/537.36 ResearchLibrary/${app.getVersion()}`;
  ses.setUserAgent(userAgent, ACCEPT_LANGUAGE);
  ses.setPermissionRequestHandler((_, __, callback) => callback(false));
  ses.setPermissionCheckHandler(() => false);
  const hosts = new Map();
  const blockedUrls = new Set();

  async function publicDns(hostname) {
    for (const service of ['https://dns.google/resolve', 'https://cloudflare-dns.com/dns-query']) {
      try {
        const addresses = [];
        for (const type of ['A', 'AAAA']) {
          const response = await dohSession.fetch(`${service}?name=${encodeURIComponent(hostname)}&type=${type}`,
            {headers: {Accept: 'application/dns-json'}, signal: AbortSignal.timeout(8000)});
          const data = await response.json();
          if (data.Status !== 0) throw fail('FULLTEXT_NETWORK', '公共 DNS 未找到全文站点');
          for (const answer of data.Answer || []) if ([1, 28].includes(answer.type)) addresses.push(String(answer.data || ''));
        }
        if (addresses.length) return addresses;
      } catch (error) {
        log('doh', error);
      }
    }
    throw fail('FULLTEXT_NETWORK', '无法验证代理 DNS 返回的地址，请检查网络或使用浏览器下载');
  }

  async function lookup(hostname) {
    const literal = hostname.replace(/^\[|\]$/g, '');
    if (parseIPv4(literal) || parseIPv6(literal)) return isPublic(literal);
    let addresses;
    try {
      addresses = (await ses.resolveHost(hostname)).endpoints.map(endpoint => endpoint.address);
    } catch {
      throw fail('FULLTEXT_NETWORK', `无法解析全文站点 ${hostname}`);
    }
    if (addresses.length && addresses.every(isFakeAddress)) addresses = await publicDns(hostname);
    const allowed = addresses.length > 0 && addresses.every(isPublic);
    if (!allowed) log('dns', `${hostname}: ${addresses.join(', ')}`);
    return allowed;
  }

  function hostAllowed(hostname) {
    const key = hostname.toLowerCase();
    const cached = hosts.get(key);
    if (cached && cached.until > Date.now()) return cached.promise;
    const promise = lookup(key);
    hosts.set(key, {promise, until: Date.now() + HOST_CACHE_MS});
    promise.catch(() => hosts.delete(key));
    return promise;
  }

  async function assertAllowed(value) {
    const url = checkUrl(value, isPublic);
    if (!(await hostAllowed(url.hostname))) throw fail('FULLTEXT_BLOCKED', '全文地址指向本机或内网，已阻止访问');
    return url;
  }

  // Defence in depth: nothing this session loads may reach a private address.
  ses.webRequest.onBeforeRequest((details, callback) => {
    let url;
    try {url = new URL(details.url);} catch {callback({cancel: true}); return;}
    if (['data:', 'blob:', 'about:'].includes(url.protocol)) {callback({}); return;}
    if (url.protocol === 'ws:' || url.protocol === 'wss:') url.protocol = url.protocol === 'ws:' ? 'http:' : 'https:';
    assertAllowed(url.href).then(() => {blockedUrls.delete(details.url); callback({});}, error => {
      if (blockedUrls.size > 1024) blockedUrls.clear();
      blockedUrls.add(details.url);
      log('blocked', `${details.resourceType} ${details.url}: ${error.message}`);
      callback({cancel: true});
    });
  });

  // Chromium opens PDFs in its built-in viewer; the hidden window needs the
  // file itself, so top-level PDF responses are turned into downloads.
  ses.webRequest.onHeadersReceived((details, callback) => {
    const headers = details.responseHeaders;
    const typeKey = headers && Object.keys(headers).find(key => key.toLowerCase() === 'content-type');
    if (details.resourceType !== 'mainFrame' || !typeKey || !/application\/(x-)?pdf/i.test(String(headers[typeKey]))) {callback({}); return;}
    const changed = {};
    for (const [key, value] of Object.entries(headers)) if (key.toLowerCase() !== 'content-disposition') changed[key] = value;
    changed['Content-Disposition'] = ['attachment'];
    callback({responseHeaders: changed});
  });

  function checkSavePath(value) {
    const target = path.resolve(String(value || ''));
    let base, real;
    try {base = fs.realpathSync(os.tmpdir()); real = fs.realpathSync(path.dirname(target));} catch {throw fail('INVALID_ARGUMENT', '全文临时文件位置不正确');}
    if (path.dirname(real) !== base || !/^rl-fetch-[\w-]+$/.test(path.basename(real)) || path.basename(target) !== 'body.part') {
      throw fail('INVALID_ARGUMENT', '全文临时文件位置不正确');
    }
    return path.join(real, 'body.part');
  }

  function throttled(onProgress) {
    let last = 0;
    return (bytes, total, force = false) => {
      const now = Date.now();
      if (onProgress && (force || now - last > 400)) {last = now; onProgress(bytes, total);}
    };
  }

  // One request, redirects validated hop by hop.
  function browserReferer(url, referer) {
    if (!referer) return '';
    const target = new URL(url), source = new URL(referer);
    if (source.protocol === 'https:' && target.protocol === 'http:') return '';
    return target.origin === source.origin ? source.href : source.origin + '/';
  }

  function request(url, {accept, referer, signal}) {
    return new Promise((resolve, reject) => {
      const req = net.request({url, session: ses, redirect: 'manual', useSessionCookies: true, cache: 'no-store'});
      req.setHeader('Accept', accept);
      req.setHeader('Accept-Language', ACCEPT_LANGUAGE);
      const referrer = browserReferer(url, referer);
      if (referrer) req.setHeader('Referer', referrer);
      const abort = () => {req.abort(); reject(fail('FULLTEXT_NETWORK', '连接全文网站超时，请稍后重试'));};
      signal.addEventListener('abort', abort, {once: true});
      req.on('redirect', (status, method, redirectUrl) => {
        signal.removeEventListener('abort', abort);
        req.abort();
        resolve({redirect: new URL(redirectUrl, url).href});
      });
      req.on('response', response => resolve({response}));   // abort stays wired: a timeout also stops the body
      req.on('error', error => {signal.removeEventListener('abort', abort); reject(error);});
      req.end();
    });
  }

  async function direct(url, options) {
    let current = (await assertAllowed(url)).href;
    for (let hop = 0; ; hop++) {
      let outcome;
      try {outcome = await request(current, options);} catch (error) {error.fetchUrl = current; throw error;}
      if (!outcome.redirect) return {response: outcome.response, finalUrl: current};
      if (hop >= MAX_REDIRECTS) throw fail('FULLTEXT_HTTP', '全文链接重定向次数过多');
      const redirected = new URL(outcome.redirect);
      log('redirect', `${new URL(current).hostname} -> ${redirected.hostname}${redirected.pathname}`);
      current = (await assertAllowed(outcome.redirect)).href;
    }
  }

  function header(response, name) {
    const value = response.headers[name];
    return Array.isArray(value) ? value.join(', ') : String(value || '');
  }

  async function save(response, target, maxBytes, progress, signal) {
    // Chromium may transparently decode gzip/br while preserving the wire
    // Content-Length. That length cannot be compared with decoded chunks.
    const encoding = header(response, 'content-encoding').trim().toLowerCase();
    const total = encoding && encoding !== 'identity' ? 0 : Number(header(response, 'content-length')) || 0;
    if (total > maxBytes) throw fail('FULLTEXT_TOO_LARGE', `全文超过 ${Math.round(maxBytes / 1024 ** 2)}MB 限制，请通过浏览器保存后导入`);
    const output = fs.createWriteStream(target, {flags: 'wx'});
    let bytes = 0;
    try {
      for await (const chunk of response) {
        if (signal.aborted) throw fail('FULLTEXT_NETWORK', '连接全文网站超时，请稍后重试');
        bytes += chunk.length;
        if (bytes > maxBytes) throw fail('FULLTEXT_TOO_LARGE', `全文超过 ${Math.round(maxBytes / 1024 ** 2)}MB 限制，请通过浏览器保存后导入`);
        if (!output.write(chunk)) await once(output, 'drain');
        progress(bytes, total);
      }
    } finally {
      output.end();
      await once(output, 'close').catch(() => {});
    }
    if (signal.aborted) throw fail('FULLTEXT_NETWORK', '连接全文网站超时，请稍后重试');
    if (total && bytes !== total) throw fail('FULLTEXT_NETWORK', `全文下载未完成：只收到 ${bytes}/${total} 字节`);
    progress(bytes, total, true);
    return bytes;
  }

  const CHALLENGE_PROBE = `(() => Boolean(document.querySelector('#challenge-form,#challenge-stage,#challenge-running,#cf-challenge-running,script[src*="/cdn-cgi/challenge-platform/"]'))
    || Boolean(document.querySelector('iframe[src*="/akamai/interstitial"],meta[http-equiv="refresh"][content*="bm-verify"]'))
    || Boolean(document.querySelector('script[src*="token.awswaf.com"],#challenge-container'))
    || /^(just a moment|请稍候|attention required)/i.test(document.title))()`;

  function challengeHtml(response, target, bytes) {
    if (!/text\/html/i.test(header(response, 'content-type')) || bytes > 128 * 1024) return false;
    const html = fs.readFileSync(target, 'utf8');
    return /\/akamai\/interstitial\.html|\/\_sec\/verify\?provider=interstitial|bm-verify=|\/cdn-cgi\/challenge-platform\/|token\.awswaf\.com|window\.awsWafCookieDomainList|id=["'](?:challenge-form|challenge-stage|cf-challenge-running)["']/i.test(html);
  }

  // A real (hidden) Chromium page for sites that answer with a JavaScript
  // challenge. PDFs arrive as downloads (plugins are off, so no PDF viewer);
  // an HTML page is returned once no challenge is left on it.
  function viaWindow(url, {referer, maxBytes, target, progress}) {
    const win = new BrowserWindow({show: false, width: 1280, height: 900, webPreferences: {
      session: ses, sandbox: true, contextIsolation: true, nodeIntegration: false, plugins: false,
      spellcheck: false, backgroundThrottling: false, webgl: false}});
    const contents = win.webContents;
    contents.setWindowOpenHandler(() => ({action: 'deny'}));
    contents.setAudioMuted(true);
    contents.setWebRTCIPHandlingPolicy('disable_non_proxied_udp');
    return new Promise((resolve, reject) => {
      let settled = false, downloading = false, status = 0, download = null;
      let navigationError = null, navigationTimer;
      const finish = (error, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        clearTimeout(navigationTimer);
        ses.removeListener('will-download', onDownload);
        if (error && download && download.getState() !== 'completed') download.cancel();
        if (!win.isDestroyed()) win.destroy();
        error ? reject(error) : resolve(value);
      };
      const timer = setTimeout(() => finish(fail('FULLTEXT_CHALLENGE', '网站的人机验证未能自动通过，请在浏览器中打开后用扩展保存，或手动导入 PDF')), browserTimeoutMs);
      const navigationFailed = (message, failedUrl = url) => {
        if (settled || downloading) return;
        if (blockedUrls.has(failedUrl) || blockedUrls.has(url) || blockedUrls.has(contents.getURL())) {
          finish(fail('FULLTEXT_BLOCKED', '全文地址指向本机或内网，已阻止访问'));
          return;
        }
        navigationError = message;
        clearTimeout(navigationTimer);
        // Chromium can reject PDF navigations with ERR_FAILED or ERR_ABORTED
        // before will-download arrives. Keep the window and listener alive for
        // that event, but report a real navigation failure promptly.
        navigationTimer = setTimeout(() => {
          if (settled || downloading) return;
          if (contents.isLoading()) {navigationError = null; return;}
          log('browser-error', navigationError);
          finish(fail('FULLTEXT_NETWORK', '无法连接全文网站，请检查网络或代理设置'));
        }, DOWNLOAD_START_GRACE_MS);
      };
      const onDownload = (event, item, source) => {
        if (source !== contents) return;
        if (settled || downloading) {event.preventDefault(); return;}
        downloading = true;
        download = item;
        navigationError = null;
        clearTimeout(navigationTimer);
        item.setSavePath(target);
        item.on('updated', () => {
          if (settled) return;
          if (item.getReceivedBytes() > maxBytes) {
            finish(fail('FULLTEXT_TOO_LARGE', `全文超过 ${Math.round(maxBytes / 1024 ** 2)}MB 限制，请通过浏览器保存后导入`));
          } else progress(item.getReceivedBytes(), item.getTotalBytes());
        });
        item.once('done', (_, state) => {
          if (settled) return;
          if (state !== 'completed') {finish(fail('FULLTEXT_NETWORK', '浏览器内核下载未完成，请稍后重试')); return;}
          if (item.getReceivedBytes() > maxBytes) {
            finish(fail('FULLTEXT_TOO_LARGE', `全文超过 ${Math.round(maxBytes / 1024 ** 2)}MB 限制，请通过浏览器保存后导入`));
            return;
          }
          progress(item.getReceivedBytes(), item.getTotalBytes(), true);
          finish(null, {finalUrl: item.getURL(), status: 200, contentType: item.getMimeType() || 'application/octet-stream', bytes: item.getReceivedBytes(), via: 'browser'});
        });
      };
      ses.on('will-download', onDownload);
      contents.on('did-fail-load', (_, code, description, failedUrl, isMainFrame) => {
        if (isMainFrame) navigationFailed(`${description} (${code})`, failedUrl);
      });
      contents.on('did-navigate', (_, __, code) => {status = code || status;});
      contents.on('did-stop-loading', async () => {
        const current = contents.getURL();
        if (settled || downloading || navigationError || !current || current === 'about:blank') return;
        try {
          const challenge = await contents.executeJavaScript(CHALLENGE_PROBE, true);
          if (settled || downloading || navigationError || challenge) return;   // its script will reload the page
          if (status >= 400) {finish(null, {finalUrl: current, status, contentType: '', bytes: 0, via: 'browser'}); return;}
          const html = await contents.executeJavaScript('document.documentElement.outerHTML', true);
          if (settled || downloading || navigationError) return;
          const body = Buffer.from(String(html), 'utf8');
          if (body.length > maxBytes) throw fail('FULLTEXT_TOO_LARGE', '网页内容过大');
          fs.writeFileSync(target, body, {flag: 'wx'});
          finish(null, {finalUrl: current, status: status || 200, contentType: 'text/html; charset=utf-8', bytes: body.length, via: 'browser'});
        } catch (error) {
          finish(error.code ? error : fail('FULLTEXT_NETWORK', '读取网页失败：' + error.message));
        }
      });
      contents.loadURL(url, {httpReferrer: browserReferer(url, referer) || undefined}).catch(error => {
        navigationFailed(String(error.message));
      });
    });
  }

  // Fetch url into savePath. Non-2xx statuses are returned, not thrown, so the
  // Python side maps them exactly like urllib's HTTPError.
  async function fetchToFile({url, accept = 'application/pdf,text/html', referer = '', maxBytes = 512 * 1024 ** 2,
    savePath, timeoutMs = 180000, allowBrowser = true, onProgress} = {}) {
    const target = checkSavePath(savePath);
    const limit = Math.min(Math.max(1, Number(maxBytes) || 0), 512 * 1024 ** 2);
    const progress = throttled(onProgress);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Math.min(Math.max(5000, Number(timeoutMs) || 0), 600000));
    try {
      const safeReferer = referer ? (() => {try {return checkUrl(referer, isPublic).href;} catch {return '';}})() : '';
      let outcome;
      try {
        outcome = await direct(url, {accept: accept.includes('json') ? accept : accept + ',*/*;q=0.8', referer: safeReferer, signal: controller.signal});
      } catch (error) {
        const failedUrl = error.fetchUrl || url;
        if (allowBrowser && /ERR_BLOCKED_BY_CLIENT/.test(String(error.message)) && !blockedUrls.has(failedUrl)) {
          await assertAllowed(failedUrl);
          log('challenge', `Chromium cancelled ${new URL(failedUrl).hostname} -> hidden window`);
          return await viaWindow(failedUrl, {referer: safeReferer, maxBytes: limit, target, progress});
        }
        throw error;
      }
      const {response, finalUrl} = outcome;
      if (response.statusCode >= 200 && response.statusCode < 300) {
        const bytes = await save(response, target, limit, progress, controller.signal);
        if (allowBrowser && challengeHtml(response, target, bytes)) {
          fs.rmSync(target, {force: true});
          log('challenge', `HTTP ${response.statusCode} interstitial ${finalUrl} -> hidden window`);
          return await viaWindow(finalUrl, {referer: safeReferer, maxBytes: limit, target, progress});
        }
        return {finalUrl, status: response.statusCode, contentType: header(response, 'content-type'), bytes, via: 'direct'};
      }
      response.resume();
      if (allowBrowser && [403, 503].includes(response.statusCode)) {
        log('challenge', `${response.statusCode} ${finalUrl} -> hidden window`);
        return await viaWindow(finalUrl, {referer: safeReferer, maxBytes: limit, target, progress});
      }
      return {finalUrl, status: response.statusCode, contentType: header(response, 'content-type'), bytes: 0, via: 'direct'};
    } catch (error) {
      fs.rmSync(target, {force: true});
      log('fetch-error', `${error.code || ''}: ${error.message || error}`);
      if (error.code && /^(FULLTEXT_|INVALID_)/.test(error.code)) throw error;
      if (blockedUrls.has(error.fetchUrl || url)) throw fail('FULLTEXT_BLOCKED', '全文地址指向本机或内网，已阻止访问');
      throw fail('FULLTEXT_NETWORK', '无法连接全文网站，请检查网络或代理设置');
    } finally {
      clearTimeout(timer);
    }
  }

  return {fetchToFile, session: ses, userAgent};
}

// Requests the Python backend may make of the main process ("host" calls).
function createHostHandler(getFetcher, write) {
  const methods = {'fulltext.fetch': (params, onProgress) => getFetcher().fetchToFile({...params, onProgress})};
  return async ({id, method, params} = {}) => {
    const reply = data => write({hostReply: {id, ...data}});
    if (!Object.hasOwn(methods, method)) {reply({error: {code: 'HOST_METHOD', message: '不支持的主进程调用'}}); return;}
    try {
      reply({result: await methods[method](params || {}, (bytes, total) => write({hostProgress: {id, bytes, total}}))});
    } catch (error) {
      reply({error: {code: error.code || 'FULLTEXT_NETWORK', message: String(error.message || error).slice(0, 500)}});
    }
  };
}

module.exports = {createFulltextFetcher, createHostHandler, isPublicAddress, checkUrl};
