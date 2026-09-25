const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {DOMParser, XMLSerializer} = require('@xmldom/xmldom');

const CSL_NS = 'http://purl.org/net/xbiblio/csl';
const BRANCH = 'v1.0.2';
const TREE_URL = `https://api.github.com/repos/citation-style-language/styles/git/trees/${BRANCH}?recursive=1`;
const MAX_STYLE_BYTES = 2 * 1024 * 1024;
const MAX_CATALOG_BYTES = 8 * 1024 * 1024;
const CACHE_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const stylePath = value => typeof value === 'string' && /^(?:dependent\/)?[a-z0-9][a-z0-9-]*\.csl$/.test(value);
const styleId = value => 'remote-' + crypto.createHash('sha256').update(value).digest('hex').slice(0, 16);
const friendly = value => path.basename(value, '.csl').replace(/-/g, ' ');

function parseStyle(xml) {
  if (Buffer.byteLength(xml, 'utf8') > MAX_STYLE_BYTES || /<!DOCTYPE|<!ENTITY/i.test(xml)) throw Error('CSL 样式文件不安全或超过 2 MB');
  const document = new DOMParser().parseFromString(xml, 'text/xml');
  const style = document.documentElement;
  if (style?.localName !== 'style' || style.namespaceURI !== CSL_NS || document.getElementsByTagName('parsererror').length) {
    throw Error('下载内容不是有效的 CSL 样式');
  }
  const info = style.getElementsByTagNameNS(CSL_NS, 'info')[0];
  const name = info?.getElementsByTagNameNS(CSL_NS, 'title')[0]?.textContent?.trim();
  const parent = Array.from(info?.getElementsByTagNameNS(CSL_NS, 'link') || [])
    .find(link => link.getAttribute('rel') === 'independent-parent')?.getAttribute('href') || '';
  const independent = style.getElementsByTagNameNS(CSL_NS, 'citation').length > 0 &&
    style.getElementsByTagNameNS(CSL_NS, 'bibliography').length > 0;
  return {name, parent, independent, locale: style.getAttribute('default-locale') || ''};
}

async function fetchText(url, limit) {
  const response = await fetch(url, {headers: {'User-Agent': 'ResearchLibrary', Accept: 'application/json, application/xml, text/xml'}, signal: AbortSignal.timeout(20000)});
  if (!response.ok) throw Error(`样式库请求失败（HTTP ${response.status}）`);
  if (Number(response.headers.get('content-length') || 0) > limit) throw Error('样式库返回的数据过大');
  const text = await response.text();
  if (Buffer.byteLength(text, 'utf8') > limit) throw Error('样式库返回的数据过大');
  return text;
}

