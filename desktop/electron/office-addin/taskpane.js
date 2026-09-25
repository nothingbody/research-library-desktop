(() => {
  const host = document.body.dataset.host;
  const $ = selector => document.querySelector(selector);
  const state = {id: '', host, documentFingerprint: '', documentName: '', styleId: 'gb-t-7714-2015', locale: 'zh-CN', clusters: [], bibliography: {anchorKey: '', renderedHash: ''}, selected: [], editingId: null};
  let token = sessionStorage.getItem(`research-library-writer-${host}`) || '';
  let timeout;
  const newId = () => crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const escape = value => String(value || '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const citationDisplay = (cluster, citation) => cluster?.citation?.annotationText ? `${cluster.citation.annotationText} ${citation}` : citation;
  function message(value, kind = '') {const node = $('#notice'); node.textContent = value; node.className = `notice ${kind}`;}
  function connection(value, kind = '') {const node = $('#connection'); node.textContent = value; node.className = `status ${kind}`;}
  async function request(path, options = {}) {
    const headers = {'Content-Type': 'application/json', ...(options.headers || {})};
    if (token) headers['x-research-writer'] = token;
    const response = await fetch(path, {...options, headers});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw Error(data.error || '本机写作服务请求失败');
    return data;
  }
  function stateForStorage() {
    return {id: state.id, host: state.host, documentFingerprint: state.documentFingerprint, documentName: state.documentName, styleId: state.styleId,
      locale: state.locale, clusters: state.clusters, bibliography: state.bibliography};
  }
  function restore(value) {
    if (!value || typeof value !== 'object' || value.host !== host) return;
    Object.assign(state, value, {selected: [], editingId: null});
    state.clusters = Array.isArray(state.clusters) ? state.clusters : [];
    state.bibliography = state.bibliography || {anchorKey: '', renderedHash: ''};
  }
  async function officeSettingsLoad() {
    if (!window.Office || host !== 'word') return;
    await Office.onReady();
    const saved = Office.context.document.settings.get('researchLibraryCitationState');
    if (saved) restore(saved);
    state.documentName = Office.context.document.url ? Office.context.document.url.split(/[\\/]/).pop() : '未保存的 Word 文档';
    state.documentFingerprint = Office.context.document.url || `word-unsaved:${state.id || newId()}`;
  }
  async function officeSettingsSave() {
    if (!window.Office || host !== 'word') return;
    Office.context.document.settings.set('researchLibraryCitationState', stateForStorage());
    await new Promise((resolve, reject) => Office.context.document.settings.saveAsync(result => result.status === Office.AsyncResultStatus.Succeeded ? resolve() : reject(Error(result.error?.message || '无法保存文档内的引文状态'))));
  }
  function wpsApplication() {return window.wps?.WpsApplication?.() || window.wps?.Application || null;}
  async function wpsSettingsLoad() {
    const app = wpsApplication();
    if (!app) return;
    try {
      const document = app.ActiveDocument;
      const raw = document.CustomDocumentProperties?.Item?.('ResearchLibraryCitationState')?.Value;
      if (raw) restore(JSON.parse(raw));
      state.documentName = document.Name || '未保存的 WPS 文档';
      state.documentFingerprint = document.FullName || `wps-unsaved:${state.id || newId()}`;
    } catch {message('已连接 WPS，但当前版本未开放文档状态接口；请先保存文档并在“刷新全文”后确认。');}
  }
  async function wpsSettingsSave() {
    const app = wpsApplication();
    if (!app) return;
    try {
      const props = app.ActiveDocument.CustomDocumentProperties;
      try {props.Item('ResearchLibraryCitationState').Value = JSON.stringify(stateForStorage());}
      catch {props.Add('ResearchLibraryCitationState', false, 4, JSON.stringify(stateForStorage()));}
    } catch {throw Error('当前 WPS 版本无法保存动态引文状态');}
  }
  async function initializeDocument() {
    if (host === 'word') await officeSettingsLoad(); else await wpsSettingsLoad();
    if (!state.id) state.id = `session-${newId()}`;
    if (!state.documentFingerprint) state.documentFingerprint = `${host}-unsaved:${state.id}`;
    if (!state.documentName) state.documentName = host === 'word' ? '未保存的 Word 文档' : '未保存的 WPS 文档';
    $('#document-name').textContent = state.documentName;
  }
  async function saveSession(eventType = 'save') {
    if (host === 'word') await officeSettingsSave(); else await wpsSettingsSave();
    const payload = {...stateForStorage(), state: {version: 1, storage: host === 'word' ? 'office-document-settings' : 'wps-custom-properties'}, eventType};
    await request('/api/session', {method: 'POST', body: JSON.stringify(payload)});
  }
  async function putWordCitation(cluster, text) {
    await Word.run(async context => {
      const range = context.document.getSelection();
      range.insertText(text, 'Replace');
      const control = range.insertContentControl();
      control.tag = cluster.anchorKey;
      control.title = '文献工作台动态引文';
      control.appearance = 'BoundingBox';
      await context.sync();
    });
  }
  async function putWpsCitation(cluster, text) {
    const app = wpsApplication();
    if (!app) throw Error('未检测到 WPS 加载项接口');
    const range = app.Selection?.Range;
    if (!range) throw Error('WPS 中没有可用的插入位置');
    range.Text = text;
    try {app.ActiveDocument.Bookmarks.Add(cluster.anchorKey, range);} catch {message('WPS 已插入引文，但当前版本未支持书签锚点，刷新前请先保存并验证。');}
  }
  async function updateWordDocument(rendered, includeBibliography = false) {
    await Word.run(async context => {
      const controls = context.document.contentControls;
      controls.load('items/tag');
      await context.sync();
      const byTag = new Map(controls.items.map(control => [control.tag, control]));
      for (const item of rendered.citations) {
        const cluster = state.clusters.find(value => value.id === item.id);
        const control = cluster && byTag.get(cluster.anchorKey);
        if (control) control.insertText(citationDisplay(cluster, item.text), 'Replace');
      }
      if (includeBibliography) {
        const bibliography = state.bibliography.anchorKey && byTag.get(state.bibliography.anchorKey);
        if (bibliography) bibliography.insertText(rendered.bibliography.text, 'Replace');
      }
      await context.sync();
    });
  }
  async function updateWpsDocument(rendered, includeBibliography = false) {
    const app = wpsApplication();
    if (!app) throw Error('未检测到 WPS 加载项接口');
    for (const item of rendered.citations) {
      const cluster = state.clusters.find(value => value.id === item.id);
      try {const mark = app.ActiveDocument.Bookmarks.Item(cluster.anchorKey); mark.Range.Text = item.text; app.ActiveDocument.Bookmarks.Add(cluster.anchorKey, mark.Range);} catch { /* Missing anchors are surfaced by the refresh message. */ }
    }
    if (includeBibliography && state.bibliography.anchorKey) {
      try {app.ActiveDocument.Bookmarks.Item(state.bibliography.anchorKey).Range.Text = rendered.bibliography.text;} catch { /* no bibliography anchor yet */ }
    }
  }
  async function rendered() {
    const result = await request('/api/render', {method: 'POST', body: JSON.stringify({clusters: state.clusters, styleId: state.styleId, locale: state.locale})});
    for (const value of result.citations) {const cluster = state.clusters.find(item => item.id === value.id); if (cluster) cluster.renderedText = value.text;}
    state.bibliography.renderedHash = await digest(result.bibliography.text);
    return result;
  }
  async function digest(value) {
    const bytes = new TextEncoder().encode(value);
    const hash = await crypto.subtle.digest('SHA-256', bytes);
    return [...new Uint8Array(hash)].map(byte => byte.toString(16).padStart(2, '0')).join('');
  }
  function renderResults(items) {
    $('#results').innerHTML = items.length ? items.map(item => `<button class="result ${state.selected.includes(item.id) ? 'active' : ''}" data-id="${item.id}"><strong>${escape(item.title)}</strong><small>${escape(item.authorText || '作者未知')} · ${escape(item.year || '年份未知')} · ${escape(item.citationKey || '')}</small></button>`).join('') : '<div class="muted">输入关键词后搜索本地文献。</div>';
    $('#results').querySelectorAll('button').forEach(button => button.addEventListener('click', () => {
      const itemId = button.dataset.id; state.selected = state.selected.includes(itemId) ? state.selected.filter(value => value !== itemId) : [...state.selected, itemId];
      search();
    }));
  }
  function renderSelected(items) {$('#selected').innerHTML = items.map(item => `<span title="${escape(item.title)}">${escape(item.title)}</span>`).join('');}
  let annotationRequest = 0;
  async function loadAnnotations() {
    const requestId = ++annotationRequest;
    const panel = $('#annotations');
    if (state.selected.length !== 1) {panel.textContent = state.selected.length ? '只选一篇文献即可查看批注' : '选择一篇文献后查看批注'; return;}
    panel.textContent = '正在读取批注…';
    try {
      const result = await request(`/api/annotations?itemId=${encodeURIComponent(state.selected[0])}`);
      if (requestId !== annotationRequest) return;
      const annotations = (result.items || []).filter(item => item.quote || item.comment);
      panel.innerHTML = annotations.length ? annotations.map((item, index) => `<article class="annotation ${item.stale ? 'stale' : ''}"><small>第 ${escape(item.page)} 页${item.stale ? ' · PDF 已变更，需重新核对' : ''}</small>${item.quote ? `<p class="quote">${escape(item.quote)}</p>` : ''}${item.comment ? `<p class="comment">批注：${escape(item.comment)}</p>` : ''}<button data-index="${index}" ${item.stale ? 'disabled' : ''}>${item.quote ? '插入摘录＋引文' : '插入批注＋引文'}</button></article>`).join('') : '<div class="muted">这篇文献暂无可引用的批注。</div>';
      panel.querySelectorAll('button[data-index]').forEach(button => button.addEventListener('click', () => insertCitation(annotations[Number(button.dataset.index)])));
    } catch (error) {if (requestId === annotationRequest) panel.textContent = error.message;}
  }
  let lastResults = [];
  async function search() {
    try {
      const q = $('#query').value.trim();
      if (!q) {lastResults = []; renderResults([]); renderSelected([]); loadAnnotations(); return;}
      const result = await request(`/api/items?q=${encodeURIComponent(q)}`);
      lastResults = result.items; renderResults(lastResults); renderSelected(lastResults.filter(item => state.selected.includes(item.id))); loadAnnotations();
    } catch (error) {message(error.message, 'error');}
  }
  function citationItems() {
    return state.selected.map(itemId => ({itemId, locator: $('#locator').value.trim(), label: 'page', prefix: $('#prefix').value.trim(), suffix: $('#suffix').value.trim(), suppressAuthor: $('#suppress-author').checked}));
  }
  async function insertCitation(annotation = null) {
    try {
      if (annotation) {
        if (annotation.stale) throw Error('PDF 已变更，请在阅读器中核对批注后再引用');
        state.selected = [annotation.itemId];
        $('#locator').value = String(annotation.page || '');
      }
      if (!state.selected.length) throw Error('请先选择至少一篇文献');
      const existing = state.editingId && state.clusters.find(cluster => cluster.id === state.editingId);
      const cluster = existing || {id: `cluster-${newId()}`, anchorKey: '', ordinal: state.clusters.length};
      cluster.anchorKey ||= `research-library:citation:${cluster.id}`;
      cluster.items = citationItems();
      cluster.citation = {...(cluster.citation || {}), properties: {noteIndex: cluster.ordinal + 1}};
      if (annotation) {
        cluster.citation.annotationId = annotation.id;
        cluster.citation.annotationItemId = annotation.itemId;
        cluster.citation.annotationText = annotation.quote ? `“${String(annotation.quote).slice(0, 5000)}”` : `［研究者批注］${String(annotation.comment).slice(0, 5000)}`;
      } else if (cluster.citation.annotationItemId && (state.selected.length !== 1 || state.selected[0] !== cluster.citation.annotationItemId)) {
        delete cluster.citation.annotationId;
        delete cluster.citation.annotationItemId;
        delete cluster.citation.annotationText;
      }
      if (!existing) state.clusters.push(cluster);
      const result = await rendered();
      const current = result.citations.find(item => item.id === cluster.id);
      if (existing) {
        if (host === 'word') await updateWordDocument(result, false); else await updateWpsDocument(result, false);
      } else if (host === 'word') await putWordCitation(cluster, citationDisplay(cluster, current.text)); else await putWpsCitation(cluster, current.text);
      await saveSession(existing ? 'edit-citation' : 'insert-citation');
      state.selected = []; state.editingId = null; $('#locator').value = ''; $('#prefix').value = ''; $('#suffix').value = ''; $('#suppress-author').checked = false;
      await search(); message(existing ? '已更新当前位置引文。' : annotation ? '已插入批注摘录和动态引文。' : '已插入动态引文。', '');
    } catch (error) {message(error.message, 'error');}
  }
  async function editCurrent() {
    try {
      let tag = '';
      if (host === 'word') await Word.run(async context => {const control = context.document.getSelection().parentContentControlOrNullObject; control.load('tag'); await context.sync(); tag = control.isNullObject ? '' : control.tag;});
      else {const app = wpsApplication(); tag = app?.Selection?.Range?.Bookmarks?.Item?.(1)?.Name || '';}
      const cluster = state.clusters.find(value => value.anchorKey === tag);
      if (!cluster) throw Error('请将光标置于文献工作台插入的动态引文中');
      state.editingId = cluster.id; state.selected = cluster.items.map(item => item.itemId);
      $('#locator').value = cluster.items[0]?.locator || ''; $('#prefix').value = cluster.items[0]?.prefix || ''; $('#suffix').value = cluster.items[0]?.suffix || ''; $('#suppress-author').checked = !!cluster.items[0]?.suppressAuthor;
      $('#insert').textContent = '保存引文修改'; await search(); message('已载入当前引文。修改后点击“保存引文修改”。');
    } catch (error) {message(error.message, 'error');}
  }
  async function bibliography() {
    try {
      if (!state.clusters.length) throw Error('请先插入至少一处引文');
      const result = await rendered();
      state.bibliography.anchorKey ||= 'research-library:bibliography';
      if (host === 'word') await Word.run(async context => {
        const controls = context.document.contentControls; controls.load('items/tag'); await context.sync();
        const existing = controls.items.find(control => control.tag === state.bibliography.anchorKey);
        if (existing) existing.insertText(result.bibliography.text, 'Replace');
        else {const range = context.document.getSelection(); range.insertText(result.bibliography.text, 'Replace'); const control = range.insertContentControl(); control.tag = state.bibliography.anchorKey; control.title = '文献工作台参考文献';}
        await context.sync();
      });
      else {const app = wpsApplication(); if (!app) throw Error('未检测到 WPS 加载项接口'); const range = app.Selection.Range; range.Text = result.bibliography.text; try {app.ActiveDocument.Bookmarks.Add(state.bibliography.anchorKey, range);} catch {}}
      await saveSession('insert-bibliography'); message('已插入或更新参考文献表。');
    } catch (error) {message(error.message, 'error');}
  }
  async function refreshAll() {
    try {
      if (!state.clusters.length) throw Error('当前文档没有可刷新的动态引文');
      const result = await rendered();
      if (host === 'word') await updateWordDocument(result, true); else await updateWpsDocument(result, true);
      await saveSession('refresh'); message('已刷新正文引文和参考文献。');
    } catch (error) {message(error.message, 'error');}
  }
  async function connect() {
    try {
      const code = $('#pair-code').value.trim();
      if (!/^\d{6}$/.test(code)) throw Error('请输入六位配对码');
      const paired = await request('/api/pair', {method: 'POST', body: JSON.stringify({host, code})});
      token = paired.token; sessionStorage.setItem(`research-library-writer-${host}`, token);
      await bootWriter();
    } catch (error) {message(error.message, 'error'); connection('未连接', 'error');}
  }
  async function bootWriter() {
    try {
      const info = await request('/api/status');
      $('#style').innerHTML = info.styles.map(style => `<option value="${escape(style.id)}">${escape(style.name)}</option>`).join('');
      await initializeDocument(); $('#style').value = state.styleId; $('#pairing').hidden = true; $('#writer').hidden = false;
      connection(host === 'word' && window.Office ? 'Word 已连接' : host === 'wps' && wpsApplication() ? 'WPS 已连接' : '本机库已连接', 'ok');
      await saveSession('open');
    } catch (error) {message(error.message, 'error'); connection('连接失败', 'error');}
  }
  $('#pair').addEventListener('click', connect);
  $('#query').addEventListener('input', () => {clearTimeout(timeout); timeout = setTimeout(search, 180);});
  $('#insert').addEventListener('click', async () => {await insertCitation(); $('#insert').textContent = '插入引文';});
  $('#edit').addEventListener('click', editCurrent); $('#bibliography').addEventListener('click', bibliography); $('#refresh').addEventListener('click', refreshAll);
  $('#style').addEventListener('change', () => {state.styleId = $('#style').value;});
  if (token) bootWriter().catch(() => {});
})();
