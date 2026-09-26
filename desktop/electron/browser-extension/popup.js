const apiRoot = 'http://127.0.0.1:28886/api/browser';
const launcherUrl = 'researchlibrary://browser-capture';
const element = id => document.getElementById(id);
let token = '', pageUrl = '', activeTabId = 0, duplicateTimer = 0, duplicateSequence = 0, nativeDownloads = [], batchRecords = [];
function message(value, kind = '') {const node = element('message'); node.textContent = value; node.className = kind;}
async function request(path, method = 'GET', body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), path === '/connect' ? 5000 : 15000);
  try {
    const response = await fetch(apiRoot + path, {method, signal: controller.signal,
      headers: {'Content-Type': 'application/json', 'X-Research-Browser': token}, body: body ? JSON.stringify(body) : undefined});
    const value = await response.json().catch(() => null);
    if (!response.ok) {const error = Error(value?.error || `本机服务返回 ${response.status}`); error.status = response.status; throw error;}
    const valid = value && typeof value === 'object' && !Array.isArray(value) &&
      (path !== '/connect' || (typeof value.token === 'string' && value.token)) &&
      (path !== '/collections' || method !== 'GET' || Array.isArray(value.collections)) &&
      (path !== '/capture' || (typeof value.itemId === 'string' && value.itemId));
    if (!valid) {const error = Error('本机服务返回内容不完整，请重试'); error.status = response.status; throw error;}
    return value;
  } catch (error) {
    if (controller.signal.aborted) throw Error('连接本机文献库超时，请重试');
    throw error;
  } finally {clearTimeout(timer);}
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
  const cnkiHost = host => /(^|\.)cnki\.(net|com\.cn)$/i.test(host || '') || /(^|[.-])cnki[.-](net|com[.-]cn)([.-]|$)/i.test(host || '');
  const cnkiDbType = (url, text) => {
    let code = '';
    try {const value = new URL(url, location.href); code = (value.searchParams.get('dbname') || value.searchParams.get('dbcode') || '').toUpperCase();} catch {}
    if (/^(CDFD|CMFD|CDMD)/.test(code) || /(?:博士|硕士)?学位论文/.test(text)) return 'thesis';
    if (/^(CPFD|IPFD|CPVD)/.test(code) || /会议/.test(text)) return 'paper-conference';
    if (/^CCND/.test(code) || /报纸/.test(text)) return 'article-newspaper';
    return 'article-journal';
  };
  const cnkiDownloads = root => [...root.querySelectorAll('a')].flatMap(link => {
    const label = cleanText(link.textContent);
    let href;
    try {href = new URL(link.getAttribute('href') || '', location.href);} catch {return [];}
    if (!/^https?:$/.test(href.protocol) || !cnkiHost(href.hostname)) return [];
    if (/PDF\s*下载|下载\s*PDF/i.test(label)) return [{format: 'pdf', url: href.href, label: '用浏览器下载 PDF（使用当前知网权限）'}];
    if (/CAJ\s*下载|整本下载|分章下载|分页下载/i.test(label)) return [{format: 'caj', url: href.href, label: `用浏览器${label}（CAJ 原件；实际为 PDF 时自动解析）`}];
    return [];
  }).filter((value, index, rows) => rows.findIndex(other => other.url === value.url) === index);
  const cnkiRecord = () => {
    if (!cnkiHost(location.hostname)) return null;
    const classification = (type, venue) => [
      '来源：知网',
      `类型：${{'article-journal': '期刊论文', 'paper-conference': '会议论文', thesis: '学位论文', book: '图书', 'article-newspaper': '报纸'}[type] || '其他文献'}`,
      ...(venue ? [`期刊：${venue}`] : [])
    ];
    const rows = [...document.querySelectorAll('tr')].filter(row => row.querySelector('td.name a, td.name a.fz14'));
    const selected = rows.filter(row => row.querySelector('input.cbItem:checked, input[type="checkbox"]:checked'));
    const fromRow = chosen => {
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
    };
    if (rows.length > 1) return {candidates: rows.slice(0, 50).map(row => ({...fromRow(row), selected: selected.length ? selected.includes(row) : true})).filter(row => row.title)};
    if (rows.length === 1) return fromRow(rows[0]);
    const titleNode = document.querySelector('.brief h1, .wx-tit h1, #chTitle');
    const title = cleanText(titleNode?.textContent);
    if (!title) return null;
    const issue = cleanText(document.querySelector('.top-tip')?.textContent);
    const venue = cleanText(document.querySelector('.top-tip a')?.textContent).replace(/[。.\s]+$/,'') || '';
    const type = cnkiDbType(location.href, issue + ' ' + cleanText(document.querySelector('.wx-tit, .brief')?.textContent).slice(0, 400));
    return {pageUrl: location.href, type, title,
      authors: [...document.querySelectorAll('#authorpart a')].map(author => cleanText(author.textContent).replace(/[\d,]+$/,'')).filter(Boolean),
      year: (issue.match(/(?:19|20)\d{2}/) || [])[0] || '', DOI: '', venue,
      abstract: cleanText(document.querySelector('#ChDivSummary, .abstract-text')?.textContent), pdfUrl: '',
      tags: classification(type, venue), nativeDownloads: cnkiDownloads(document)};
  };
  const cnki = cnkiRecord();
  if (cnki) return cnki;
  const metas = [...document.querySelectorAll('meta')];
  const values = name => metas.filter(meta => (meta.getAttribute('name') || meta.getAttribute('property') || '').toLowerCase() === name.toLowerCase())
    .map(meta => (meta.getAttribute('content') || '').trim()).filter(Boolean);
  const first = (...names) => names.flatMap(values)[0] || '';
  const absolute = value => {try {const url = new URL(String(value || ''), location.href); return /^https?:$/.test(url.protocol) ? url.href : '';} catch {return '';}};
  const doiValue = value => (String(value || '').match(/10\.\d{4,9}\/[^\s<>"?#]+/i) || [])[0] || '';
  const structured = [];
  const addStructured = record => {
    if (!record?.title) return;
    const key = doiValue(record.DOI) || cleanText(record.title).toLowerCase();
    if (structured.some(item => (doiValue(item.DOI) || cleanText(item.title).toLowerCase()) === key)) return;
    structured.push(record);
  };
  const jsonNodes = [...document.querySelectorAll('script[type="application/ld+json"]')];
  const visitJson = (node, depth = 0) => {
    if (!node || depth > 5) return;
    if (Array.isArray(node)) {node.forEach(value => visitJson(value, depth + 1)); return;}
    if (typeof node !== 'object') return;
    if (node['@graph']) visitJson(node['@graph'], depth + 1);
    const kinds = [node['@type']].flat().map(value => String(value || '').toLowerCase());
    const type = kinds.some(value => /scholarlyarticle/.test(value)) ? 'article-journal' :
      kinds.some(value => /book/.test(value)) ? 'book' : kinds.some(value => /thesis/.test(value)) ? 'thesis' :
      kinds.some(value => /article/.test(value)) ? 'webpage' : '';
    if (!type || !cleanText(node.headline || node.name)) return;
    const authors = [node.author].flat().filter(Boolean).map(value => cleanText(typeof value === 'string' ? value :
      value.name || [value.givenName, value.familyName].filter(Boolean).join(' '))).filter(Boolean);
    const part = node.isPartOf || node.publication || {};
    const venue = type === 'book' ? '' : cleanText(typeof part === 'string' ? part : part.name || '');
    const identifier = [node.doi, node.identifier, node.sameAs].flat().find(value => doiValue(typeof value === 'string' ? value : value?.value || value?.['@id'])) || '';
    const encoding = [node.encoding, node.associatedMedia].flat().find(value => value && /pdf/i.test(String(value.encodingFormat || value.fileFormat || value.contentUrl || '')));
    addStructured({pageUrl: absolute(node.url || node.mainEntityOfPage?.['@id'] || node.mainEntityOfPage) || location.href,
      type, title: cleanText(node.headline || node.name), authors,
      year: (String(node.datePublished || node.dateCreated || '').match(/(?:19|20)\d{2}/) || [])[0] || '',
      DOI: doiValue(typeof identifier === 'string' ? identifier : identifier?.value), venue,
      publisher: cleanText(typeof node.publisher === 'string' ? node.publisher : node.publisher?.name), ISBN: cleanText(node.isbn),
      abstract: cleanText(node.abstract || node.description), pdfUrl: absolute(encoding?.contentUrl), tags: []});
  };
  for (const script of jsonNodes) {try {visitJson(JSON.parse(script.textContent || ''));} catch {}}
  for (const node of document.querySelectorAll('span.Z3988[title], abbr.Z3988[title]')) {
    const raw = node.getAttribute('title') || '';
    const query = new URL('https://coins.invalid/?' + raw).searchParams;
    const title = cleanText(query.get('rft.atitle') || query.get('rft.title') || query.get('rft.btitle'));
    if (!title) continue;
    const authors = query.getAll('rft.au').map(cleanText).filter(Boolean);
    if (!authors.length && query.get('rft.aulast')) authors.push(cleanText([query.get('rft.aulast'), query.get('rft.aufirst')].filter(Boolean).join(', ')));
    const contextLink = node.closest?.('article, li, tr, .gs_r, .result, .search-result')?.querySelector?.('a[href]');
    const coinsType = /book/i.test(query.get('rft.genre') || '') ? 'book' : /article/i.test(query.get('rft.genre') || '') ? 'article-journal' : 'document';
    addStructured({pageUrl: absolute(contextLink?.href) || location.href,
      type: coinsType,
      title, authors, year: ((query.get('rft.date') || '').match(/(?:19|20)\d{2}/) || [])[0] || '',
      DOI: doiValue(query.getAll('rft_id').join(' ') + ' ' + (query.get('rft.doi') || '')),
      venue: coinsType === 'book' ? '' : cleanText(query.get('rft.jtitle') || query.get('rft.btitle')), publisher: cleanText(query.get('rft.pub')),
      ISBN: cleanText(query.get('rft.isbn')), abstract: '', pdfUrl: '', tags: []});
  }
  // Google Scholar result pages expose neither citation_* nor a stable article
  // API. Capture visible result cards and let the user review titles before import.
  if (/^scholar\.google\./i.test(location.hostname)) {
    for (const card of document.querySelectorAll('.gs_r.gs_or.gs_scl')) {
      const link = card.querySelector('.gs_rt a[href]');
      const title = cleanText(link?.textContent || card.querySelector('.gs_rt')?.textContent).replace(/^\[[^\]]+\]\s*/, '');
      if (!title) continue;
      const byline = cleanText(card.querySelector('.gs_a')?.textContent);
      addStructured({pageUrl: absolute(link?.href) || location.href, type: 'document', title,
        authors: byline.split(/\s+[-–]\s+/)[0].split(/[,，]/).map(cleanText).filter(Boolean),
        year: (byline.match(/(?:19|20)\d{2}/) || [])[0] || '', DOI: '', venue: '',
        abstract: cleanText(card.querySelector('.gs_rs')?.textContent), pdfUrl: '', tags: []});
    }
  }
  if (!first('citation_title') && structured.length > 1) {
    const candidates = structured.map(record => ({...record,
      pageUrl: record.pageUrl === location.href && record.DOI ? `https://doi.org/${record.DOI}` : record.pageUrl}))
      .filter(record => record.pageUrl !== location.href || /^https?:\/\/doi\.org\/10\./i.test(record.pageUrl));
    if (candidates.length) return {candidates: candidates.slice(0, 50)};
  }
  const citationTitle = cleanText(first('citation_title')).toLowerCase();
  const item = (citationTitle ? structured.find(record => cleanText(record.title).toLowerCase() === citationTitle) : structured[0]) || {};
  const unapiNode = document.querySelector('abbr.unapi-id[title], span.unapi-id[title]');
  const unapiLink = document.querySelector('link[rel="unapi-server"][href]');
  const unapiServer = absolute(unapiLink?.href);
  const unapiId = cleanText(unapiNode?.getAttribute('title'));
  const risLink = !first('citation_title') && !item.title ? [...document.querySelectorAll('link[rel="alternate"][href], a[href]')].find(link => {
    const url = absolute(link.href);
    if (!url || new URL(url).origin !== new URL(location.href).origin) return false;
    return /(?:\.ris(?:$|[?#])|[?&](?:format|type)=ris(?:&|$))/i.test(url) ||
      /research-info-systems|x-ris/i.test(link.getAttribute('type') || '');
  }) : null;
  const rawDate = first('citation_date','citation_publication_date','citation_online_date','dc.date','prism.publicationdate','article:published_time');
  const currentUrl = new URL(location.href);
  const pathDoi = (() => {try {return (decodeURIComponent(currentUrl.pathname).match(/\/(?:article|articles|doi\/(?:full|abs|pdf|epdf))\/(10\.\d{4,9}\/[^/?#]+)/i) || [])[1] || '';} catch {return '';}})();
  const queryDoi = /^10\.\d{4,9}\/\S+$/i.test(currentUrl.searchParams.get('id') || '') ? currentUrl.searchParams.get('id') : '';
  const doi = (first('citation_doi','dc.identifier.doi','prism.doi') || item.DOI || pathDoi || queryDoi)
    .replace(/^https?:\/\/(?:dx\.)?doi\.org\//i,'').trim();
  const pdf = first('citation_pdf_url','dc.identifier.pdf');
  const href = document.querySelector('link[type="application/pdf"]')?.href || '';
  let pdfUrl = '';
  for (const value of [pdf, href]) {
    try {if (value) {const candidate = new URL(value, location.href); if (/^https?:$/.test(candidate.protocol)) {pdfUrl = candidate.href; break;}}} catch {}
  }
  // Prefer the current article's official PDF control when metadata is absent.
  // Reference lists and supplementary-file controls are not main PDFs.
  const host = currentUrl.hostname.toLowerCase();
  const isHost = domain => host === domain || host.endsWith('.' + domain);
  const articleId = (currentUrl.pathname.match(/\/(?:articles|document)\/(PMC\d+|\d+)(?:\/|$)/i) || [])[1] || '';
  const doiMatches = url => {try {return !!doi && decodeURIComponent(url.href).toLowerCase().includes(doi.toLowerCase());} catch {return false;}};
  const mainArticleLink = link => !link.closest('aside, footer, [role="complementary"], [class*="reference" i], [id*="reference" i], [class*="related" i], [class*="recommend" i], [class*="supplement" i]');
  const publisherPdf = [...document.querySelectorAll('a[href]')].filter(mainArticleLink).find(link => {
    let url;
    try {url = new URL(link.href, location.href);} catch {return false;}
    if (!/^https?:$/.test(url.protocol)) return false;
    const candidateHost = url.hostname.toLowerCase();
    if (candidateHost !== host && !candidateHost.endsWith('.' + host) && !host.endsWith('.' + candidateHost)) return false;
    let path;
    try {path = decodeURIComponent(url.pathname).toLowerCase();} catch {return false;}
    const label = cleanText(link.getAttribute('aria-label') || link.getAttribute('title') || link.textContent).toLowerCase();
    if (/supplement|supporting|appendix|cover|graphical|附录|补充材料/i.test(label + ' ' + path)) return false;
    if (isHost('link.springer.com')) return /\/content\/pdf\/.+\.pdf$/i.test(path) && doiMatches(url);
    if (['onlinelibrary.wiley.com', 'tandfonline.com', 'journals.sagepub.com', 'pubs.acs.org', 'dl.acm.org'].some(isHost))
      return /\/doi\/(?:pdf|epdf)\//.test(path) && doiMatches(url);
    if (isHost('mdpi.com')) return /\/pdf\/?$/.test(path) && path.replace(/\/pdf\/?$/, '') === currentUrl.pathname.toLowerCase().replace(/\/$/, '');
    if (isHost('frontiersin.org')) return /\/articles\/.+\/pdf\/?$/.test(path) &&
      path.replace(/\/pdf\/?$/, '') === currentUrl.pathname.toLowerCase().replace(/\/(?:full|abstract)\/?$/, '');
    if (isHost('journals.plos.org')) return /\/article\/file$/.test(path) &&
      url.searchParams.get('type') === 'printable' && (url.searchParams.get('id') || '').toLowerCase() === doi.toLowerCase();
    if (isHost('pmc.ncbi.nlm.nih.gov')) return !!articleId && path.includes(`/articles/${articleId.toLowerCase()}/pdf`) && /\bpdf\b/i.test(label);
    if (isHost('academic.oup.com')) return /\/article-pdf\//.test(path) && /\bpdf\b/i.test(label);
    if (isHost('cambridge.org')) {
      const articleHash = currentUrl.pathname.split('/').filter(Boolean).at(-1)?.toLowerCase() || '';
      return /^[a-f0-9]{32}$/.test(articleHash) && path.includes(`/content/view/${articleHash}/`) && /\.pdf(?:\/|$)/.test(path) && /\bpdf\b/i.test(label);
    }
    if (isHost('ieeexplore.ieee.org')) return !!articleId &&
      (url.searchParams.get('arnumber') === articleId || path.includes(`/${articleId}-`)) && /\bpdf\b/i.test(label);
    return false;
  });
  if (!pdfUrl && publisherPdf) pdfUrl = publisherPdf.href;
  // MDPI's PDF control can be rendered after the extension reads the page.
  // Its journal article route has a stable article-specific /pdf endpoint.
  if (!pdfUrl && isHost('mdpi.com') && /^\/\d{4}-\d{4}\/\d+\/\d+\/\d+\/?$/.test(currentUrl.pathname))
    pdfUrl = new URL(currentUrl.pathname.replace(/\/$/, '') + '/pdf', currentUrl).href;
  // ScienceDirect's article-specific PDF link may require institutional access.
  // Offer it through the reader's browser session without guessing OA status.
  const scienceDirect = /(^|\.)sciencedirect\.com$/i.test(currentUrl.hostname);
  const pii = (currentUrl.pathname.match(/\/article\/pii\/([^/?#]+)/i) || [])[1] || '';
  let fulltextNote = pdfUrl ? '已识别当前论文的 PDF 入口；保存后会尝试下载，实际可用性以返回的 PDF 为准。' : '';
  if (scienceDirect && pii) {
    const articlePdf = [...document.querySelectorAll('a[href]')].find(link => {
      const url = String(link.href || '');
      const label = cleanText(link.getAttribute('aria-label') || link.textContent);
      return url.includes(`/science/article/pii/${pii}/pdfft`) && /pdf/i.test(label + ' ' + url);
    });
    if (articlePdf?.href) {
      pdfUrl = articlePdf.href;
      fulltextNote = '检测到本文的 ScienceDirect PDF 链接；浏览器会使用你的当前访问权限下载。';
    } else if (!pdfUrl) {
      fulltextNote = '页面上没有找到本文的 PDF 链接，请确认已登录或使用机构访问。';
    }
  }
  if (!pdfUrl) {
    const words = /\bpdf\b|PDF\s*下载|下载\s*PDF|全文下载|下载全文|download\s+(?:the\s+)?(?:full\s*text|article)/i;
    const button = [...document.querySelectorAll('a[href]')].find(link => {
      if (!mainArticleLink(link) || link.closest('nav, header')) return false;
      let url;
      try {url = new URL(link.href, location.href);} catch {return false;}
      if (!/^https?:$/.test(url.protocol)) return false;
      const candidateHost = url.hostname.toLowerCase();
      if (candidateHost !== host && !candidateHost.endsWith('.' + host) && !host.endsWith('.' + candidateHost)) return false;
      let candidateHref;
      try {candidateHref = decodeURIComponent(url.href);} catch {candidateHref = url.href;}
      if (doi && /10\.\d{4,9}\//i.test(candidateHref) && !doiMatches(url)) return false;
      const label = cleanText([link.textContent, link.getAttribute('title'), link.getAttribute('aria-label')].join(' '));
      return words.test(label) && !/supplement|supporting|appendix|补充材料/i.test(label + ' ' + url.pathname);
    });
    if (button) {
      pdfUrl = button.href;
      fulltextNote = '检测到 PDF 下载按钮；浏览器会使用当前网站会话尝试下载。';
    }
  }
  const arxiv = /^(?:www\.)?arxiv\.org$/i.test(new URL(location.href).hostname);
  const directPdf = document.contentType === 'application/pdf' || /\.pdf(?:$|[?#])/i.test(location.pathname) ||
    /\/(?:pdfdirect|epdf|pdfft)(?:\/|$)/i.test(location.pathname) || (arxiv && /^\/pdf\//i.test(location.pathname));
  const detectedPdfUrl = directPdf ? location.href : pdfUrl;
  const authors = values('citation_author').length ? values('citation_author') : values('dc.creator').length ? values('dc.creator') : item.authors || [];
  const year = (rawDate.match(/(?:19|20)\d{2}/) || [])[0] || item.year || '';
  const recognizedJournal = !!pdfUrl && ['link.springer.com', 'onlinelibrary.wiley.com', 'tandfonline.com',
    'journals.sagepub.com', 'pubs.acs.org', 'mdpi.com', 'frontiersin.org', 'journals.plos.org',
    'pmc.ncbi.nlm.nih.gov', 'academic.oup.com', 'cambridge.org', 'sciencedirect.com'].some(isHost);
  const type = first('citation_conference_title') ? 'paper-conference' : first('citation_dissertation_institution') ? 'thesis' : first('citation_book_title') ? 'book' :
    first('citation_journal_title') || recognizedJournal ? 'article-journal' : item.type || (directPdf || (arxiv && !!first('citation_arxiv_id')) ? 'document' : 'webpage');
  const browserDownload = !!(detectedPdfUrl || item.pdfUrl);
  return {pageUrl: item.pageUrl || location.href, type, title: first('citation_title','dc.title') || item.title || first('og:title') || document.title.replace(/\s+[|–-]\s+[^|–-]+$/, '').trim(),
    authors, year, DOI: doi,
    venue: first('citation_journal_title','citation_conference_title','prism.publicationname') || item.venue || '',
    abstract: first('citation_abstract','dc.description','description') || item.abstract || '',
    publisher: item.publisher || '', ISBN: first('citation_isbn') || item.ISBN || '',
    pdfUrl: detectedPdfUrl || item.pdfUrl || '',
    fulltextNote: browserDownload ? '已识别当前论文的 PDF 入口；浏览器会使用当前站点会话下载，完成后验证并导入。' : fulltextNote,
    tags: [], nativeDownloads: [], browserDownload, unapiServer, unapiId, risUrl: absolute(risLink?.href)};
 }
async function enrichRis(url) {
  const target = new URL(url, location.href);
  if (target.origin !== location.origin || !/^https?:$/.test(target.protocol)) return null;
  const response = await fetch(target.href, {credentials: 'same-origin'});
  if (!response.ok) return null;
  const text = await response.text();
  if (text.length > 500000 || !/^TY\s{1,2}-/m.test(text)) return null;
  const fields = {};
  for (const line of text.split(/\r?\n/)) {
    const match = /^([A-Z][A-Z0-9])\s{1,2}-\s?(.*)$/.exec(line);
    if (!match) continue;
    (fields[match[1]] ||= []).push(match[2].trim());
  }
  const first = (...keys) => keys.flatMap(key => fields[key] || [])[0] || '';
  const title = first('TI', 'T1');
  if (!title) return null;
  const kind = first('TY');
  return {title, authors: fields.AU || fields.A1 || [], year: (first('PY', 'Y1').match(/(?:19|20)\d{2}/) || [])[0] || '',
    DOI: first('DO'), venue: first('JO', 'JF', 'T2'), abstract: first('AB', 'N2'),
    type: kind === 'BOOK' ? 'book' : kind === 'THES' ? 'thesis' : kind === 'JOUR' ? 'article-journal' : 'document',
    ISBN: kind === 'BOOK' ? first('SN') : '', publisher: first('PB')};
}
async function enrichUnapi(server, id) {
  const target = new URL(server, location.href);
  if (target.origin !== location.origin || !/^https?:$/.test(target.protocol) || !id || id.length > 500) return null;
  target.searchParams.set('id', id);
  target.searchParams.set('format', 'mods');
  const response = await fetch(target.href, {credentials: 'same-origin'});
  if (!response.ok || !/xml|mods/i.test(response.headers.get('Content-Type') || '')) return null;
  const xml = await response.text();
  if (xml.length > 500000) return null;
  const doc = new DOMParser().parseFromString(xml, 'application/xml');
  if (doc.querySelector('parsererror')) return null;
  const nodes = name => [...doc.getElementsByTagNameNS('*', name)];
  const first = name => nodes(name)[0]?.textContent?.replace(/\s+/g, ' ').trim() || '';
  const title = first('title');
  if (!title) return null;
  const authors = nodes('name').map(node => [...node.getElementsByTagNameNS('*', 'namePart')]
    .map(part => part.textContent?.trim()).filter(Boolean).join(' ')).filter(Boolean);
  const doi = nodes('identifier').find(node => node.getAttribute('type')?.toLowerCase() === 'doi')?.textContent?.trim() || '';
  const hostItem = nodes('relatedItem').find(node => node.getAttribute('type') === 'host');
  const venue = hostItem?.getElementsByTagNameNS('*', 'title')[0]?.textContent?.trim() || '';
  return {title, authors, year: (first('dateIssued').match(/(?:19|20)\d{2}/) || [])[0] || '', DOI: doi,
    venue, abstract: first('abstract'), type: /book/i.test(first('genre')) ? 'book' : 'article-journal'};
}
async function enrichCnkiDetail(detailUrl) {
  // This runs in the current CNKI tab's isolated world. It uses the browser's
  // existing authenticated request context but never reads cookies or credentials.
  const cleanText = value => String(value || '').replace(/\s+/g, ' ').trim();
  const url = new URL(String(detailUrl || ''), location.href);
  const host = url.hostname.toLowerCase();
  const cnkiHost = value => /(^|\.)cnki\.(net|com\.cn)$/i.test(value || '') || /(^|[.-])cnki[.-](net|com[.-]cn)([.-]|$)/i.test(value || '');
  if (!/^https?:$/.test(url.protocol) || !cnkiHost(host)) throw Error('详情页地址不是知网地址');
  const response = await fetch(url.href, {credentials: 'include', redirect: 'follow'});
  if (!response.ok) throw Error(`知网详情页无法访问（${response.status}）`);
  const document = new DOMParser().parseFromString(await response.text(), 'text/html');
  const title = cleanText(document.querySelector('.brief h1, .wx-tit h1, #chTitle')?.textContent);
  if (!title) throw Error('知网未返回可识别的文章详情；可能需要先完成页面安全验证');
  const issue = cleanText(document.querySelector('.top-tip')?.textContent);
  const venue = cleanText(document.querySelector('.top-tip a')?.textContent).replace(/[。.\s]+$/,'');
  const downloads = [...document.querySelectorAll('a')].flatMap(link => {
    const label = cleanText(link.textContent);
    let href;
    try {href = new URL(link.getAttribute('href') || '', url.href);} catch {return [];}
    if (!/^https?:$/.test(href.protocol) || !cnkiHost(href.hostname)) return [];
    if (/PDF\s*下载|下载\s*PDF/i.test(label)) return [{format: 'pdf', url: href.href, label: '用浏览器下载 PDF（使用当前知网权限）'}];
    if (/CAJ\s*下载|整本下载|分章下载|分页下载/i.test(label)) return [{format: 'caj', url: href.href, label: `用浏览器${label}（CAJ 原件；实际为 PDF 时自动解析）`}];
    return [];
  }).filter((value, index, rows) => rows.findIndex(other => other.url === value.url) === index);
  const keywords = [...document.querySelectorAll('#catalog_KEYWORD a, #catalog_KEYWORD span, .keywords a, .keywords span, [id*="KEYWORD"] a')]
    .map(node => cleanText(node.textContent)).filter(value => value && value !== '关键词').slice(0, 8);
  return {title, authors: [...document.querySelectorAll('#authorpart a')]
    .map(author => cleanText(author.textContent).replace(/[\d,]+$/,'')).filter(Boolean),
    year: (issue.match(/(?:19|20)\d{2}/) || [])[0] || '', venue,
    abstract: cleanText(document.querySelector('#ChDivSummary, .abstract-text, .abstract')?.textContent),
    keywords: [...new Set(keywords)], downloads};
}
async function currentPage() {
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  if (!tab || !/^https?:\/\//i.test(tab.url || '')) throw Error('请打开论文网页或公开 PDF 后再保存');
  pageUrl = tab.url;
  activeTabId = tab.id;
  let result;
  try {[{result}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: extract});}
  catch {const url = new URL(tab.url), path = url.pathname;
    let contentPdf = false;
    try {const response = await fetch(tab.url, {method: 'HEAD', credentials: 'include'}); contentPdf = /application\/pdf/i.test(response.headers.get('Content-Type') || '');} catch {}
    const isPdf = contentPdf || /\.pdf(?:$|[?#])/i.test(path) || /\/(?:pdfdirect|epdf|pdfft)(?:\/|$)/i.test(path) || (/^(?:www\.)?arxiv\.org$/i.test(url.hostname) && /^\/pdf\//i.test(path));
    return {pageUrl: tab.url, type: isPdf ? 'document' : 'webpage', title: decodeURIComponent(path.split('/').pop() || tab.title || '网页文献').replace(/\.pdf$/i,''),
      authors: [], year: '', DOI: '', venue: '', abstract: '', pdfUrl: isPdf ? tab.url : '', browserDownload: isPdf};}
  if (!result || typeof result !== 'object') throw Error('未能从当前页面读取题录，请打开文章详情页后重试。');
  if (Array.isArray(result.candidates)) return result;
  pageUrl = result.pageUrl || tab.url;
  if (result.unapiServer && result.unapiId) {
    try {
      const [{result: detail}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: enrichUnapi, args: [result.unapiServer, result.unapiId]});
      if (detail) result = {...result, title: detail.title || result.title, authors: detail.authors?.length ? detail.authors : result.authors,
        year: detail.year || result.year, DOI: detail.DOI || result.DOI, venue: detail.venue || result.venue,
        abstract: detail.abstract || result.abstract, type: detail.type || result.type};
    } catch {}
  }
  if (result.risUrl) {
    try {
      const [{result: detail}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: enrichRis, args: [result.risUrl]});
      if (detail) result = {...result, title: detail.title || result.title, authors: detail.authors?.length ? detail.authors : result.authors,
        year: detail.year || result.year, DOI: detail.DOI || result.DOI, venue: detail.venue || result.venue,
        abstract: detail.abstract || result.abstract, type: detail.type || result.type,
        ISBN: detail.ISBN || result.ISBN, publisher: detail.publisher || result.publisher};
    } catch {}
  }
  if (result.cnkiDetailUrl) {
    try {
      const [{result: details}] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: enrichCnkiDetail, args: [result.cnkiDetailUrl]});
      if (details && typeof details === 'object') {
        result = {...result, title: details.title || result.title, authors: details.authors?.length ? details.authors : result.authors,
          year: details.year || result.year, venue: details.venue || result.venue, abstract: details.abstract || result.abstract,
          tags: [...new Set([...(result.tags || []), ...(details.keywords || []).map(keyword => `关键词：${keyword}`)])],
          nativeDownloads: details.downloads?.length ? details.downloads : result.nativeDownloads,
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
  element('publisher').value = data.publisher || '';
  element('isbn').value = data.ISBN || '';
  element('book-fields').hidden = element('type').value !== 'book';
  element('abstract').value = data.abstract || '';
  element('pdf').value = data.pdfUrl || '';
  element('pdf-label').textContent = data.pdfUrl ? '已识别的 PDF 入口' : 'PDF 地址';
  const fulltextNote = element('fulltext-note');
  fulltextNote.textContent = data.fulltextNote || (data.pdfUrl ? '保存后会尝试下载并验证 PDF，再建立全文索引；需要登录的链接可能无法自动下载。' : '未检测到当前论文的 PDF 入口；可留空，稍后在附件页添加。');
  fulltextNote.className = 'fulltext-note';
  syncFulltextMode(!!data.pdfUrl);
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
element('type').addEventListener('change', () => {element('book-fields').hidden = element('type').value !== 'book';});
function syncFulltextMode(hasPdf) {
  const mode = element('fulltext-mode');
  for (const option of mode.options) if (option.value !== 'none') option.disabled = !hasPdf;
  if (!hasPdf) mode.value = 'none';
  else if (mode.value === 'none') mode.value = 'browser';
}
element('pdf').addEventListener('input', () => syncFulltextMode(!!element('pdf').value.trim()));
async function collections() {
  const {collections} = await request('/collections');
  for (const id of ['collection', 'batch-collection']) {
    const select = element(id), selected = select.value;
    select.replaceChildren(new Option('我的文献',''));
    for (const item of collections) select.add(new Option(item.name, item.id));
    select.value = selected;
  }
}
function downloadInBrowser(choice, itemId, ticket, title = element('title').value.trim(), sourceUrl = pageUrl) {
  return new Promise((resolve, reject) => chrome.runtime.sendMessage({type: 'download-browser', token, ticket, itemId, pageUrl: sourceUrl, url: choice.url, format: choice.format, title}, response => {
    const error = chrome.runtime.lastError;
    if (error) return reject(Error(error.message));
    if (!response?.ok) return reject(Error(response?.error || '无法启动浏览器下载'));
    resolve(response);
  }));
}
async function showRecord() {
  element('connection').hidden = true;
  await collections();
  try {
    const data = await currentPage();
    if (data?.candidates?.length) {showBatch(data.candidates); return data;}
    element('batch').hidden = true; element('record').hidden = false;
    fill(data); element('save').disabled = false; return data;
  } catch (error) {
    element('save').disabled = true; element('connection').hidden = false;
    element('record').hidden = true; element('batch').hidden = true;
    message(error.message,'error'); return null;
  }
}
function showBatch(candidates) {
  batchRecords = candidates.slice(0, 50);
  element('record').hidden = true; element('batch').hidden = false;
  element('batch-summary').textContent = `当前页识别到 ${batchRecords.length} 篇文献。勾选需要保存的条目：`;
  const list = element('batch-items'); list.replaceChildren();
  for (const [index, record] of batchRecords.entries()) {
    const row = document.createElement('label'); row.className = 'batch-row';
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.value = String(index); checkbox.checked = record.selected !== false;
    const text = document.createElement('span'); text.textContent = record.title;
    const byline = document.createElement('small'); byline.textContent = [record.authors?.slice(0, 2).join('; '), record.year, record.venue].filter(Boolean).join(' · ');
    text.append(byline); row.append(checkbox, text); list.append(row);
  }
}
element('batch-save').addEventListener('click', async () => {
  const button = element('batch-save');
  const selected = [...element('batch-items').querySelectorAll('input:checked')].map(node => batchRecords[Number(node.value)]).filter(Boolean);
  if (!selected.length) {message('请至少勾选一篇文献', 'error'); return;}
  button.disabled = true;
  let created = 0, existing = 0, downloads = 0;
  const failures = [], downloadFailures = [];
  try {
    for (const [index, original] of selected.entries()) {
      message(`正在保存第 ${index + 1} / ${selected.length} 篇：${original.title}`);
      let record = original;
      if (record.cnkiDetailUrl) {
        try {
          const [{result: detail}] = await chrome.scripting.executeScript({target: {tabId: activeTabId}, func: enrichCnkiDetail, args: [record.cnkiDetailUrl]});
          record = {...record, title: detail.title || record.title, authors: detail.authors?.length ? detail.authors : record.authors,
            year: detail.year || record.year, venue: detail.venue || record.venue, abstract: detail.abstract || record.abstract,
            tags: [...new Set([...(record.tags || []), ...(detail.keywords || []).map(value => `关键词：${value}`)])],
            nativeDownloads: detail.downloads || []};
        } catch {}
      }
      try {
        const browserChoice = record.nativeDownloads?.find(value => value.format === 'pdf') ||
          (record.pdfUrl ? {url: record.pdfUrl, format: 'pdf'} : null);
        const response = await request('/capture', 'POST', {pageUrl: record.pageUrl, collectionId: element('batch-collection').value || null,
          pdfUrl: record.pdfUrl || '', downloadPdf: !!browserChoice, browserDownload: !!browserChoice,
          browserDownloadUrl: browserChoice?.url || '',
          data: {type: record.type || 'document', title: record.title, author: record.authors || [], year: record.year || '', DOI: record.DOI || '',
            'container-title': record.venue || '', publisher: record.publisher || '', ISBN: record.ISBN || '',
            abstract: record.abstract || '', tags: record.tags || []}});
        if (response.created) created++; else existing++;
        if (browserChoice && !response.pdfAlreadyAttached && !response.downloadSkippedOffline) {
          try {
            await downloadInBrowser(browserChoice, response.itemId, response.downloadTicket, record.title, record.pageUrl);
            downloads++;
          } catch (error) {downloadFailures.push(`${record.title}：${error.message || '下载未启动'}`);}
        }
      } catch (error) {failures.push(`${record.title}：${error.message || '保存失败'}`);}
    }
    message(`批量采集完成：新增 ${created} 篇，已存在 ${existing} 篇，启动全文下载 ${downloads} 篇` +
      `${failures.length ? `；保存失败 ${failures.length} 篇：${failures.slice(0, 2).join('；')}` : ''}` +
      `${downloadFailures.length ? `；下载未启动 ${downloadFailures.length} 篇：${downloadFailures.slice(0, 2).join('；')}` : ''}。`,
      failures.length || downloadFailures.length ? 'error' : 'success');
  } finally {button.disabled = false;}
});
async function showLastDownload() {
  const {downloadStatus} = await chrome.storage.local.get('downloadStatus');
  await chrome.action.setBadgeText({text: ''}).catch(() => {});
  if (!downloadStatus || Date.now() - downloadStatus.at > 30 * 60 * 1000) return;
  const node = element('last-download');
  node.textContent = '上次下载：' + downloadStatus.message;
  node.className = 'fulltext-note' + (downloadStatus.kind === 'success' ? ' success' : '');
  node.hidden = false;
}
async function retryBrowserImports() {
  await chrome.runtime.sendMessage({type: 'retry-downloads', token}).catch(() => {});
}
async function initialize() {
  showLastDownload().catch(() => {});
  token = (await chrome.storage.local.get('token')).token || '';
  if (token) {
    try {const data = await showRecord(); if (data?.detailEnriched) message('已自动补全知网详情页的摘要与关键词', 'success'); else if (data?.detailEnrichmentError) message(data.detailEnrichmentError, 'error'); await retryBrowserImports(); return;} catch (error) {
      if (error.status === 401 || error.status === 403) {await chrome.storage.local.remove('token'); token = '';}
      else if (!localServiceUnavailable(error)) throw error;
    }
  }
  const result = await connectOrWakeDesktop();
  token = result.token;
  await chrome.storage.local.set({token});
  const data = await showRecord();
  if (data) message(data.detailEnriched ? '已连接本机文献库，并自动补全知网详情页数据' : data.detailEnrichmentError || '已自动连接本机文献库', data.detailEnrichmentError ? 'error' : 'success');
  await retryBrowserImports();
}
chrome.runtime.onMessage.addListener(event => {
  if (event?.type === 'download-status') {
    message(event.message, event.kind === 'success' ? 'success' : 'error');
    if (event.kind !== 'success') element('connection').hidden = false;
  }
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
    const pdfUrl = element('pdf').value.trim(); if (pdfUrl && !/^https?:\/\//i.test(pdfUrl)) throw Error('PDF 地址应以 http(s) 开头');
    const selected = element('native-download').value;
    const nativeChoice = selected ? nativeDownloads[Number(selected.split(':')[1])] : null;
    const mode = element('fulltext-mode').value;
    const useBrowser = !!nativeChoice || (!!pdfUrl && mode === 'browser');
    const browserChoice = nativeChoice || (useBrowser ? {url: pdfUrl, format: 'pdf'} : null);
    const result = await request('/capture','POST',{pageUrl, collectionId: element('collection').value || null, pdfUrl,
      downloadPdf: useBrowser || mode === 'desktop', browserDownload: useBrowser,
      browserDownloadUrl: browserChoice?.url || '',
      data: {type: element('type').value, title, author: authors, year, DOI: element('doi').value.trim(),
        'container-title': element('venue').value.trim(), abstract: element('abstract').value.trim(),
        publisher: element('publisher').value.trim(), ISBN: element('isbn').value.trim(),
        tags: (element('classification').textContent || '').split(' · ').map(value => value.trim()).filter(Boolean)}});
    let browserError = '';
    if (useBrowser && browserChoice && !result.pdfAlreadyAttached && !result.downloadSkippedOffline) {
      try {await downloadInBrowser(browserChoice, result.itemId, result.downloadTicket);} catch (error) {browserError = error.message || '浏览器拒绝下载';}
    }
    message((result.created ? '已保存新文献' : '文献已存在，已补充集合和来源') +
      (browserError ? `；PDF 下载未启动：${browserError}` :
       result.pdfAlreadyAttached ? '；PDF 已在文献库中' : result.downloadSkippedOffline ? '；联网已关闭，已记录 PDF 来源但未下载' :
       useBrowser ? '；已交由浏览器下载，完成后会验证并导入全文' :
       result.downloadJobId ? '；桌面端 PDF 下载任务已开始' : result.pdfSourceId ? '；已记录 PDF 来源' : '') +
      (result.pdfWarning ? '；' + result.pdfWarning : ''), browserError || result.pdfWarning ? 'error' : 'success');
    scheduleDuplicateCheck();
  } catch (error) {message(error.message,'error');} finally {button.disabled = false;}
});
initialize().catch(error => {element('connection').hidden = false; element('record').hidden = true; message(error.message || '无法连接本机文献库', 'error');});