function createStyleCatalog({directory, styles, options, Cite, isOnline = async () => true}) {
  let paths = null;
  const cacheFile = path.join(directory, 'catalog-v1.0.2.json');
  function register(id, name, xml) {
    styles.add(id, xml);
    new Cite([{type: 'article-journal', title: 'Citation style check', author: [{family: 'Smith', given: 'Jane'}], issued: {'date-parts': [[2024]]}}])
      .format('bibliography', {format: 'text', template: id, lang: 'en-US'});
    const existing = options.find(option => option.id === id);
    if (existing) existing.name = name;
    else options.push({id, name});
  }
  function restore() {
    if (!fs.existsSync(directory)) return;
    for (const file of fs.readdirSync(directory).filter(name => /^remote-[a-f0-9]{16}\.json$/.test(name))) {
      try {
        const metadata = JSON.parse(fs.readFileSync(path.join(directory, file), 'utf8'));
        if (!stylePath(metadata.path) || styleId(metadata.path) + '.json' !== file) continue;
        const xml = fs.readFileSync(path.join(directory, styleId(metadata.path) + '.csl'), 'utf8');
        if (!parseStyle(xml).independent) continue;
        register(styleId(metadata.path), metadata.name || friendly(metadata.path), xml);
      } catch { /* One damaged style must not prevent the app from starting. */ }
    }
  }
  async function catalog() {
    if (paths) return paths;
    let cached;
    try {
      cached = JSON.parse(fs.readFileSync(cacheFile, 'utf8'));
      if (!Array.isArray(cached.paths) || cached.paths.length < 1000 || !cached.paths.every(stylePath)) cached = null;
    } catch {cached = null;}
    if (cached && Date.now() - cached.at < CACHE_AGE_MS) return (paths = cached.paths);
    if (!(await isOnline())) {
      if (cached) return (paths = cached.paths);
      throw Error('联网已关闭；请在设置中启用联网查询后搜索样式');
    }
    try {
      const tree = JSON.parse(await fetchText(TREE_URL, MAX_CATALOG_BYTES));
      if (tree.truncated || !Array.isArray(tree.tree)) throw Error('样式目录不完整，请稍后重试');
      const found = tree.tree.filter(entry => entry.type === 'blob' && stylePath(entry.path)).map(entry => entry.path);
      if (found.length < 1000) throw Error('样式目录不完整，请稍后重试');
      fs.mkdirSync(directory, {recursive: true});
      fs.writeFileSync(cacheFile + '.tmp', JSON.stringify({at: Date.now(), paths: found}));
      fs.renameSync(cacheFile + '.tmp', cacheFile);
      return (paths = found);
    } catch (error) {
      if (cached) return (paths = cached.paths);
      throw Error('无法连接官方 CSL 样式库：' + error.message);
    }
  }
  async function search(query) {
    const term = String(query || '').trim().toLowerCase().slice(0, 100);
    if (term.length < 2) return [];
    const words = term.split(/\s+/).filter(Boolean);
    const found = (await catalog()).filter(value => words.every(word => friendly(value).includes(word)));
    found.sort((left, right) => {
      const score = value => (friendly(value).startsWith(term) ? 0 : 1) + (value.startsWith('dependent/') ? 1 : 0);
      return score(left) - score(right) || friendly(left).localeCompare(friendly(right));
    });
    return found.slice(0, 50).map(value => ({path: value, name: friendly(value), dependent: value.startsWith('dependent/'),
      installedId: options.some(option => option.id === styleId(value)) ? styleId(value) : null}));
  }
  async function install(value) {
    if (!stylePath(value) || !(await catalog()).includes(value)) throw Error('样式不在官方 CSL 目录中');
    if (!(await isOnline())) throw Error('联网已关闭；请在设置中启用联网查询后安装样式');
    const fetchStyle = filename => fetchText(`https://raw.githubusercontent.com/citation-style-language/styles/${BRANCH}/${filename}`, MAX_STYLE_BYTES);
    const original = await fetchStyle(value);
    const metadata = parseStyle(original);
    let xml = original;
    if (!metadata.independent) {
      const parentId = metadata.parent.match(/^https?:\/\/(?:www\.)?zotero\.org\/styles\/([a-z0-9-]+)$/i)?.[1];
      const parentPath = parentId ? parentId + '.csl' : '';
      if (!stylePath(parentPath) || !(await catalog()).includes(parentPath)) throw Error('期刊样式引用的母样式不在官方目录中');
      xml = await fetchStyle(parentPath);
      if (!parseStyle(xml).independent) throw Error('期刊样式的母样式缺少引用格式');
      if (metadata.locale) {
        const parentDocument = new DOMParser().parseFromString(xml, 'text/xml');
        parentDocument.documentElement.setAttribute('default-locale', metadata.locale);
        xml = new XMLSerializer().serializeToString(parentDocument);
      }
    }
    const id = styleId(value);
    const name = metadata.name || friendly(value);
    register(id, name, xml);
    fs.mkdirSync(directory, {recursive: true});
    fs.writeFileSync(path.join(directory, id + '.csl.tmp'), xml, 'utf8');
    fs.renameSync(path.join(directory, id + '.csl.tmp'), path.join(directory, id + '.csl'));
    fs.writeFileSync(path.join(directory, id + '.json.tmp'), JSON.stringify({name, path: value, source: TREE_URL, installedAt: new Date().toISOString()}));
    fs.renameSync(path.join(directory, id + '.json.tmp'), path.join(directory, id + '.json'));
    return {id, name};
  }
  return {restore, search, install};
}

module.exports = {createStyleCatalog, parseStyle};
