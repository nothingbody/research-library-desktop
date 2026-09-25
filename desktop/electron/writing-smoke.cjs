const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

exports.run = async ({win, rpc, root, project, app}) => {
  const out = process.env.RESEARCH_QA_OUT || path.join(project, 'docs/verification/writing');
  fs.mkdirSync(out, {recursive: true});
  const report = {startedAt: new Date().toISOString(), library: root, assertions: [], screenshots: [], console: []};
  const run = code => win.webContents.executeJavaScript(code, true);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  win.webContents.setBackgroundThrottling(false); win.setContentSize(1586, 992); win.showInactive();
  win.webContents.on('console-message', event => {if (event.level === 'error') report.console.push(event.message);});
  async function until(code, message, timeout = 25000) {
    const started = Date.now();
    while (Date.now() - started < timeout) {if (await run(code)) return; await sleep(160);}
    throw Error('Timeout: ' + message + ': ' + await run('document.body.innerText.slice(-1800)'));
  }
  async function capture(name) {await run('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))'); await sleep(260); fs.writeFileSync(path.join(out, name + '.png'), (await win.webContents.capturePage()).toPNG()); report.screenshots.push(name + '.png');}
  try {
    await until('!!document.querySelector(".library-header")', 'app shell');
    const paper = await rpc('items.create', {data: {title: 'Writing bridge verification source', author: [{family: 'Wang', given: 'Mei'}], year: '2024', type: 'article-journal'}});
    const bridge = await run('window.research.call("writing.status")');
    assert.equal(bridge.running, true, bridge.error || 'writing bridge unavailable');
    await run(`(()=>{const target=[...document.querySelectorAll('.nav-item')].find(item=>item.textContent.includes('写作引用'));if(!target)throw Error('writing nav missing');target.click();})()`);
    await until('!!document.querySelector(".writing-page")', 'writing page');
    await until('document.querySelector(".pairing-code strong")?.textContent.trim().length===6', 'pairing code');
    await capture('01-writing-center');
    const session = await run(`window.research.call('writing.session.save',{id:'session-smoke-12345678',host:'word',documentFingerprint:'smoke:writer',documentName:'writing-smoke.docx',styleId:'gb-t-7714-2015',locale:'zh-CN',state:{version:1},eventType:'smoke',bibliography:{anchorKey:'research-library:bibliography'},clusters:[{id:'cluster-smoke-12345678',anchorKey:'research-library:citation:cluster-smoke-12345678',citation:{properties:{noteIndex:1}},renderedText:'[1]',items:[{itemId:${JSON.stringify(paper.id)},locator:'12',label:'page',prefix:'',suffix:'',suppressAuthor:false}]}]})`);
    assert.equal(session.id, 'session-smoke-12345678');
    const sessions = await run('window.research.call("writing.sessions")');
    assert.equal(sessions[0].citationCount, 1);
    report.assertions.push({name: 'Writing bridge, pairing UI, and persisted citation session', passed: true});
    assert.equal(report.console.length, 0, report.console.join('\n'));
    report.assertions.push({name: 'No renderer console errors', passed: true});
    report.finishedAt = new Date().toISOString(); report.passed = true;
  } catch (error) {
    report.passed = false; report.error = error.stack; try {await capture('failure');} catch {}
  }
  fs.writeFileSync(path.join(out, 'smoke-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({passed: report.passed, assertions: report.assertions.length, error: report.error, console: report.console, out}));
  process.exitCode = report.passed ? 0 : 1; app.quit();
};
