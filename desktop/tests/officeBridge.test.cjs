const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const {plugins} = require('@citation-js/core');
require('@citation-js/plugin-csl');
const {startOfficeBridge, BROWSER_ORIGIN, LEGACY_BROWSER_ORIGIN} = require('../electron/office-bridge.cjs');

const csl = plugins.config.get('@csl');
csl.locales.add('zh-CN', fs.readFileSync(path.join(__dirname, '../electron/csl/locales-zh-CN.xml'), 'utf8'));
csl.styles.add('bridge-gb', fs.readFileSync(path.join(__dirname, '../electron/csl/gb-t-7714-2015-numeric.csl'), 'utf8'));
const item = {id: '11111111-1111-1111-1111-111111111111', title: 'Bridge source', author: [{family: 'Wang', given: 'Mei'}], issued: {'date-parts': [[2024]]}, type: 'article-journal', citationKey: 'Wang2024'};

test('pairs a local Word taskpane and serves only paired citation calls', async () => {
  const calls = [];
  const bridge = await startOfficeBridge({
    port: 0,
    rpc: async (method, params) => {calls.push([method, params]); if (method === 'items.list') return {items: [item]}; if (method === 'items.get') return item; if (method === 'annotations.search') return {items: [{id: 'annotation-1', itemId: item.id, quote: 'Evidence', page: 4, stale: false}], total: 1}; if (method === 'writing.session.save') return {id: params.id}; if (method === 'writing.event') return {ok: true}; throw Error('unexpected ' + method);},
    styles: () => [{id: 'bridge-gb', name: 'GB'}], styleXml: id => csl.styles.get(id), localeXml: language => csl.locales.get(language) || csl.locales.get('en-US'),
    hostStatus: () => ({word: {installed: true}, wps: {installed: false}}),
  });
  try {
    const blocked = await fetch(`http://localhost:${bridge.status().port}/api/items?q=bridge`);
    assert.equal(blocked.status, 401);
    const wps = await fetch(`http://localhost:${bridge.status().port}/api/pair`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({host: 'wps', code: bridge.status().pairingCode})});
    assert.equal(wps.status, 401);
    assert.equal((await fetch(`http://localhost:${bridge.status().port}/office/wps.html`)).status, 404);
    const paired = await fetch(`http://localhost:${bridge.status().port}/api/pair`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({host: 'word', code: bridge.status().pairingCode})});
    assert.equal(paired.status, 200);
    const {token} = await paired.json();
    const annotationUrl = `http://localhost:${bridge.status().port}/api/annotations?itemId=${item.id}`;
    assert.equal((await fetch(annotationUrl)).status, 401);
    const annotationResponse = await fetch(annotationUrl, {headers: {'x-research-writer': token}});
    assert.equal(annotationResponse.status, 200);
    assert.equal((await annotationResponse.json()).items[0].quote, 'Evidence');
    assert.ok(calls.some(([method, params]) => method === 'annotations.search' && params.itemIds[0] === item.id));
    const render = await fetch(`http://localhost:${bridge.status().port}/api/render`, {method: 'POST', headers: {'Content-Type': 'application/json', 'x-research-writer': token}, body: JSON.stringify({styleId: 'bridge-gb', clusters: [{id: 'cluster-one', ordinal: 0, items: [{itemId: item.id}]}]})});
    const value = await render.json();
    assert.equal(value.citations[0].text, '[1]');
    assert.match(value.bibliography.text, /Bridge source/);
    assert.ok(calls.some(([method]) => method === 'items.get'));
  } finally {await bridge.close();}
});

