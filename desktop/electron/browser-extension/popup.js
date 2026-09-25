const apiRoot = 'http://127.0.0.1:28886/api/browser';
const launcherUrl = 'researchlibrary://browser-capture';
const element = id => document.getElementById(id);
let token = '', pageUrl = '', duplicateTimer = 0, duplicateSequence = 0, nativeDownloads = [];
function message(value, kind = '') {const node = element('message'); node.textContent = value; node.className = kind;}
async function request(path, method = 'GET', body) {
  const response = await fetch(apiRoot + path, {method, headers: {'Content-Type': 'application/json', 'X-Research-Browser': token}, body: body ? JSON.stringify(body) : undefined});
  const value = await response.json().catch(() => ({}));
  if (!response.ok) {const error = Error(value.error || `本机服务返回 ${response.status}`); error.status = response.status; throw error;}
  return value;
}
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
function localServiceUnavailable(error) { return !Number.isInteger(error?.status); }
async function connectOrWakeDesktop() {
  try {return await request('/connect', 'POST');}
  catch (error) {if (!localServiceUnavailable(error)) throw error;}
  message('正在启动文献工作台并连接本机文献库…');
  // This only starts the installed local application. No page metadata,
  // cookies or credentials are included in the URI.
  try {await chrome.tabs.create({url: launcherUrl, active: false});} catch {}
  const deadline = Date.now() + 25000;
  let lastError;
  while (Date.now() < deadline) {
    await sleep(650);
    try {return await request('/connect', 'POST');}
    catch (error) {lastError = error; if (!localServiceUnavailable(error)) throw error;}
  }
  throw Error(lastError?.message || '文献工作台未能启动。请确认桌面端已安装后重试。');
}
function extract() {
  // chrome.scripting serializes only this function. Keep site-specific helpers
  // inside it so they are available in the target page context.
  const cleanText = value => String(value || '').replace(/\s+/g, ' ').trim();
  const cnkiType = value => {
    const text = cleanText(value);
    if (/学位论文/.test(text)) return 'thesis';
    if (/会议/.test(text)) return 'paper-conference';
    if (/图书/.test(text)) return 'book';
    if (/报纸/.test(text)) return 'article-newspaper';
    return /期刊/.test(text) ? 'article-journal' : 'document';
  };
  const cnkiRecord = () => {
    if (!/(^|\.)cnki\.net$/i.test(location.hostname || '')) return null;
    const classification = (type, venue) => [
      '来源：知网',
      `类型：${{'article-journal': '期刊论文', 'paper-conference': '会议论文', thesis: '学位论文', book: '图书', 'article-newspaper': '报纸'}[type] || '其他文献'}`,
      ...(venue ? [`期刊：${venue}`] : [])
    ];
    const officialDownloads = () => [...document.querySelectorAll('a')].flatMap(link => {
      const label = cleanText(link.textContent);
      const href = String(link.href || '').trim();
      if (!href || !/^https:\/\//i.test(href) || !/(^|\.)cnki\.(net|com\.cn)(?:\/|$)/i.test(href)) return [];
      if (/PDF\s*下载/i.test(label)) return [{format: 'pdf', url: href, label: '使用知网授权下载 PDF（保存后自动解析）'}];
      if (/CAJ\s*下载/i.test(label)) return [{format: 'caj', url: href, label: '使用知网授权下载 CAJ 原件'}];
      return [];
    }).filter((value, index, rows) => rows.findIndex(other => other.format === value.format && other.url === value.url) === index);
    const rows = [...document.querySelectorAll('tr')].filter(row => row.querySelector('td.name a, td.name a.fz14'));
    const chosen = rows.find(row => row.querySelector('input.cbItem:checked, input[type="checkbox"]:checked')) || (rows.length === 1 ? rows[0] : null);
    if (rows.length && !chosen) return {requiresSelection: true};
    if (chosen) {
      const titleLink = chosen.querySelector('td.name a, td.name a.fz14');
      const title = cleanText(titleLink?.textContent);
      if (!title) return null;
      const href = titleLink?.href || location.href;
      const type = cnkiType(chosen.querySelector('td.data')?.textContent);
      const venue = cleanText(chosen.querySelector('td.source')?.textContent);
      return {pageUrl: href, type, title,
        authors: [...chosen.querySelectorAll('td.author a')].map(author => cleanText(author.textContent)).filter(Boolean),
        year: (cleanText(chosen.querySelector('td.date')?.textContent).match(/(?:19|20)\d{2}/) || [])[0] || '', DOI: '',
        venue, abstract: '', pdfUrl: '', tags: classification(type, venue), nativeDownloads: [], cnkiDetailUrl: href};
    }
    const titleNode = document.querySelector('.brief h1, .wx-tit h1, #chTitle');
    const title = cleanText(titleNode?.textContent);
    if (!title) return null;
    const issue = cleanText(document.querySelector('.top-tip')?.textContent);
    const venue = cleanText(document.querySelector('.top-tip a')?.textContent).replace(/[。.\s]+$/,'') || '';
    const type = 'article-journal';
    return {pageUrl: location.href, type, title,
      authors: [...document.querySelectorAll('#authorpart a')].map(author => cleanText(author.textContent).replace(/[\d,]+$/,'')).filter(Boolean),
      year: (issue.match(/(?:19|20)\d{2}/) || [])[0] || '', DOI: '', venue,
      abstract: cleanText(document.querySelector('#ChDivSummary, .abstract-text')?.textContent), pdfUrl: '',
      tags: classification(type, venue), nativeDownloads: officialDownloads()};
  };
  const cnki = cnkiRecord();
  if (cnki) return cnki;
  const metas = [...document.querySelectorAll('meta')];
  const values = name => metas.filter(meta => (meta.getAttribute('name') || meta.getAttribute('property') || '').toLowerCase() === name.toLowerCase())
    .map(meta => (meta.getAttribute('content') || '').trim()).filter(Boolean);
  const first = (...names) => names.flatMap(values)[0] || '';
  const rawDate = first('citation_date','citation_publication_date','citation_online_date','dc.date','prism.publicationdate','article:published_time');
  const pdf = first('citation_pdf_url','dc.identifier.pdf');
  const href = document.querySelector('link[type="application/pdf"]')?.href || '';
  let pdfUrl = href;
  if (pdf) {try {pdfUrl = new URL(pdf, location.href).href;} catch {pdfUrl = href;}}
  // ScienceDirect does not consistently expose citation_pdf_url. Its article
  // page holds a short-lived, article-specific /pdfft link instead. Only use
  // that link when the current article itself is visibly marked Open Access;
  // links in the reference list must never be mistaken for the main article.
  const currentUrl = new URL(location.href);
  const scienceDirect = /(^|\.)sciencedirect\.com$/i.test(currentUrl.hostname);
  const pii = (currentUrl.pathname.match(/\/article\/pii\/([^/?#]+)/i) || [])[1] || '';
  let fulltextNote = '';
  if (scienceDirect && pii) {
    // The OA badge lives in ScienceDirect's article header, outside its
    // <article> body. Inspect <main> so translated and original pages work.
    const pageText = cleanText((document.querySelector('main') || document.body)?.textContent).slice(0, 5000);
    const openAccess = /\bopen\s+access\b|开放获取/i.test(pageText);
    const articlePdf = [...document.querySelectorAll('a[href]')].find(link => {
      const url = String(link.href || '');
      const label = cleanText(link.getAttribute('aria-label') || link.textContent);
      return url.includes(`/science/article/pii/${pii}/pdfft`) && /pdf/i.test(label + ' ' + url);
    });
    if (openAccess && articlePdf?.href) {
      pdfUrl = articlePdf.href;
      fulltextNote = '已识别 ScienceDirect 开放获取 PDF；保存后将自动下载并建立全文索引。';
    } else {
      // A publisher PDF link without an OA marker might require entitlement.
      // Do not silently send it to the local public-downloader.
      pdfUrl = '';
      fulltextNote = '未识别到可公开下载的 PDF。若有机构访问权限，请在期刊页面下载后，通过“添加附件”导入。';
    }
  }
  const arxiv = /^(?:www\.)?arxiv\.org$/i.test(new URL(location.href).hostname);
  const directPdf = /\.pdf(?:$|[?#])/i.test(location.pathname) || (arxiv && /^\/pdf\//i.test(location.pathname));
  const authors = values('citation_author').length ? values('citation_author') : values('dc.creator');
  const year = (rawDate.match(/(?:19|20)\d{2}/) || [])[0] || '';
  const type = first('citation_conference_title') ? 'paper-conference' : first('citation_dissertation_institution') ? 'thesis' : first('citation_book_title') ? 'book' :
    first('citation_journal_title') ? 'article-journal' : directPdf || (arxiv && !!first('citation_arxiv_id')) ? 'document' : 'webpage';
  return {pageUrl: location.href, type, title: first('citation_title','dc.title','og:title') || document.title.replace(/\s+[|–-]\s+[^|–-]+$/, '').trim(),
    authors, year, DOI: first('citation_doi','dc.identifier.doi','prism.doi').replace(/^https?:\/\/(?:dx\.)?doi\.org\//i,''),
    venue: first('citation_journal_title','citation_conference_title','prism.publicationname'),
    abstract: first('citation_abstract','dc.description','description'),
    pdfUrl: directPdf ? location.href : pdfUrl, fulltextNote, tags: [], nativeDownloads: []};
 }
async function enrichCnkiDetail(detailUrl) {
  // This runs in the current CNKI tab's isolated world. It uses the browser's
  // existing authenticated request context but never reads cookies or credentials.
  const cleanText = value => String(value || '').replace(/\s+/g, ' ').trim();
  const url = new URL(String(detailUrl || ''), location.href);
  const host = url.hostname.toLowerCase();
  if (url.protocol !== 'https:' || !(host === 'cnki.net' || host.endsWith('.cnki.net') || host === 'cnki.com.cn' || host.endsWith('.cnki.com.cn'))) throw Error('详情页地址不是知网官方地址');
  const response = await fetch(url.href, {credentials: 'include', redirect: 'follow'});
  if (!response.ok) throw Error(`知网详情页无法访问（${response.status}）`);
  const document = new DOMParser().parseFromString(await response.text(), 'text/html');
  const title = cleanText(document.querySelector('.brief h1, .wx-tit h1, #chTitle')?.textContent);
  if (!title) throw Error('知网未返回可识别的文章详情；可能需要先完成页面安全验证');
  const issue = cleanText(document.querySelector('.top-tip')?.textContent);
  const venue = cleanText(document.querySelector('.top-tip a')?.textContent).replace(/[。.\s]+$/,'');
  const keywords = [...document.querySelectorAll('#catalog_KEYWORD a, #catalog_KEYWORD span, .keywords a, .keywords span, [id*="KEYWORD"] a')]
    .map(node => cleanText(node.textContent)).filter(value => value && value !== '关键词').slice(0, 8);
  return {title, authors: [...document.querySelectorAll('#authorpart a')]
    .map(author => cleanText(author.textContent).replace(/[\d,]+$/,'')).filter(Boolean),
    year: (issue.match(/(?:19|20)\d{2}/) || [])[0] || '', venue,
    abstract: cleanText(document.querySelector('#ChDivSummary, .abstract-text, .abstract')?.textContent),
    keywords: [...new Set(keywords)]};
}
async function currentPage() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  if (!tab || !/^https?:\/\//i.test(tab.url || '')) throw Error('请打开论文网页或公开 PDF 后再保存');
  pageUrl = tab.url;
  let result;
  try {[{result}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: extract});}
  catch {const url = new URL(tab.url), path = url.pathname, isPdf = /\.pdf(?:$|[?#])/i.test(path) || (/^(?:www\.)?arxiv\.org$/i.test(url.hostname) && /^\/pdf\//i.test(path));
    return {pageUrl: tab.url, type: isPdf ? 'document' : 'webpage', title: decodeURIComponent(path.split('/').pop() || tab.title || '网页文献').replace(/\.pdf$/i,''),
      authors: [], year: '', DOI: '', venue: '', abstract: '', pdfUrl: isPdf ? tab.url : ''};}
  if (!result || typeof result !== 'object') throw Error('未能从当前页面读取题录，请打开文章详情页后重试。');
  if (result.requiresSelection) throw Error('知网结果页包含多篇文献，请先勾选一篇后再保存；也可打开该文献详情页。');
  pageUrl = result.pageUrl || tab.url;
  if (result.cnkiDetailUrl) {
    try {
      const [{result: details}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: enrichCnkiDetail, args: [result.cnkiDetailUrl]});
      if (details && typeof details === 'object') {
        result = {...result, title: details.title || result.title, authors: details.authors?.length ? details.authors : result.authors,
          year: details.year || result.year, venue: details.venue || result.venue, abstract: details.abstract || result.abstract,
          tags: [...new Set([...(result.tags || []), ...(details.keywords || []).map(keyword => `关键词：${keyword}`)])],
          detailEnriched: true};
      }
    } catch (error) {result.detailEnrichmentError = error.message || '未能自动补全知网详情页数据';}
  }
  return result;
}
function fill(data) {
  nativeDownloads = Array.isArray(data.nativeDownloads) ? data.nativeDownloads : [];
  const classification = element('classification');
  classification.textContent = (data.tags || []).join(' · ');
  classification.hidden = !classification.textContent;
  const native = element('native-download');
  native.replaceChildren(new Option('仅保存题录', ''));
  for (const choice of nativeDownloads) native.add(new Option(choice.label, choice.format + ':' + nativeDownloads.indexOf(choice)));
  element('native-download-wrap').hidden = !nativeDownloads.length;
  element('type').value = data.type || 'document';
  element('title').value = data.title || '';
  element('authors').value = (data.authors || []).join('; ');
  element('year').value = data.year || '';
  element('doi').value = data.DOI || '';
  element('venue').value = data.venue || '';
  element('abstract').value = data.abstract || '';
  element('pdf').value = data.pdfUrl || '';
  element('pdf-label').textContent = data.pdfUrl ? '已识别的公开 PDF 地址' : '公开 PDF 地址';
  const fulltextNote = element('fulltext-note');
  fulltextNote.textContent = data.fulltextNote || (data.pdfUrl ? '已识别公开 PDF；保存后会自动下载并建立全文索引。' : '未检测到公开 PDF；可留空，稍后在附件页添加。');
  fulltextNote.className = data.pdfUrl ? 'fulltext-note success' : 'fulltext-note';
  element('download').checked = !!data.pdfUrl;
  element('download').disabled = !data.pdfUrl;
  element('source').textContent = '来源：' + pageUrl;
  scheduleDuplicateCheck();
}
function duplicatePayload() {
  return {pageUrl, data: {title: element('title').value.trim(),
    author: element('authors').value.split(/[;；]/).map(value => value.trim()).filter(Boolean),
    year: element('year').value.trim(), DOI: element('doi').value.trim()}};
}
function scheduleDuplicateCheck() {
  clearTimeout(duplicateTimer);
  const sequence = ++duplicateSequence, node = element('duplicate');
  node.hidden = true;
  if (!pageUrl || !element('title').value.trim() || !/^\d{0,4}$/.test(element('year').value.trim())) return;
  duplicateTimer = setTimeout(async () => {
    try {
      const {duplicate} = await request('/duplicates', 'POST', duplicatePayload());
      if (sequence !== duplicateSequence || !duplicate) return;
      node.textContent = `库中已有《${duplicate.title}》${duplicate.year ? `（${duplicate.year}）` : ''}；依据：${duplicate.basis}。保存时会复用该条目，并补充集合与 PDF 来源。`;
      node.hidden = false;
    } catch {node.hidden = true;}
  }, 350);
}
for (const name of ['title', 'authors', 'year', 'doi']) element(name).addEventListener('input', scheduleDuplicateCheck);
element('pdf').addEventListener('input', () => {
  const hasPdf = !!element('pdf').value.trim();
  if (hasPdf && element('download').disabled) element('download').checked = true;
  element('download').disabled = !hasPdf;
  if (!hasPdf) element('download').checked = false;
});
async function collections() {
  const {collections} = await request('/collections');
  const select = element('collection'), selected = select.value;
  select.replaceChildren(new Option('我的文献',''));
  for (const item of collections) select.add(new Option(item.name, item.id));
  select.value = selected;
}
function downloadNative(choice, itemId) {
  return new Promise((resolve, reject) => chrome.runtime.sendMessage({type: 'download-cnki', token, itemId, sourceUrl: pageUrl, url: choice.url, format: choice.format, title: element('title').value.trim()}, response => {
    const error = chrome.runtime.lastError;
    if (error) return reject(Error(error.message));
    if (!response?.ok) return reject(Error(response?.error || '无法启动浏览器授权下载'));
    resolve(response);
  }));
}
async function showRecord() {
  element('connection').hidden = true; element('record').hidden = false;
  await collections();
  try {const data = await currentPage(); fill(data); return data;} catch (error) {element('save').disabled = true; message(error.message,'error'); return null;}
}
async function initialize() {
  token = (await chrome.storage.local.get('token')).token || '';
  if (token) {
    try {const data = await showRecord(); if (data?.detailEnriched) message('已自动补全知网详情页的摘要与关键词', 'success'); else if (data?.detailEnrichmentError) message(data.detailEnrichmentError, 'error'); return;} catch (error) {
      if (error.status === 401 || error.status === 403) {await chrome.storage.local.remove('token'); token = '';}
      else if (!localServiceUnavailable(error)) throw error;
    }
  }
  const result = await connectOrWakeDesktop();
  token = result.token;
  await chrome.storage.local.set({token});
  const data = await showRecord();
  message(data?.detailEnriched ? '已连接本机文献库，并自动补全知网详情页数据' : data?.detailEnrichmentError || '已自动连接本机文献库', data?.detailEnrichmentError ? 'error' : 'success');
}
chrome.runtime.onMessage.addListener(event => {
  if (event?.type === 'download-status') message(event.message, event.kind === 'success' ? 'success' : 'error');
});
element('retry').addEventListener('click', async () => {
  try {await initialize();} catch (error) {element('connection').hidden = false; element('record').hidden = true; message(error.message, 'error');}
});
element('create-collection').addEventListener('click', async () => {
  try {const name = element('new-collection').value.trim(); if (!name) throw Error('请输入集合名称');
    const result = await request('/collections','POST',{name}); await collections();
    element('collection').value = result.id; element('new-collection').value = ''; message('集合已创建','success');
  } catch (error) {message(error.message,'error');}
});
element('save').addEventListener('click', async () => {
  const button = element('save'); button.disabled = true;
  try {
    const title = element('title').value.trim(); if (!title) throw Error('请填写题名');
    const year = element('year').value.trim(); if (year && !/^\d{4}$/.test(year)) throw Error('年份应为四位数字');
    const authors = element('authors').value.split(/[;；]/).map(value => value.trim()).filter(Boolean);
    const pdfUrl = element('pdf').value.trim(); if (pdfUrl && !/^https?:\/\//i.test(pdfUrl)) throw Error('公开 PDF 地址应以 http(s) 开头');
    const result = await request('/capture','POST',{pageUrl, collectionId: element('collection').value || null, pdfUrl,
      downloadPdf: element('download').checked,
      data: {type: element('type').value, title, author: authors, year, DOI: element('doi').value.trim(),
        'container-title': element('venue').value.trim(), abstract: element('abstract').value.trim(),
        tags: (element('classification').textContent || '').split(' · ').map(value => value.trim()).filter(Boolean)}});
    const selected = element('native-download').value;
    const nativeChoice = selected ? nativeDownloads[Number(selected.split(':')[1])] : null;
    if (nativeChoice) await downloadNative(nativeChoice, result.itemId);
    message((result.created ? '已保存新文献' : '文献已存在，已补充集合和来源') +
      (nativeChoice ? `；已交由浏览器使用当前知网授权下载 ${nativeChoice.format.toUpperCase()}，完成后会自动导入${nativeChoice.format === 'pdf' ? '并解析索引' : '为原件'}` :
       result.downloadJobId ? '；公开 PDF 下载与索引任务已开始' : result.pdfAlreadyAttached ? '；PDF 已在文献库中' : result.downloadSkippedOffline ? '；联网已关闭，已记录 PDF 来源但未下载' : result.pdfSourceId ? '；已记录 PDF 来源' : ''),'success');
    scheduleDuplicateCheck();
  } catch (error) {message(error.message,'error');} finally {button.disabled = false;}
});
initialize().catch(error => {element('connection').hidden = false; element('record').hidden = true; message(error.message || '无法连接本机文献库', 'error');});
