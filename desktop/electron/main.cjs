const {app, BrowserWindow, ipcMain, dialog, protocol, shell, clipboard, Menu, safeStorage} = require('electron');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const {Readable} = require('node:stream');
const mainLog = require('./main-log.cjs');
const {startOfficeBridge, PORT: OFFICE_BRIDGE_PORT} = require('./office-bridge.cjs');
const {Cite, plugins} = require('@citation-js/core');
require('@citation-js/plugin-csl');
require('@citation-js/plugin-bibtex');
const csl = plugins.config.get('@csl');
csl.locales.add('zh-CN', fs.readFileSync(path.join(__dirname,'csl/locales-zh-CN.xml'),'utf8'));
csl.styles.add('gb-t-7714-2015', fs.readFileSync(path.join(__dirname,'csl/gb-t-7714-2015-numeric.csl'),'utf8'));
const styleOptions = [{id:'apa',name:'APA'},{id:'vancouver',name:'Vancouver'},{id:'harvard1',name:'Harvard'},{id:'gb-t-7714-2015',name:'GB/T 7714—2015（顺序编码）'}];

protocol.registerSchemesAsPrivileged([{scheme: 'app', privileges: {standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true}}]);
const project = process.env.RESEARCH_QA_PROJECT || path.resolve(__dirname, '../..');
const smoke = process.argv.includes('--smoke');
const launcherScheme = 'researchlibrary';
if (smoke) app.commandLine.appendSwitch('force-device-scale-factor', '1');
if (smoke && process.env.RESEARCH_QA_OUT) {const qaProfile=path.join(process.env.RESEARCH_QA_OUT, 'profile'); fs.mkdirSync(qaProfile,{recursive:true}); app.setPath('userData',qaProfile);}
mainLog.install(app.getPath('userData'), {captureConsole: !smoke});
let win, child, closing = false, sequence = 0, readyPromise, root, configPath, config = {}, officeBridge;
const pending = new Map();
const allowed = new Set(('app.info library.stats library.reindex items.list items.get items.create items.update items.bulk items.duplicates items.merge items.undoMerge collections.list collections.edit notes.list notes.save notes.history notes.delete attachments.get attachments.position attachments.download attachments.setRole annotations.list annotations.save annotations.delete annotations.excerpt annotations.search annotations.exportText reading.get reading.save reading.activity terms.list terms.save terms.delete assistant.status assistant.settings assistant.test assistant.run assistant.runs assistant.translation.page assistant.apply aiSearch.create aiSearch.list aiSearch.get aiSearch.savePlan aiSearch.run aiSearch.expand aiSearch.citationExpand aiSearch.citationLinks aiSearch.rerank aiSearch.journalMatch aiSearch.results aiSearch.evidence aiSearch.intro aiSearch.import aiSearch.cancel aiSearch.verify aiSearch.decision searchEvaluation.report searchEvaluation.save relations.profile.get relations.profile.run relations.create relations.list relations.get relations.diff relations.history relations.run relations.results relations.evidence relations.confirm relations.export relations.forItem relations.discover relations.discoveries relations.discoveryProgress relations.discoveryDecide relations.manualList relations.manualAdd relations.manualRemove relations.graphExport imports.commit jobs.list jobs.action journals.stats journals.list journals.get journals.catalog journals.link journals.related journals.ensure journals.index collector.control settings.get settings.save browser.status browser.importDownloaded writing.sessions writing.session writing.session.save writing.event comparison.list comparison.save metadata.lookup metadata.apply export.text fulltext.sources fulltext.add fulltext.obtain researchAsk.create researchAsk.list researchAsk.get researchAsk.send researchAsk.saveClaim projects.create projects.list projects.get projects.archive projects.link smartCollections.save smartCollections.list smartCollections.results').split(' '));

