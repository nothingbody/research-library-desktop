const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

exports.run = async ({win, root, project, app}) => {
  const out = process.env.RESEARCH_QA_OUT || path.join(project, 'docs/verification/comparison');
  fs.mkdirSync(out, {recursive: true});
  const report = {startedAt: new Date().toISOString(), library: root, assertions: [], console: [], screenshots: []};
  win.webContents.setBackgroundThrottling(false);
  win.setContentSize(1586, 992);
  win.showInactive();
  win.webContents.on('console-message', event => {if (event.level === 'error') report.console.push(event.message);});
  const execute = code => win.webContents.executeJavaScript(code, true);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function until(code, message, timeout = 12000) {
    const started = Date.now();
    while (Date.now() - started < timeout) {if (await execute(code)) return; await sleep(150);}
    throw new Error('Timeout: ' + message + ': ' + await execute('document.body.innerText.slice(-1500)'));
  }
  async function capture(name) {
    await execute('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
    await sleep(300);
    const image = await win.webContents.capturePage();
    fs.writeFileSync(path.join(out, name + '.png'), image.toPNG());
    report.screenshots.push(name + '.png');
  }
  try {
    await until('!!window.research && !!document.querySelector(".library-header")', 'library mount');
    await execute(`(() => {const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim().startsWith('检索对比')); if (!b) throw Error('comparison navigation not found'); b.click();})()`);
    await until('!!document.querySelector(".comparison-page")', 'comparison page');
    const data = {
      '对比检索主题': 'multi-workshop multi-objective job shop scheduling',
      '外部检索来源': 'External source smoke',
      '外部来源页面': 'https://example.org/external-search',
      '外部题名': 'External scheduling retrieval',
      '外部作者': 'External Author',
      '外部年份': '2026',
      '外部期刊 / 来源': 'External Journal',
      '外部DOI': '10.1000/external',
      '外部摘要': 'A deliberately different external record.',
      '外部开放获取链接': 'https://example.org/external.pdf',
      '本地题名': 'Local scheduling retrieval',
      '本地作者': 'Local Author',
      '本地年份': '2024',
      '本地期刊 / 来源': 'Journal of Manufacturing Systems',
      '本地DOI': '10.1016/j.jmsy.2024.03.005',
      '本地摘要': 'A locally retrieved scholarly candidate.',
      '本地开放获取链接': 'https://doi.org/10.1016/j.jmsy.2024.03.005',
      '对比结论': 'Smoke test preserves a field-level comparison.',
    };
    await execute(`(() => {const values = ${JSON.stringify(data)}; for (const [label, value] of Object.entries(values)) {const el = document.querySelector('[aria-label="' + label + '"]'); if (!el) throw Error('Input not found: ' + label); const prototype = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(prototype, 'value').set.call(el, value); el.dispatchEvent(new Event('input', {bubbles: true}));}})()`);
    await until('document.querySelector(".comparison-field-grid .different")?.textContent.includes("不一致")', 'live field comparison');
    assert.ok(await execute('document.querySelector(".comparison-preview").textContent.includes("两侧 DOI 不同")'));
    await capture('01-comparison-before-save');
    await execute('document.querySelector(".comparison-preview button.primary").click()');
    await until('document.querySelector(".comparison-history")?.textContent.includes("External scheduling retrieval")', 'saved comparison history');
    const saved = await execute('window.research.call("comparison.list").then(rows => rows[0])');
    assert.equal(saved.title, 'External scheduling retrieval');
    assert.ok(saved.summary.different >= 1);
    assert.equal(saved.external.doi, '10.1000/external');
    assert.equal(saved.local.doi, '10.1016/j.jmsy.2024.03.005');
    report.assertions.push({name: 'Visible comparison form saves field differences through the local RPC', passed: true});
    await capture('02-comparison-saved');
    await execute('document.querySelector(".comparison-history article button").click()');
    await until('document.querySelector("[aria-label=对比结论]").value.includes("Smoke test preserves")', 'reload full conclusion');
    assert.equal(await execute('document.querySelector("[aria-label=外部DOI]").value'), data['外部DOI']);
    report.assertions.push({name:'Different-paper warning and saved full-record reopening',passed:true});
    report.finishedAt = new Date().toISOString(); report.passed = true;
  } catch (error) {
    report.passed = false; report.error = error.stack; await capture('failure').catch(() => {});
  }
  fs.writeFileSync(path.join(out, 'smoke-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({passed: report.passed, assertions: report.assertions.length, error: report.error, console: report.console, out}));
  process.exitCode = report.passed ? 0 : 1;
  app.quit();
};
