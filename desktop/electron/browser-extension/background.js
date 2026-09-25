const apiRoot = 'http://127.0.0.1:28886/api/browser';
const pendingKey = id => `cnki-download:${id}`;
async function remember(id, data) { await chrome.storage.session.set({[pendingKey(id)]: data}); }
async function pending(id) { return (await chrome.storage.session.get(pendingKey(id)))[pendingKey(id)] || null; }
async function forget(id) { await chrome.storage.session.remove(pendingKey(id)); }
function officialCnki(url) {
  try {const value = new URL(url); return value.protocol === 'https:' && (value.hostname === 'cnki.net' || value.hostname.endsWith('.cnki.net') || value.hostname === 'cnki.com.cn' || value.hostname.endsWith('.cnki.com.cn'));}
  catch {return false;}
}
const publisherHosts = ['link.springer.com', 'onlinelibrary.wiley.com', 'tandfonline.com',
  'journals.sagepub.com', 'pubs.acs.org', 'dl.acm.org', 'mdpi.com',
  'academic.oup.com', 'ieeexplore.ieee.org', 'sciencedirect.com'];
function officialPublisher(url) {
  try {const value = new URL(url); return value.protocol === 'https:' && publisherHosts.some(host => value.hostname === host || value.hostname.endsWith('.' + host));}
  catch {return false;}
}
function samePublisher(left, right) {
  try {const a = new URL(left).hostname, b = new URL(right).hostname;
    return a === b || a.endsWith('.' + b) || b.endsWith('.' + a);
  } catch {return false;}
}
function fileStem(value) {return String(value || 'CNKI 文献').replace(/[<>:"/\\|?*\x00-\x1f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 100) || 'CNKI 文献';}
async function importCompleted(data, downloadId) {
  const [item] = await chrome.downloads.search({id: downloadId});
  if (!item?.filename) throw Error('浏览器未返回下载文件位置');
  const response = await fetch(apiRoot + '/importDownloaded', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Research-Browser': data.token},
    body: JSON.stringify({itemId: data.itemId, sourceUrl: data.sourceUrl, downloadUrl: data.downloadUrl, format: data.format, path: item.filename})});
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(value.error || `本机文献库返回 ${response.status}`);
  await chrome.runtime.sendMessage({type: 'download-status', kind: 'success', message: value.message || '下载文件已导入文献库'}).catch(() => {});
}
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const publisher = message?.type === 'download-publisher';
  if (!publisher && message?.type !== 'download-cnki') return;
  const format = publisher ? 'pdf' : String(message.format || '').toLowerCase();
  const valid = publisher ? officialPublisher(message.url) && officialPublisher(message.sourceUrl) && samePublisher(message.url, message.sourceUrl) :
    ['pdf', 'caj'].includes(format) && officialCnki(message.url) && officialCnki(message.sourceUrl);
  if (!message.token || !message.itemId || !valid) {sendResponse({ok: false, error: publisher ? '期刊 PDF 地址与当前论文站点不匹配' : '知网授权下载信息不正确'}); return;}
  chrome.downloads.download({url: message.url, filename: `文献工作台/${fileStem(message.title)}.${format}`, conflictAction: 'uniquify', saveAs: false}, downloadId => {
    const error = chrome.runtime.lastError;
    if (error || !downloadId) {sendResponse({ok: false, error: error?.message || '浏览器拒绝下载，请确认当前站点具有全文权限'}); return;}
    remember(downloadId, {token: message.token, itemId: message.itemId, sourceUrl: message.sourceUrl, downloadUrl: publisher ? message.url : '', format, publisher})
      .then(() => sendResponse({ok: true, downloadId}))
      .catch(storageError => sendResponse({ok: false, error: storageError.message || '无法记录下载任务'}));
  });
  return true;
});
chrome.downloads.onChanged.addListener(async change => {
  const data = await pending(change.id);
  if (!data) return;
  if (change.state?.current === 'interrupted') {
    await forget(change.id);
    await chrome.runtime.sendMessage({type: 'download-status', kind: 'error', message: data.publisher ? '期刊 PDF 下载未完成，请确认页面访问权限或在浏览器下载列表中重试' : '知网下载未完成，请确认全文权限或在浏览器下载列表中重试'}).catch(() => {});
    return;
  }
  if (change.state?.current !== 'complete') return;
  await forget(change.id);
  try {await importCompleted(data, change.id);} catch (error) {await chrome.runtime.sendMessage({type: 'download-status', kind: 'error', message: error.message || '下载文件导入失败'}).catch(() => {});}
});
