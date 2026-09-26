const apiRoot = 'http://127.0.0.1:28886/api/browser';
const pendingKey = id => `browser-download:${id}`;
const importing = new Set();

async function report(kind, message) {
  await chrome.storage.local.set({downloadStatus: {kind, message, at: Date.now()}});
  await chrome.action.setBadgeBackgroundColor({color: kind === 'success' ? '#2f6f4e' : '#b3261e'}).catch(() => {});
  await chrome.action.setBadgeText({text: kind === 'success' ? '✓' : '!'}).catch(() => {});
  await chrome.runtime.sendMessage({type: 'download-status', kind, message}).catch(() => {});
}

function fileStem(value) {
  return String(value || '文献全文').replace(/[<>:"/\\|?*\x00-\x1f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 90) || '文献全文';
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === 'retry-downloads') {
    retryCompletedDownloads(message.token).then(() => sendResponse({ok: true}))
      .catch(error => sendResponse({ok: false, error: error.message || '无法重试全文导入'}));
    return true;
  }
  if (message?.type !== 'download-browser') return;
  let url;
  try {url = new URL(String(message.url || ''));} catch {url = null;}
  const format = String(message.format || '').toLowerCase();
  if (!message.token || !/^[0-9a-f]{32}$/.test(String(message.ticket || '')) || !message.itemId ||
      !url || !/^https?:$/.test(url.protocol) || !['pdf', 'caj'].includes(format)) {
    sendResponse({ok: false, error: '下载信息不完整，请重新保存'});
    return;
  }
  const filename = `文献工作台/${fileStem(message.title)}-${message.ticket.slice(0, 12)}.${format}`;
  chrome.downloads.download({url: url.href, filename, conflictAction: 'uniquify', saveAs: false}, downloadId => {
    const error = chrome.runtime.lastError;
    if (error || !downloadId) {
      sendResponse({ok: false, error: error?.message || '浏览器拒绝下载，请确认当前网站具有全文访问权限'});
      return;
    }
    chrome.storage.session.set({[pendingKey(downloadId)]: {
      token: message.token, ticket: message.ticket, itemId: message.itemId, format,
      pageUrl: message.pageUrl, url: url.href, createdAt: Date.now()
    }}).then(async () => {
      sendResponse({ok: true, downloadId});
      // Small cached downloads can finish before their tracking record commits.
      try {
        const [item] = await chrome.downloads.search({id: downloadId});
        if (item?.state) await importCompletedDownload({id: downloadId, state: {current: item.state}});
      } catch (error) {
        await report('error', (error.message || '未能检查浏览器下载') + '；打开扩展可重试全文导入').catch(() => {});
      }
    })
      .catch(exc => sendResponse({ok: false, error: exc.message || '无法记录下载任务'}));
  });
  return true;
});

async function importCompletedDownload(change) {
  const key = pendingKey(change.id);
  if (!change.state?.current || importing.has(change.id)) return;
  if (change.state.current !== 'interrupted' && change.state.current !== 'complete') return;
  importing.add(change.id);
  try {
    const data = (await chrome.storage.session.get(key))[key];
    if (!data) return;
    if (change.state.current === 'interrupted') {
      await chrome.storage.session.remove(key);
      await report('error', '全文下载未完成，请在浏览器下载列表中查看失败原因');
      return;
    }
    if (data.createdAt && Date.now() - data.createdAt > 2 * 60 * 60 * 1000) {
      await chrome.storage.session.remove(key);
      await report('error', '全文下载的导入凭据已过期，浏览器文件仍保留；请手动导入或重新打开网页保存');
      return;
    }
    const [item] = await chrome.downloads.search({id: change.id});
    if (!item?.filename) throw Error('浏览器未返回下载文件位置');
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
    let result;
    try {
      const response = await fetch(apiRoot + '/importDownloaded', {method: 'POST', signal: controller.signal, headers: {
        'Content-Type': 'application/json', 'X-Research-Browser': data.token
      }, body: JSON.stringify({itemId: data.itemId, ticket: data.ticket, path: item.filename,
        sourceUrl: data.pageUrl, downloadUrl: data.url, format: data.format})});
      result = await response.json().catch(() => null);
      if (!response.ok) throw Error(result?.error || `本机文献库返回 ${response.status}`);
      if (!result?.attachmentId) throw Error('本机文献库返回内容不完整，请重试导入');
    } catch (error) {
      if (controller.signal.aborted) throw Error('全文导入超时，请重试');
      throw error;
    } finally {clearTimeout(timer);}
    await chrome.storage.session.remove(key);
    await report('success', result.message || '全文已导入文献库');
  } catch (error) {
    if (/下载.*凭据.*(?:失效|过期)/.test(error.message || '')) {
      await chrome.storage.session.remove(key);
      await report('error', '全文导入凭据已失效，浏览器文件仍保留；请手动导入或重新打开网页保存');
    } else {
      await report('error', (error.message || '下载文件导入失败') + '；打开扩展可重试全文导入');
    }
  } finally {importing.delete(change.id);}
}

async function retryCompletedDownloads(token) {
  const pending = await chrome.storage.session.get(null);
  for (const [key, data] of Object.entries(pending)) {
    if (!key.startsWith('browser-download:')) continue;
    const id = Number(key.slice('browser-download:'.length));
    if (!Number.isInteger(id)) continue;
    if (typeof token === 'string' && token && data.token !== token) {
      await chrome.storage.session.set({[key]: {...data, token}});
    }
    const [item] = await chrome.downloads.search({id});
    if (item?.state) await importCompletedDownload({id, state: {current: item.state}});
  }
}

chrome.downloads.onChanged.addListener(importCompletedDownload);
