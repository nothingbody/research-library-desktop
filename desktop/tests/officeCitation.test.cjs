const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const {plugins} = require('@citation-js/core');
require('@citation-js/plugin-csl');
const {render} = require('../electron/office-citation.cjs');

const csl = plugins.config.get('@csl');
csl.locales.add('zh-CN', fs.readFileSync(require('node:path').join(__dirname, '../electron/csl/locales-zh-CN.xml'), 'utf8'));
csl.styles.add('gb-t-7714-2015-test', fs.readFileSync(require('node:path').join(__dirname, '../electron/csl/gb-t-7714-2015-numeric.csl'), 'utf8'));
const records = [
  {id: '11111111-1111-1111-1111-111111111111', title: 'First source', author: [{family: 'Wang', given: 'Mei'}], issued: {'date-parts': [[2024]]}, type: 'article-journal'},
  {id: '22222222-2222-2222-2222-222222222222', title: 'Second source', author: [{family: 'Li', given: 'Qiang'}], issued: {'date-parts': [[2025]]}, type: 'article-journal'},
];
const localeXml = language => csl.locales.get(language) || csl.locales.get('en-US');

test('replays citation clusters in document order and renders bibliography', () => {
  const result = render({
    clusters: [
      {id: 'cluster-one', ordinal: 0, items: [{itemId: records[0].id, locator: '12', label: 'page'}]},
      {id: 'cluster-two', ordinal: 1, items: [{itemId: records[1].id}]},
    ], records, styleXml: csl.styles.get('gb-t-7714-2015-test'), localeXml, locale: 'zh-CN',
  });
  assert.equal(result.citations[0].text, '[1]');
  assert.equal(result.citations[1].text, '[2]');
  assert.match(result.bibliography.text, /\[1\].*First source/);
  assert.match(result.bibliography.text, /\[2\].*Second source/);
});

test('keeps author-date locators and suppress-author instructions', () => {
  const result = render({
    clusters: [{id: 'cluster-three', ordinal: 0, items: [{itemId: records[0].id, locator: '12', label: 'page', suppressAuthor: true}]}],
    records, styleXml: csl.styles.get('apa'), localeXml, locale: 'en-US',
  });
  assert.match(result.citations[0].text, /2024/);
  assert.match(result.citations[0].text, /p\. 12/);
  assert.doesNotMatch(result.citations[0].text, /Wang/);
});
