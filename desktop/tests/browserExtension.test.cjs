const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const {BROWSER_ORIGIN} = require('../electron/office-bridge.cjs');
const manifest = require('../electron/browser-extension/manifest.json');

const source = fs.readFileSync(path.join(__dirname, '../electron/browser-extension/popup.js'), 'utf8');
const begin = source.indexOf('function extract() {');
const end = source.indexOf('\nasync function currentPage()', begin);
assert.ok(begin >= 0 && end > begin);

function metadata(values, href = 'https://example.org/article') {
  const nodes = values.map(([name, content]) => ({getAttribute: key => key === 'name' ? name : key === 'content' ? content : ''}));
  const url = new URL(href);
  const document = {title: 'Fallback title - Publisher', querySelectorAll: selector => selector === 'meta' ? nodes : [], querySelector: () => null};
  return vm.runInNewContext(source.slice(begin, end) + ';extract()', {
    document, location: {href, pathname: url.pathname, hostname: url.hostname}, URL,
  });
}

function cnkiRecord({detail = false, multiple = false, downloads = false} = {}) {
  const value = text => ({textContent: text});
  const row = {
    querySelector: selector => ({
      'input.cbItem:checked, input[type="checkbox"]:checked': null,
      'td.name a, td.name a.fz14': {textContent: '客户企业数字化、供应商企业ESG表现与供应链可持续发展', href: 'https://kns.cnki.net/kcms2/article/abstract?v=fixture'},
      'td.data': value('期刊'), 'td.date': value('2024-03-20'), 'td.source': value('经济研究'),
    })[selector] || null,
    querySelectorAll: selector => selector === 'td.author a' ? [value('肖红军'), value('沈洪涛'), value('周艳坤')] : [],
  };
  const document = detail ? {
    title: '客户企业数字化、供应商企业ESG表现与供应链可持续发展 - 中国知网',
    querySelectorAll: selector => selector === 'tr' ? [] : selector === '#authorpart a' ? [value('肖红军1'), value('沈洪涛2,3'), value('周艳坤4')] : selector === 'a' && downloads ? [{textContent: 'PDF下载', href: 'https://kns.cnki.net/kns8s/download/pdf?fixture=1'}, {textContent: 'CAJ下载', href: 'https://kns.cnki.net/kns8s/download/caj?fixture=1'}] : [],
    querySelector: selector => ({
      '.brief h1, .wx-tit h1, #chTitle': value('客户企业数字化、供应商企业ESG表现与供应链可持续发展'),
      '.top-tip': value('经济研究 . 2024,59 (03) : 54-73'), '.top-tip a': value('经济研究 .'),
      '#ChDivSummary, .abstract-text': value('这是摘要。'),
    })[selector] || null,
  } : {title: '检索-中国知网', querySelectorAll: selector => selector === 'tr' ? (multiple ? [row, row] : [row]) : [], querySelector: () => null};
  return vm.runInNewContext(source.slice(begin, end) + ';extract()', {
    document, location: {href: 'https://kns.cnki.net/kns8s/defaultresult/index?kw=fixture', pathname: '/kns8s/defaultresult/index', hostname: 'kns.cnki.net'}, URL,
  });
}

function cnkiDetailEnrichment() {
  const begin = source.indexOf('async function enrichCnkiDetail(');
  const end = source.indexOf('\nasync function currentPage()', begin);
  assert.ok(begin >= 0 && end > begin);
  const value = text => ({textContent: text});
  const document = {
    querySelector: selector => ({
      '.brief h1, .wx-tit h1, #chTitle': value('客户企业数字化、供应商企业ESG表现与供应链可持续发展'),
      '.top-tip': value('经济研究 . 2024,59 (03) : 54-73'), '.top-tip a': value('经济研究 .'),
      '#ChDivSummary, .abstract-text, .abstract': value('供应链可持续发展摘要。'),
    })[selector] || null,
    querySelectorAll: selector => selector === '#authorpart a' ? [value('肖红军1'), value('沈洪涛2,3'), value('周艳坤4')] :
      selector.includes('KEYWORD') ? [value('客户企业数字化'), value('供应商企业 ESG'), value('客户企业数字化')] : [],
  };
  let fetchOptions;
  const context = {URL, location: {href: 'https://kns.cnki.net/kns8s/defaultresult/index'}, DOMParser: class {parseFromString() {return document;}},
    fetch: async (url, options) => {fetchOptions = options; return {ok: true, text: async () => '<html></html>'};}};
  return {result: vm.runInNewContext(source.slice(begin, end) + ';enrichCnkiDetail("https://kns.cnki.net/kcms2/article/abstract?v=fixture")', context), options: () => fetchOptions};
}

