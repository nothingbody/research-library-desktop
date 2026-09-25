const apiRoot = 'http://127.0.0.1:28886/api/browser';
const pendingKey = id => `browser-download:${id}`;

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
      pageUrl: message.pageUrl, url: url.href
    }}).then(() => sendResponse({ok: true, downloadId}))
      .catch(exc => sendResponse({ok: false, error: exc.message || '无法记录下载任务'}));
  });
  return true;
});

chrome.downloads.onChanged.addListener(async change => {
  const key = pendingKey(change.id);
  const data = (await chrome.storage.session.get(key))[key];
  if (!data || !change.state?.current) return;
  if (change.state.current !== 'interrupted' && change.state.current !== 'complete') return;
  await chrome.storage.session.remove(key);
  if (change.state.current === 'interrupted') {
    await report('error', '全文下载未完成，请在浏览器下载列表中查看失败原因');
    return;
  }
  try {
    const [item] = await chrome.downloads.search({id: change.id});
    if (!item?.filename) throw Error('浏览器未返回下载文件位置');
    const response = await fetch(apiRoot + '/importDownloaded', {method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-Research-Browser': data.token
    }, body: JSON.stringify({itemId: data.itemId, ticket: data.ticket, path: item.filename,
      sourceUrl: data.pageUrl, downloadUrl: data.url, format: data.format})});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(result.error || `本机文献库返回 ${response.status}`);
    await report('success', result.message || '全文已导入文献库');
  } catch (error) {
    await report('error', error.message || '下载文件导入失败');
  }
});