function focusWindow() {
  if (!win || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function writerHosts() {
  const programFiles = process.env.ProgramFiles || 'C:\\Program Files';
  const word = path.join(programFiles, 'Microsoft Office', 'root', 'Office16', 'WINWORD.EXE');
  return {word: {installed: fs.existsSync(word), path: fs.existsSync(word) ? word : null}};
}

function assistantSecretPath() {return path.join(app.getPath('userData'), 'assistant-key.bin');}
function assistantSecretStatus() {const encrypted = safeStorage.isEncryptionAvailable(); return {hasKey: encrypted && fs.existsSync(assistantSecretPath()), encrypted};}
function readAssistantSecret() {
  if (!safeStorage.isEncryptionAvailable() || !fs.existsSync(assistantSecretPath())) return null;
  try {const value = safeStorage.decryptString(fs.readFileSync(assistantSecretPath())); return value.trim() || null;} catch {return null;}
}
function saveAssistantSecret(value) {
  const key = String(value || '').trim();
  if (!safeStorage.isEncryptionAvailable()) throw new Error('当前系统无法提供加密凭据存储，未保存访问密钥');
  if (!key || key.length > 4096) throw new Error('访问密钥格式不正确');
  const target = assistantSecretPath(), temporary = target + '.tmp';
  fs.writeFileSync(temporary, safeStorage.encryptString(key)); fs.renameSync(temporary, target);
}
async function activateAssistantSecret() {
  const key = readAssistantSecret();
  return key ? rpc('assistant.configure', {apiKey: key}) : rpc('assistant.clear', {});
}

function rpc(method, params = {}) {
  if (!child || child.exitCode !== null || !child.stdin.writable) return Promise.reject(new Error('本地服务未连接，请重启应用'));
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {pending.delete(id); reject(new Error('操作超时，请在任务中心检查结果后再试'));}, 180000);
    pending.set(id, {resolve, reject, timer});
    try {
      child.stdin.write(JSON.stringify({id, method, params}) + '\n', error => {
        if (error && pending.has(id)) {
          clearTimeout(timer); pending.delete(id);
          reject(new Error('本地服务连接已断开，请重启应用'));
          mainLog.write('backend pipe', error);
        }
      });
    } catch (error) {
      clearTimeout(timer); pending.delete(id); reject(error);
    }
  });
}
function startBackend() {
  const executable = app.isPackaged ? path.join(process.resourcesPath, 'backend/research-backend.exe') : path.join(project, '.venv-client/Scripts/python.exe');
  const args = app.isPackaged ? [] : ['-X', 'utf8', '-m', 'client_backend.service'];
  args.push('--library', root);
  const journals = process.env.RESEARCH_JOURNALS || path.join(project, 'data/scholay');
  if (fs.existsSync(path.join(journals, 'journals.sqlite3'))) args.push('--journals', journals);
  let resolveReady, rejectReady;
  readyPromise = new Promise((resolve, reject) => {resolveReady = resolve; rejectReady = reject;});
  const backendEnv = {...process.env, PYTHONIOENCODING: 'utf-8'};
  delete backendEnv.RESEARCH_QA_API_KEY;
  child = spawn(executable, args, {cwd: app.isPackaged ? path.dirname(executable) : project, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], env: backendEnv});
  const errorLog = fs.createWriteStream(path.join(app.getPath('userData'), 'backend.log'), {flags: 'a'});
  child.stderr.pipe(errorLog);
  child.stdin.on('error', error => mainLog.write('backend pipe', error));
  child.on('error', error => rejectReady(error));
  readline.createInterface({input: child.stdout}).on('line', line => {
    let message;
    try {message = JSON.parse(line);} catch {return;}
    if (message.event === 'ready') resolveReady();
    if (message.event && win && !win.isDestroyed()) win.webContents.send('backend-event', message.event, message.data);
    const task = pending.get(message.id);
    if (task) {
      clearTimeout(task.timer); pending.delete(message.id);
      message.error ? task.reject(Object.assign(new Error(message.error.message), message.error)) : task.resolve(message.result);
    }
  });
  child.on('exit', code => {
    rejectReady(new Error('本地服务启动失败，请检查日志：' + path.join(app.getPath('userData'), 'backend.log')));
    for (const task of pending.values()) {clearTimeout(task.timer); task.reject(new Error('本地服务已退出'));}
    pending.clear();
    if (!closing && win && !win.isDestroyed()) win.webContents.send('backend-event', 'backend.error', {message: '本地服务退出（' + code + '），请重启应用。已保存的数据仍保留。'});
    errorLog.end();
  });
  return readyPromise;
}
function trusted(event) {
  if (!event.senderFrame || !event.senderFrame.url.startsWith('app://local/')) throw new Error('拒绝非应用页面访问');
}
function saveConfig() {
  fs.writeFileSync(configPath + '.tmp', JSON.stringify(config));
  fs.renameSync(configPath + '.tmp', configPath);
}
async function chooseDirectory(title) {
  const result = await dialog.showOpenDialog(win, {title, properties: ['openDirectory', 'createDirectory']});
  return result.canceled ? null : result.filePaths[0];
}
const filters = [{name: '文献与 PDF', extensions: ['pdf', 'ris', 'bib', 'bibtex', 'json']}, {name: '所有文件', extensions: ['*']}];
async function fileAction(kind, p) {
  if (kind === 'citationStyle') {
    const picked = await dialog.showOpenDialog(win, {title:'导入独立 CSL 样式',properties:['openFile'],filters:[{name:'CSL 样式',extensions:['csl']}]});
    if (picked.canceled) return null;
    if (fs.statSync(picked.filePaths[0]).size>2*1024*1024) throw new Error('样式文件超过2MB');
    const xml=fs.readFileSync(picked.filePaths[0],'utf8');
    if(!/<style\b/.test(xml)||!/<bibliography\b/.test(xml)) throw new Error('请选择包含 bibliography 的独立 CSL 样式');
    const id='custom-'+require('node:crypto').createHash('sha256').update(xml).digest('hex').slice(0,16);
    csl.styles.add(id,xml);
    const target=path.join(app.getPath('userData'),'csl');fs.mkdirSync(target,{recursive:true});fs.writeFileSync(path.join(target,id+'.csl'),xml,'utf8');
    const name=(/<title>([^<]+)<\/title>/.exec(xml)||[])[1]||path.basename(picked.filePaths[0]);
    if(!styleOptions.some(s=>s.id===id)) styleOptions.push({id,name});
    return {id,name};
  }
  if (kind === 'writingAddin') {
    const target = app.isPackaged ? path.join(process.resourcesPath, 'word-addin', 'word-manifest.xml') : path.join(__dirname, 'office-addin', 'word-manifest.xml');
    if (!fs.existsSync(target)) throw new Error('Word 加载项清单未安装，请重新安装文献工作台');
    shell.showItemInFolder(target);
    return {path: target, port: OFFICE_BRIDGE_PORT};
  }
  if (kind === 'connectCollector') return require('./collector.cjs').connect({app, parent:win, rpc, project});
  if (kind === 'import' || kind === 'attach') {
    const picked = await dialog.showOpenDialog(win, {title: kind === 'attach' ? '添加附件' : '导入文献', properties: ['openFile', 'multiSelections'], filters});
    return picked.canceled ? null : rpc('_imports.preview', {paths: picked.filePaths, itemId: kind === 'attach' ? p.itemId : null});
  }
  if (kind === 'relink') {
    const picked = await dialog.showOpenDialog(win, {title: '重新定位附件', properties: ['openFile']});
    return picked.canceled ? null : rpc('_attachments.relink', {id: p.id, path: picked.filePaths[0]});
  }
  if (kind === 'openAttachment') {
    const value = await rpc('_attachments.path', {id: p.id});
    if (!/\.(pdf|txt|md|png|jpe?g|gif|csv|docx?|xlsx?|pptx?)$/i.test(value.name)) throw new Error('此文件类型请使用“显示所在位置”后手动打开');
    return shell.openPath(value.path);
  }
  if (kind === 'revealAttachment') {const value = await rpc('_attachments.path', {id: p.id}); shell.showItemInFolder(value.path); return true;}
  if (kind === 'export' || kind === 'annotatedPDF' || kind === 'noteExport' || kind === 'annotationExport' || kind === 'graphExport' || kind === 'graphImageExport') {
    const ext = kind === 'annotatedPDF' ? 'pdf' : kind === 'graphImageExport' ? 'svg' : kind === 'graphExport' ? 'json' : kind === 'annotationExport' ? (p.format === 'json' ? 'json' : 'md') : kind === 'noteExport' ? (p.format === 'html' ? 'html' : 'md') : {bibtex: 'bib', biblatex: 'bib', ris: 'ris', json: 'json'}[p.format];
    if (!ext) throw new Error('导出格式不支持');
    const picked = smoke && kind === 'graphImageExport' && process.env.RESEARCH_QA_OUT
      ? {canceled: false, filePath: path.join(process.env.RESEARCH_QA_OUT, 'relation-graph.svg')}
      : await dialog.showSaveDialog(win, {title: '导出副本', defaultPath: (kind === 'annotatedPDF' ? '批注文献' : kind === 'graphExport' || kind === 'graphImageExport' ? '文献关联图谱' : kind === 'annotationExport' ? '文献批注汇总' : kind === 'noteExport' ? '阅读笔记' : '参考文献') + '.' + ext, filters: [{name: ext.toUpperCase(), extensions: [ext]}]});
    if (picked.canceled) return null;
    if (kind === 'annotatedPDF') return rpc('_attachments.export', {id: p.id, path: picked.filePath});
    const text = kind === 'export' ? await rpc('export.text', p) : String(p.text || '');
    if (Buffer.byteLength(text) > 64 * 1024 * 1024) throw new Error('导出内容过大');
    if (path.resolve(picked.filePath).startsWith(path.resolve(root) + path.sep)) throw new Error('请导出到文献库目录以外');
    fs.writeFileSync(picked.filePath + '.tmp', text, 'utf8'); fs.renameSync(picked.filePath + '.tmp', picked.filePath);
    return {path: picked.filePath};
  }
  if (kind === 'backupLocation') {const directory = await chooseDirectory('选择自动备份目录'); return directory ? rpc('settings.save', {backupDirectory:directory,autoBackup:true}) : null;}
  if (kind === 'backup') {const directory = await chooseDirectory('选择备份保存目录'); return directory ? rpc('_backup.create', {directory}) : null;}
  if (kind === 'verifyBackup' || kind === 'restore') {
    const backup = await chooseDirectory('选择包含 manifest.json 的完整备份目录');
    if (!backup) return null;
    const verified = await rpc('_backup.verify', {path: backup});
    if (kind === 'verifyBackup') return verified;
    const directory = await chooseDirectory('选择恢复目标父目录（将新建文献库）');
    return directory ? rpc('_backup.restore', {backup, directory}) : null;
  }
  if (kind === 'journalRoot') {const directory = await chooseDirectory('选择包含 journals.sqlite3 的期刊数据目录'); if (!directory) return null; if (!fs.existsSync(path.join(directory, 'journals.sqlite3'))) throw new Error('该目录中没有 journals.sqlite3'); return rpc('settings.save', {journalRoot: directory});}
  if (kind === 'libraryRoot') {
    const directory = await chooseDirectory('选择文献库目录（可选空目录建立新库）');
    if (!directory) return null;
    const entries = fs.readdirSync(directory);
    if (entries.length && !entries.includes('library.sqlite3')) throw new Error('请选择空目录或已有文献库目录');
    config.libraryRoot = directory; saveConfig(); delete process.env.RESEARCH_LIBRARY; app.relaunch(); app.quit(); return true;
  }
  if (kind === 'revealLibrary') {await shell.openPath(root); return true;}
  if (kind === 'revealBrowserExtension') {await shell.openPath(app.isPackaged ? path.join(process.resourcesPath, 'browser-extension') : path.join(__dirname, 'browser-extension')); return true;}
  throw new Error('文件操作不支持');
}
async function serve(request) {
  const url = new URL(request.url);
  if (url.hostname !== 'local') return new Response('Not found', {status: 404});
  let file, mime;
  if (url.pathname.startsWith('/attachment/')) {
    const id = url.pathname.split('/')[2];
    if (!/^[a-f0-9-]{36}$/.test(id)) return new Response('Not found', {status: 404});
    try {const record = await rpc('_attachments.path', {id}); if (record.mime !== 'application/pdf') return new Response('Unsupported', {status: 415}); file = record.path; mime = record.mime;} catch (e) {return new Response(e.message, {status: 404});}
  } else {
    const base = path.resolve(__dirname, '../dist');
    file = path.resolve(base, '.' + decodeURIComponent(url.pathname === '/' ? '/index.html' : url.pathname));
    if (!file.startsWith(base + path.sep)) return new Response('Forbidden', {status: 403});
    mime = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.wasm': 'application/wasm', '.bcmap': 'application/octet-stream'}[path.extname(file)] || 'application/octet-stream';
  }
  try {
    const size = fs.statSync(file).size;
    let start = 0, end = size - 1, status = 200;
    const headers = {'Content-Type': mime, 'Accept-Ranges': 'bytes', 'X-Content-Type-Options': 'nosniff'};
    const range = request.headers.get('range');
    if (range) {
      const match = /^bytes=(\d+)-(\d*)$/.exec(range);
      if (!match) return new Response(null, {status: 416, headers: {'Content-Range': `bytes */${size}`}});
      start = Number(match[1]); end = match[2] ? Math.min(Number(match[2]), end) : end;
      if (start > end || start >= size) return new Response(null, {status: 416, headers: {'Content-Range': `bytes */${size}`}});
      headers['Content-Range'] = `bytes ${start}-${end}/${size}`; status = 206;
    }
    headers['Content-Length'] = String(end - start + 1);
    const body = request.method === 'HEAD' ? null : fs.createReadStream(file, {start, end});
    if (body) body.on('error', error => mainLog.write('resource stream', request.url, error));
    return new Response(body ? Readable.toWeb(body) : null, {status, headers});
  } catch {return new Response('File not found', {status: 404});}
}

