const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {BROWSER_ORIGIN, LEGACY_BROWSER_ORIGIN} = require('./office-bridge.cjs');

exports.run = async ({win, rpc, project, app}) => {
  const out = process.env.RESEARCH_QA_OUT || path.join(project, 'docs/verification/browser');
  fs.mkdirSync(out, {recursive: true});
  const report = {passed: false, assertions: [], errors: []};
  const execute = code => win.webContents.executeJavaScript(code, true);
  win.showInactive();
  win.webContents.on('console-message', event => {if (event.level === 'error') report.errors.push(event.message);});
  try {
    assert.ok(await execute('Boolean(window.research && document.querySelector(".library-header"))'));
    const status = await execute('window.research.call("browser.status")');
    assert.equal(status.running, true);
    report.assertions.push('installed UI and loopback bridge started');
    await execute(`(() => {const button = [...document.querySelectorAll('button')].find(value => value.textContent.trim() === '设置'); if (!button) throw Error('settings nav missing'); button.click();})()`);
    await new Promise(resolve => setTimeout(resolve, 400));
    assert.match(await execute('document.body.innerText'), /浏览器采集/);
    fs.writeFileSync(path.join(out, 'browser-settings.png'), (await win.webContents.capturePage()).toPNG());
    assert.doesNotMatch(await execute('document.body.innerText'), /本机配对码|撤销旧连接并换码/);
    report.assertions.push('browser setup visible without pairing code');
    const base = `http://127.0.0.1:${status.port}/api/browser`;
    const origin = BROWSER_ORIGIN;
    assert.equal((await fetch(base + '/connect', {method: 'POST', headers: {Origin: 'chrome-extension://' + 'a'.repeat(32)}})).status, 403);
    const connected = await fetch(base + '/connect', {method: 'POST', headers: {Origin: origin}});
    assert.equal(connected.status, 200);
    const legacyConnected = await fetch(base + '/connect', {method: 'POST', headers: {Origin: LEGACY_BROWSER_ORIGIN}});
    assert.equal(legacyConnected.status, 200);
    report.assertions.push('installed extension legacy origin also auto-connects');
    const {token} = await connected.json();
    const headers = {Origin: origin, 'Content-Type': 'application/json', 'X-Research-Browser': token};
    assert.equal((await fetch(base + '/collections', {headers: {'X-Research-Browser': token}})).status, 200);
    assert.equal((await fetch(base + '/collections', {headers: {'X-Research-Browser': 'expired'}})).status, 401);
    report.assertions.push('origin-less extension reads use token and stale tokens trigger reconnect');
    const collection = await fetch(base + '/collections', {method: 'POST', headers, body: JSON.stringify({name: '浏览器采集验收'})});
    assert.equal(collection.status, 200);
    const collectionId = (await collection.json()).id;
    report.assertions.push('extension auto-connected and created local collection');
    const payload = {pageUrl: 'https://example.org/scheduling-study', collectionId, pdfUrl: 'https://example.org/scheduling-study.pdf',
      data: {type: 'article-journal', title: 'Browser capture acceptance fixture', author: ['Wang, Mei'], year: '2025', DOI: '10.1000/browser-smoke'}};
    const capture = async () => {
      const response = await fetch(base + '/capture', {method: 'POST', headers, body: JSON.stringify(payload)});
      assert.equal(response.status, 200);
      return response.json();
    };
    const first = await capture(), second = await capture();
    assert.equal(first.created, true); assert.equal(second.created, false); assert.equal(first.itemId, second.itemId);
    const duplicate = await fetch(base + '/duplicates', {method: 'POST', headers, body: JSON.stringify({pageUrl: payload.pageUrl, data: payload.data})});
    assert.equal(duplicate.status, 200);
    assert.equal((await duplicate.json()).duplicate.itemId, first.itemId);
    assert.equal((await rpc('items.get', {id: first.itemId})).DOI, '10.1000/browser-smoke');
    assert.equal((await rpc('items.list', {collectionId})).total, 1);
    assert.equal((await rpc('fulltext.sources', {itemId: first.itemId})).length, 1);
    report.assertions.push('capture is idempotent and records the PDF source');
    report.assertions.push('duplicate preview agrees with saved record');
    assert.equal((await fetch(base + '/collections', {headers: {...headers, Origin: 'chrome-extension://' + 'a'.repeat(32)}})).status, 403);
    report.assertions.push('other extension origins cannot use browser token');
    assert.deepEqual(report.errors, []);
    report.passed = true;
  } catch (error) {report.error = error.stack;}
  fs.writeFileSync(path.join(out, 'browser-smoke-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({passed: report.passed, assertions: report.assertions.length, error: report.error, out}));
  process.exitCode = report.passed ? 0 : 1;
  app.quit();
};
