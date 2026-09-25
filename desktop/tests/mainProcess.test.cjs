const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {spawnSync} = require('node:child_process');

const desktop = path.resolve(__dirname, '..');
const main = fs.readFileSync(path.join(desktop, 'electron/main.cjs'), 'utf8');
const backend = fs.readFileSync(path.join(desktop, '../client_backend/service.py'), 'utf8');
const allowed = new Set(main.match(/const allowed = new Set\(\('([^']+)'\)/)[1].split(' '));
const local = new Set(['citation.styles', 'citation.format', 'writing.status', 'writing.newPairing', 'browser.status']);

test('every literal frontend RPC is handled locally or allowed by the backend', () => {
  const files = fs.readdirSync(path.join(desktop, 'src')).filter(name => /\.tsx?$/.test(name));
  const called = new Set(files.flatMap(name => [...fs.readFileSync(path.join(desktop, 'src', name), 'utf8').matchAll(/\bapi\(\s*['"]([^'"]+)['"]/g)].map(match => match[1])));
  const missing = [...called].filter(method => !local.has(method) && !allowed.has(method));
  const absent = [...called].filter(method => !local.has(method) && !backend.includes(`'${method}':`));
  assert.deepEqual(missing, [], `Missing Electron allowlist entries: ${missing.join(', ')}`);
  assert.deepEqual(absent, [], `Missing backend routes: ${absent.join(', ')}`);
});

test('GUI error logging does not write to a broken stderr pipe', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'research-main-log-'));
  try {
    const script = `const log=require(${JSON.stringify(path.join(desktop, 'electron/main-log.cjs'))});log.install(${JSON.stringify(directory)});process.stderr.destroy();console.error('原始错误:',new Error('resource unavailable'));setTimeout(()=>{},10);`;
    const result = spawnSync(process.execPath, ['-e', script], {encoding: 'utf8', timeout: 5000});
    assert.equal(result.status, 0, result.stderr);
    const content = fs.readFileSync(path.join(directory, 'main.log'), 'utf8');
    assert.match(content, /原始错误/);
    assert.match(content, /resource unavailable/);
  } finally {
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test('packaged Word manifest is exposed as a real file for Office upload', () => {
  const config = require('../package.json');
  assert.ok(config.build.extraResources.some(entry => entry.to === 'word-addin/word-manifest.xml'));
  assert.match(main, /process\.resourcesPath, 'word-addin', 'word-manifest\.xml'/);
  assert.ok(fs.existsSync(path.join(desktop, 'electron/office-addin/word-manifest.xml')));
});

test('the installed app registers a local launcher for browser capture wake-up', () => {
  const config = require('../package.json');
  assert.ok(config.build.protocols.some(protocol => protocol.schemes.includes('researchlibrary')));
  assert.match(main, /app\.setAsDefaultProtocolClient\(launcherScheme\)/);
  assert.match(main, /app\.on\('second-instance', \(\) => focusWindow\(\)\)/);
});
