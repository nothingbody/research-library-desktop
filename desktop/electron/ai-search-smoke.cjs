const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

exports.run = async ({win, rpc, root, project, app}) => {
  const out = process.env.RESEARCH_QA_OUT || path.join(project, 'docs/verification/ai-search');
  fs.mkdirSync(out, {recursive: true});
  const report = {startedAt: new Date().toISOString(), library: root, assertions: [], screenshots: [], console: []};
  const run = code => win.webContents.executeJavaScript(code, true);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  win.webContents.setBackgroundThrottling(false); win.setContentSize(1586, 992); win.showInactive();
  win.webContents.on('console-message', event => {if (event.level === 'error') report.console.push(event.message);});
  async function until(code, message, timeout = 30000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {if (await run(code)) return; await sleep(180);}
    throw Error('Timeout: ' + message + ': ' + await run('document.body.innerText.slice(-1500)'));
  }
  async function capture(name) {
    await run('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))'); await sleep(240);
    fs.writeFileSync(path.join(out, name + '.png'), (await win.webContents.capturePage()).toPNG()); report.screenshots.push(name + '.png');
  }
  try {
    await until('!!document.querySelector(".library-header")', 'app shell');
    await run(`(()=>{const target=[...document.querySelectorAll('.nav-item')].find(item=>item.textContent.includes('AI 文献检索'));if(!target)throw Error('AI search nav missing');target.click();})()`);
    await until('!!document.querySelector(".ai-composer")', 'AI search composer');
    await run(`(()=>{const field=document.querySelector('textarea[aria-label="研究任务"]');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(field,'distributed flexible job shop scheduling multi objective');field.dispatchEvent(new Event('input',{bubbles:true}));[...document.querySelectorAll('.source-toggles input')].forEach((input,i)=>{if(input.checked!==(i<2))input.click();});const model=document.querySelector('.model-plan input');if(model.checked)model.click();})()`);
    await run(`(()=>{const button=[...document.querySelectorAll('button')].find(item=>item.textContent.includes('生成检索计划'));button.click();})()`);
    await until('!!document.querySelector(".ai-plan-panel")', 'editable search plan');
    assert.ok(await run('document.querySelectorAll(".query-editor").length>0'));
    await capture('01-plan');
    await run(`(()=>{const button=[...document.querySelectorAll('button')].find(item=>item.textContent.trim().startsWith('运行检索'));button.click();})()`);
    await until('document.querySelectorAll(".ai-result-card").length>0', 'network results with evidence', 45000);
    const cardCount = await run('document.querySelectorAll(".ai-result-card").length'); assert.ok(cardCount > 0);
    await until('!document.querySelector(".result-select").disabled', 'retrieval completion', 120000);
    await run('document.querySelector(".ai-result-card .result-select").click();document.querySelector(".detail-action").click()');
    await until('document.querySelectorAll(".evidence-list article").length>=2', 'evidence sidebar');
    await capture('02-results-evidence');
    assert.ok(await run(`(()=>{const card=document.querySelector('.ai-result-card');const title=card.querySelector('.paper-title').getBoundingClientRect();const actions=card.querySelector('.result-card-actions').getBoundingClientRect();return title.right<=actions.left&&title.height>0;})()`), 'long paper title must not overlap score and actions');
    report.assertions.push({name: 'Long paper title stays clear of score and actions', passed: true});
    await run('document.querySelector(".result-footer .primary").click()');
    await until('window.research.call("library.stats").then(value=>value.items===1)', 'import selected result');
    report.assertions.push({name: 'AI plan, public-source retrieval, evidence and local import', passed: true});
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