test('browser capture connects automatically only from this extension origin', async () => {
  const origin = BROWSER_ORIGIN;
  const calls = [];
  const bridge = await startOfficeBridge({
    port: 0,
    rpc: async (method, params) => {calls.push([method, params]); if (method === 'collections.list') return [{id: 'collection-1', name: '调度'}];
      if (method === 'collections.edit') return {id: 'collection-2'};
      if (method === 'browser.capture') return {itemId: 'item-1', created: true};
      if (method === 'browser.duplicates') return {duplicate: {itemId: 'item-1', title: 'Paper', basis: 'DOI'}};
      if (method === 'browser.importDownloaded') return {attachmentId: 'attachment-1', message: 'PDF 已导入，正在解析并建立全文索引'};
      throw Error('unexpected ' + method);},
    styles: () => [], styleXml: () => '', localeXml: () => '', hostStatus: () => ({}),
  });
  try {
    const base = `http://127.0.0.1:${bridge.status().port}/api/browser`;
    assert.equal((await fetch(base + '/collections')).status, 401);
    assert.equal((await fetch(base + '/connect', {method: 'POST'})).status, 403);
    const oldExtension = await fetch(base + '/connect', {method: 'POST', headers: {Origin: 'chrome-extension://' + 'a'.repeat(32)}});
    assert.equal(oldExtension.status, 403);
    assert.equal(oldExtension.headers.get('access-control-allow-origin'), 'chrome-extension://' + 'a'.repeat(32));
    assert.match((await oldExtension.json()).error, /扩展版本不匹配/);
    const preflight = await fetch(base + '/connect', {method: 'OPTIONS', headers: {Origin: origin, 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type,x-research-browser'}});
    assert.equal(preflight.status, 204);
    assert.equal(preflight.headers.get('access-control-allow-origin'), origin);
    const connected = await fetch(base + '/connect', {method: 'POST', headers: {Origin: origin}});
    assert.equal(connected.status, 200);
    const legacyConnected = await fetch(base + '/connect', {method: 'POST', headers: {Origin: LEGACY_BROWSER_ORIGIN}});
    assert.equal(legacyConnected.status, 200);
    assert.equal(legacyConnected.headers.get('access-control-allow-origin'), LEGACY_BROWSER_ORIGIN);
    const {token} = await connected.json();
    const headers = {Origin: origin, 'X-Research-Browser': token, 'Content-Type': 'application/json'};
    const noOriginList = await fetch(base + '/collections', {headers: {'X-Research-Browser': token}});
    assert.equal(noOriginList.status, 200);
    assert.equal(noOriginList.headers.get('access-control-allow-origin'), null);
    assert.equal((await fetch(base + '/collections', {headers: {'X-Research-Browser': 'expired'}})).status, 401);
    const list = await fetch(base + '/collections', {headers});
    assert.equal(list.status, 200);
    assert.equal((await list.json()).collections[0].name, '调度');
    const duplicate = await fetch(base + '/duplicates', {method: 'POST', headers, body: JSON.stringify({pageUrl: 'https://example.org', data: {title: 'Paper'}})});
    assert.equal(duplicate.status, 200);
    assert.equal((await duplicate.json()).duplicate.basis, 'DOI');
    const capture = await fetch(base + '/capture', {method: 'POST', headers, body: JSON.stringify({pageUrl: 'https://example.org', data: {title: 'Paper'}})});
    assert.equal((await capture.json()).created, true);
    assert.ok(calls.some(([method]) => method === 'browser.capture'));
    const imported = await fetch(base + '/importDownloaded', {method: 'POST', headers, body: JSON.stringify({itemId: 'item-1', sourceUrl: 'https://kns.cnki.net/kcms2/article/abstract?v=fixture', format: 'pdf', path: 'C:\\Temp\\fixture.pdf'})});
    assert.equal((await imported.json()).attachmentId, 'attachment-1');
    assert.ok(calls.some(([method]) => method === 'browser.importDownloaded'));
    assert.equal((await fetch(`http://127.0.0.1:${bridge.status().port}/api/items`, {headers: {'X-Research-Writer': token}})).status, 401);
    assert.equal((await fetch(base + '/collections', {headers: {...headers, Origin: 'chrome-extension://' + 'a'.repeat(32)}})).status, 403);
  } finally {await bridge.close();}
});