test('extension has a stable origin and opens without a pairing form', () => {
  assert.match(BROWSER_ORIGIN, /^chrome-extension:\/\/[a-p]{32}$/);
  assert.ok(manifest.key);
  assert.match(source, /request\('\/connect', 'POST'\)/);
  assert.match(source, /researchlibrary:\/\/browser-capture/);
  assert.match(source, /connectOrWakeDesktop/);
  assert.doesNotMatch(fs.readFileSync(path.join(__dirname, '../electron/browser-extension/popup.html'), 'utf8'), /配对码|id="pair"/);
  assert.match(fs.readFileSync(path.join(__dirname, '../electron/browser-extension/popup.html'), 'utf8'), /id="download" type="checkbox" checked/);
  assert.match(source, /request\('\/duplicates', 'POST'/);
});

test('publisher citation tags produce reviewable fields and absolute PDF source', () => {
  const result = metadata([
    ['citation_title', 'Multi-workshop scheduling'],
    ['citation_author', 'Wang, Mei'], ['citation_author', 'Li, Jun'],
    ['citation_date', '2025/03/01'], ['citation_doi', 'https://doi.org/10.1000/fixture'],
    ['citation_journal_title', 'Scheduling Journal'], ['citation_pdf_url', '/files/paper.pdf'],
  ]);
  assert.equal(result.title, 'Multi-workshop scheduling');
  assert.equal(result.type, 'article-journal');
  assert.deepEqual(Array.from(result.authors), ['Wang, Mei', 'Li, Jun']);
  assert.equal(result.year, '2025');
  assert.equal(result.DOI, '10.1000/fixture');
  assert.equal(result.pdfUrl, 'https://example.org/files/paper.pdf');
});

test('direct PDF and sparse page remain editable without invented metadata', () => {
  const direct = metadata([], 'https://example.org/files/paper.pdf');
  assert.equal(direct.type, 'document');
  assert.equal(direct.pdfUrl, 'https://example.org/files/paper.pdf');
  const sparse = metadata([], 'https://example.org/article');
  assert.equal(sparse.DOI, '');
  assert.equal(sparse.year, '');
  assert.equal(sparse.title, 'Fallback title');
});

test('arXiv abstract is a preprint document with its public PDF source', () => {
  const result = metadata([
    ['citation_title', 'Attention Is All You Need'],
    ['citation_author', 'Vaswani, Ashish'],
    ['citation_date', '2017/06/12'],
    ['citation_arxiv_id', '1706.03762'],
    ['citation_pdf_url', 'https://arxiv.org/pdf/1706.03762'],
  ], 'https://arxiv.org/abs/1706.03762');
  assert.equal(result.type, 'document');
  assert.equal(result.year, '2017');
  assert.equal(result.pdfUrl, 'https://arxiv.org/pdf/1706.03762');
  assert.equal(result.DOI, '');
  const pdf = metadata([], 'https://arxiv.org/pdf/1706.03762');
  assert.equal(pdf.type, 'document');
  assert.equal(pdf.pdfUrl, 'https://arxiv.org/pdf/1706.03762');
});

test('ScienceDirect uses only the current open-access article PDF link', () => {
  const pii = 'S0360835226005450';
  const href = `https://www.sciencedirect.com/science/article/pii/${pii}`;
  const currentPdf = `${href}/pdfft?md5=current&pid=1-s2.0-${pii}-main.pdf`;
  const referencePdf = 'https://www.sciencedirect.com/science/article/pii/S0360835223004825/pdfft?md5=reference';
  const metas = [['citation_title', 'Recent developments in the Last-Mile Location-Routing problem']]
    .map(([name, content]) => ({getAttribute: key => key === 'name' ? name : key === 'content' ? content : ''}));
  const links = [
    {href: currentPdf, textContent: '', getAttribute: key => key === 'aria-label' ? 'View PDF document. Opens in new window' : ''},
    {href: referencePdf, textContent: 'View PDF', getAttribute: () => ''},
  ];
  const document = {title: 'Paper - ScienceDirect',
    querySelectorAll: selector => selector === 'meta' ? metas : selector === 'a[href]' ? links : [],
    querySelector: selector => selector === 'main' ? {textContent: 'Open access This is the main article.'} : null};
  const result = vm.runInNewContext(source.slice(begin, end) + ';extract()', {
    document, location: {href, pathname: new URL(href).pathname, hostname: 'www.sciencedirect.com'}, URL,
  });
  assert.equal(result.pdfUrl, currentPdf);
  assert.match(result.fulltextNote, /已识别 ScienceDirect 开放获取 PDF/);
});

test('ScienceDirect does not queue a publisher PDF without an open-access marker', () => {
  const href = 'https://www.sciencedirect.com/science/article/pii/S0360835226005450';
  const document = {title: 'Paper - ScienceDirect', querySelectorAll: selector => selector === 'meta' ? [] : selector === 'a[href]' ? [] : [],
    querySelector: selector => selector === 'main' ? {textContent: 'Subscription article'} : null};
  const result = vm.runInNewContext(source.slice(begin, end) + ';extract()', {
    document, location: {href, pathname: new URL(href).pathname, hostname: 'www.sciencedirect.com'}, URL,
  });
  assert.equal(result.pdfUrl, '');
  assert.match(result.fulltextNote, /未识别到可公开下载的 PDF/);
});

test('CNKI result page uses the sole or selected result instead of the search phrase', () => {
  const result = cnkiRecord();
  assert.equal(result.title, '客户企业数字化、供应商企业ESG表现与供应链可持续发展');
  assert.deepEqual(Array.from(result.authors), ['肖红军', '沈洪涛', '周艳坤']);
  assert.equal(result.year, '2024');
  assert.equal(result.venue, '经济研究');
  assert.equal(result.type, 'article-journal');
  assert.equal(result.pageUrl, 'https://kns.cnki.net/kcms2/article/abstract?v=fixture');
});

test('CNKI article detail page extracts title, authors, issue and abstract', () => {
  const result = cnkiRecord({detail: true});
  assert.deepEqual(Array.from(result.authors), ['肖红军', '沈洪涛', '周艳坤']);
  assert.equal(result.year, '2024');
  assert.equal(result.venue, '经济研究');
  assert.equal(result.abstract, '这是摘要。');
});

test('CNKI detail exposes only official native download choices and auto classification', () => {
  const result = cnkiRecord({detail: true, downloads: true});
  assert.deepEqual(Array.from(result.tags), ['来源：知网', '类型：期刊论文', '期刊：经济研究']);
  assert.deepEqual(Array.from(result.nativeDownloads).map(value => value.format), ['pdf', 'caj']);
  assert.ok(result.nativeDownloads.every(value => value.url.includes('cnki.net')));
});

test('CNKI result links are enriched through the current authenticated page context', async () => {
  const pending = cnkiDetailEnrichment();
  const result = await pending.result;
  assert.equal(result.title, '客户企业数字化、供应商企业ESG表现与供应链可持续发展');
  assert.deepEqual(Array.from(result.authors), ['肖红军', '沈洪涛', '周艳坤']);
  assert.equal(result.year, '2024');
  assert.equal(result.venue, '经济研究');
  assert.equal(result.abstract, '供应链可持续发展摘要。');
  assert.deepEqual(Array.from(result.keywords), ['客户企业数字化', '供应商企业 ESG']);
  assert.equal(pending.options().credentials, 'include');
});

test('CNKI multi-result page requires an explicit selection', () => {
  assert.equal(cnkiRecord({multiple: true}).requiresSelection, true);
});
