const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');

exports.run = async ({win, rpc, root, project, app}) => {
  const out = process.env.RESEARCH_QA_OUT || path.join(project, 'docs/verification/ai-search');
  fs.mkdirSync(out, {recursive: true});
  const report = {startedAt: new Date().toISOString(), library: root, assertions: [], screenshots: [], console: []};
  const run = code => win.webContents.executeJavaScript(code, true);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  win.webContents.setBackgroundThrottling(false);
  win.setContentSize(1586, 992);
  win.showInactive();
  win.webContents.on('console-message', event => {if (event.level === 'error') report.console.push(event.message);});
  let introCalls = 0;
  const modelServer = http.createServer((req, res) => {
    let body = '';
    req.on('data', chunk => {body += chunk;});
    req.on('end', () => {
      const prompt = JSON.parse(body).messages.at(-1).content;
      const source = JSON.parse(prompt);
      introCalls++;
      const content = JSON.stringify({titleZh: '测试译文：' + source.title,
        abstractZh: source.abstract ? '测试摘要：' + source.abstract : '',
        keywordsZh: source.keywords.map(word => '测试关键词：' + word)});
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({choices: [{message: {content}}]}));
    });
  });
  async function until(code, message, timeout = 60000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {if (await run(code)) return; await sleep(250);}
    throw Error('Timeout: ' + message + ': ' + await run('document.body.innerText.slice(-1200)'));
  }
  async function capture(name) {
    await run('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    fs.writeFileSync(path.join(out, name + '.png'), (await win.webContents.capturePage()).toPNG());
    report.screenshots.push(name + '.png');
  }
  try {
    await new Promise(resolve => modelServer.listen(0, '127.0.0.1', resolve));
    await rpc('assistant.settings', {assistantBaseUrl: `http://127.0.0.1:${modelServer.address().port}/v1`, assistantModel: 'local-smoke'});
    await rpc('assistant.configure', {apiKey: 'local-smoke-key'});
    await until('!!document.querySelector(".library-header")', 'app shell');
    await run(`(()=>{[...document.querySelectorAll('.nav-item')].find(item=>item.textContent.includes('AI 文献检索')).click()})()`);
    await until('!!document.querySelector(".ai-composer")', 'search composer');
    await run(`(()=>{const field=document.querySelector('textarea[aria-label="研究任务"]');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(field,'大模型下的智慧物流发展综述');field.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('.model-plan input').click()})()`);
    await capture('01-search-entry');
    await run(`document.querySelector('.discovery-search-button').click()`);
    await until('!!document.querySelector(".ai-workspace-header")', 'automatic search');
    assert.equal(await run('!!document.querySelector(".ai-plan-panel")'), false);
    await until('document.querySelectorAll(".discovery-list tbody tr").length>0', 'public-source results', 90000);
    await until('!document.querySelector(".discovery-list tbody input").disabled', 'retrieval completion', 120000);
    await capture('02-simple-results');
    await run(`document.querySelector('.discovery-paper button').click()`);
    await until('!!document.querySelector(".discovery-intro")', 'paper overview');
    await until('document.querySelector(".intro-translation")?.textContent.includes("测试译文")', 'automatic title translation');
    assert.ok(await run('!!document.querySelector(".discovery-reading-steps")'));
    assert.ok(await run('!!document.querySelector(".discovery-evidence-details")'));
    await capture('03-paper-overview');
    await run(`document.querySelector('.ai-evidence-panel .panel-heading button').click();document.querySelector('.discovery-paper button').click()`);
    await until('document.querySelector(".intro-translation")?.textContent.includes("测试译文")', 'cached translation');
    assert.equal(introCalls, 1, 'reopening must reuse the saved translation');
    report.assertions.push({name: 'One-click search, neutral result list, automatic and cached paper translation', passed: true});
    assert.equal(report.console.length, 0, report.console.join('\n'));
    report.finishedAt = new Date().toISOString(); report.passed = true;
  } catch (error) {
    report.passed = false; report.error = error.stack;
    try {await capture('failure');} catch {}
  }
  fs.writeFileSync(path.join(out, 'smoke-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({passed: report.passed, error: report.error, out}));
  modelServer.close();
  process.exitCode = report.passed ? 0 : 1; app.quit();
};