async function init() {
  fs.mkdirSync(app.getPath('userData'), {recursive: true});
  configPath = path.join(app.getPath('userData'), 'library-config.json');
  try {config = JSON.parse(fs.readFileSync(configPath, 'utf8'));} catch {}
  root = process.env.RESEARCH_LIBRARY || config.libraryRoot || (smoke && process.env.RESEARCH_QA_OUT ? path.join(process.env.RESEARCH_QA_OUT, 'library') : app.isPackaged ? path.join(app.getPath('documents'), '文献工作台') : path.join(project, 'library'));
  if (!smoke && process.env.RESEARCH_LIBRARY) {config.libraryRoot = root; saveConfig();}
  await startBackend();
  await activateAssistantSecret();
  const customStyles=path.join(app.getPath('userData'),'csl');
  if(fs.existsSync(customStyles)) for(const name of fs.readdirSync(customStyles).filter(n=>/^custom-[a-f0-9]{16}\.csl$/.test(n))) {
    try {const xml=fs.readFileSync(path.join(customStyles,name),'utf8'), id=name.slice(0,-4); csl.styles.add(id,xml);styleOptions.push({id,name:(/<title>([^<]+)<\/title>/.exec(xml)||[])[1]||id});} catch {}
  }
  try {
    officeBridge = await startOfficeBridge({
      rpc,
      styles: () => styleOptions,
      styleXml: id => csl.styles.get(id),
      localeXml: language => csl.locales.get(language) || csl.locales.get('en-US'),
      hostStatus: writerHosts,
      onEvent: (event, data) => {if (win && !win.isDestroyed()) win.webContents.send('backend-event', event, data);},
    });
  } catch (error) {
    const bridgeError = error.message || '本机写作服务未启动';
    officeBridge = {manifestPath: path.join(__dirname, 'office-addin', 'word-manifest.xml'), rotatePairing: () => null,
      browserStatus: () => ({running: false, port: OFFICE_BRIDGE_PORT, error: bridgeError}),
      status: () => ({running: false, port: OFFICE_BRIDGE_PORT, error: bridgeError, pairingCode: null, pairedClients: 0, hosts: writerHosts(), styles: styleOptions}), close: async () => {}};
  }
  protocol.handle('app', request => Promise.resolve().then(() => serve(request)).catch(error => {
    mainLog.write('app protocol', request.url, error);
    return new Response('本地资源加载失败', {status: 500});
  }));
  ipcMain.handle('rpc', async (event, method, params) => {
    trusted(event);
    if (method === 'writing.status') return officeBridge.status();
    if (method === 'browser.status') return officeBridge.browserStatus();
    if (method === 'writing.newPairing') {
      const code = officeBridge.rotatePairing();
      if (!code) throw new Error(officeBridge.status().error || '本机写作服务未启动');
      return officeBridge.status();
    }
    if (method === 'citation.styles') return styleOptions;
    if (method === 'citation.format') {
      const items = await Promise.all(params.ids.map(id => rpc('items.get', {id})));
      const style = styleOptions.some(s=>s.id===params.style) ? params.style : 'apa';
      const lang = params.lang === 'zh-CN' ? 'zh-CN' : 'en-US';
      const citation = new Cite(items);
      return {text: citation.format('bibliography', {format: 'text', template: style, lang}), html: citation.format('bibliography', {format: 'html', template: style, lang})};
    }
    if (!allowed.has(method)) throw new Error('操作不支持');
    if (method === 'settings.save' && (params.journalRoot !== undefined || params.backupDirectory !== undefined)) throw new Error('请通过目录选择器修改路径');
    if (method === 'settings.save' && ['assistantBaseUrl','assistantModel','assistantTimeout'].some(key => params[key] !== undefined)) throw new Error('请通过阅读助手设置保存服务配置');
    return rpc(method, params);
  });
  ipcMain.handle('files', (event, kind, params) => {trusted(event); return fileAction(kind, params || {});});
  ipcMain.handle('assistant-secret', async (event, action, value) => {
    trusted(event);
    if (action === 'status') return assistantSecretStatus();
    if (action === 'save') {saveAssistantSecret(value); await activateAssistantSecret(); return assistantSecretStatus();}
    if (action === 'delete') {if (fs.existsSync(assistantSecretPath())) fs.unlinkSync(assistantSecretPath()); await activateAssistantSecret(); return assistantSecretStatus();}
    throw new Error('凭据操作不支持');
  });
  ipcMain.handle('drop', (event, paths) => {trusted(event); return rpc('_imports.preview', {paths});});
  ipcMain.handle('external', async (event, value) => {trusted(event); const url = new URL(value); if (!['https:', 'http:'].includes(url.protocol)) throw new Error('只支持网页链接'); await shell.openExternal(url.href);});
  ipcMain.handle('clipboard', (event, text) => {trusted(event); clipboard.writeText(String(text).slice(0, 16000000));});
  ipcMain.on('window', (event, action) => {trusted(event); if (action === 'minimize') win.minimize(); else if (action === 'maximize') win.isMaximized() ? win.unmaximize() : win.maximize(); else if (action === 'close') win.close();});
  win = new BrowserWindow({width: 1600, height: 1000, minWidth: 1120, minHeight: 700, show: false, frame: false, backgroundColor: '#f7f8fb', title: '文献工作台', webPreferences: {preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, nodeIntegration: false, sandbox: true}});
  win.webContents.setWindowOpenHandler(({url}) => {if (/^https?:\/\//i.test(url)) shell.openExternal(url); return {action: 'deny'};});
  win.webContents.on('will-navigate', (event, url) => {if (!url.startsWith('app://local/')) event.preventDefault();});
  win.webContents.session.setPermissionRequestHandler((_, __, callback) => callback(false));
  Menu.setApplicationMenu(Menu.buildFromTemplate([{label: '文件', submenu: [
    {label: '导入文献', accelerator: 'CmdOrCtrl+O', click: () => win.webContents.send('backend-event', 'menu.import', {})},
    {label: '搜索文献', accelerator: 'CmdOrCtrl+F', click: () => win.webContents.send('backend-event', 'menu.search', {})},
    {type: 'separator'}, {role: 'quit', label: '退出'}]}, {label: '编辑', submenu: [{role: 'undo'}, {role: 'redo'}, {role: 'cut'}, {role: 'copy'}, {role: 'paste'}, {role: 'selectAll'}]}]));
  await win.loadURL('app://local/');
  if (smoke) await require(process.env.RESEARCH_QA_MODE==='translationLayout' ? './translation-layout-smoke.cjs' : process.env.RESEARCH_QA_MODE==='advancedSearch' ? './advanced-search-smoke.cjs' : process.env.RESEARCH_QA_MODE==='browser' ? './browser-smoke.cjs' : process.env.RESEARCH_QA_MODE==='qiewenReal' ? './qiewen-real-smoke.cjs' : process.env.RESEARCH_QA_MODE==='continuous' ? './continuous-smoke.cjs' : process.env.RESEARCH_QA_MODE==='realPdf' ? './real-pdf-smoke.cjs' : process.env.RESEARCH_QA_MODE==='deepseek' ? './deepseek-smoke.cjs' : process.env.RESEARCH_QA_MODE==='mainError' ? './main-error-smoke.cjs' : process.env.RESEARCH_QA_MODE==='workflow' ? './workflow-smoke.cjs' : process.env.RESEARCH_QA_MODE==='searchV2' ? './search-v2-smoke.cjs' : process.env.RESEARCH_QA_MODE==='journals' ? './journal-smoke.cjs' : process.env.RESEARCH_QA_MODE==='aiSearch' ? './ai-search-smoke.cjs' : process.env.RESEARCH_QA_MODE==='relations' ? './relations-smoke.cjs' : process.env.RESEARCH_QA_MODE==='writing' ? './writing-smoke.cjs' : process.env.RESEARCH_QA_MODE==='comparison' ? './comparison-smoke.cjs' : './smoke.cjs').run({win, rpc, root, project, app});
  else win.show();
}
if (!app.requestSingleInstanceLock({smoke})) app.quit();
else {
  // The browser extension opens this local-only URI if it cannot reach the
  // capture bridge. It contains no citation data or credentials.
  if (!smoke) app.setAsDefaultProtocolClient(launcherScheme);
  app.on('second-instance', () => focusWindow());
  app.on('open-url', (event, url) => {if (url.startsWith(`${launcherScheme}://`)) {event.preventDefault(); focusWindow();}});
  app.whenReady().then(init).catch(error => {mainLog.write('startup', error); if (smoke) console.error(error); else dialog.showErrorBox('无法启动文献工作台', `${error.message}\n\n日志：${path.join(app.getPath('userData'), 'main.log')}`); app.exit(1);});
}
app.on('window-all-closed', () => {if (!closing) app.quit();});
app.on('before-quit', event => {
  if (officeBridge) {officeBridge.close().catch(() => {}); officeBridge = null;}
  if (!closing && child && child.exitCode === null) {
    event.preventDefault(); closing = true;
    // Stop renderer timers before closing the backend pipe. They otherwise
    // keep sending IPC calls during shutdown and can surface broken-pipe logs.
    if (win && !win.isDestroyed()) win.destroy();
    if (child.stdin.writable) child.stdin.end(JSON.stringify({method: '_shutdown'}) + '\n');
    child.once('exit', () => smoke ? app.exit(process.exitCode || 0) : app.quit());
    setTimeout(() => {if (child.exitCode === null) child.kill(); smoke ? app.exit(process.exitCode || 0) : app.quit();}, 10000).unref();
  }
});
