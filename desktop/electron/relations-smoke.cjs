const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

exports.run = async ({win, rpc, root, project, app}) => {
  const out = process.env.RESEARCH_QA_OUT || path.join(project, 'docs/verification/relations');
  fs.mkdirSync(out, {recursive: true});
  const report = {startedAt: new Date().toISOString(), library: root, assertions: [], screenshots: [], console: []};
  const run = code => win.webContents.executeJavaScript(code, true);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  win.webContents.setBackgroundThrottling(false); win.setContentSize(1586, 992); win.showInactive();
  win.webContents.on('console-message', event => {if (event.level === 'error') report.console.push(event.message);});
  async function until(code, message, timeout = 25000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {if (await run(code)) return; await sleep(160);}
    throw Error('Timeout: ' + message + ': ' + await run('document.body.innerText.slice(-1800)'));
  }
  async function capture(name) {
    await run('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))'); await sleep(260);
    fs.writeFileSync(path.join(out, name + '.png'), (await win.webContents.capturePage()).toPNG()); report.screenshots.push(name + '.png');
  }
  try {
    await until('!!document.querySelector(".library-header")', 'app shell');
    const papers = [
      ['Evidence-aware scheduling methods for flexible manufacturing', 'This study investigates flexible manufacturing scheduling. We compare a genetic algorithm with dispatching rules on energy use and completion time. The results show lower energy use, while the limited benchmark set requires further validation.'],
      ['Multi-objective optimization for energy efficient flexible job shops', 'We study flexible job shop scheduling with multi-objective optimization. The method evaluates energy consumption and makespan using industrial benchmark data. Results suggest an efficient schedule, but sample coverage remains limited.'],
      ['Benchmark design for industrial scheduling experiments', 'This paper reports industrial benchmark design and explains limitations of scheduling evaluation datasets.']
    ];
    for (const [index, [title, abstract]] of papers.entries()) await rpc('items.create', {data: {title, abstract, year: 2022 + index, citationCount: (index + 1) * 10, type: 'article-journal'}});
    await run(`(()=>{const target=[...document.querySelectorAll('.nav-item')].find(item=>item.textContent.includes('研究关联'));if(!target)throw Error('relations nav missing');target.click();})()`);
    await until('!!document.querySelector(".relation-composer")', 'relation composer');
    await until('document.querySelectorAll(".relation-picker label").length===3', 'local paper picker');
    await run('document.querySelectorAll(".relation-picker label").forEach(label=>label.click())');
    await until('document.querySelectorAll(".selected-reading-set span").length===3', 'three selected papers');
    await capture('01-relation-set');
    await run(`(()=>{const button=[...document.querySelectorAll('button')].find(item=>item.textContent.includes('建立分析集'));button.click();})()`);
    await until('!!document.querySelector(".relation-workspace-header")', 'relation workspace');
    await run(`(()=>{const button=[...document.querySelectorAll('button')].find(item=>item.textContent.trim().startsWith('开始分析'));button.click();})()`);
    await until('document.querySelectorAll(".relation-graph-edge").length>0', 'local evidence network', 30000);
    assert.equal(await run('document.querySelectorAll(".relation-graph-node").length'), 3);
    const initialTransform = await run('document.querySelector(".relation-graph svg > g").getAttribute("transform")');
    await run(`document.querySelector('[aria-label="放大关系网络"]').click()`);
    assert.notEqual(await run('document.querySelector(".relation-graph svg > g").getAttribute("transform")'), initialTransform);
    await run(`document.querySelector('[aria-label="重置关系网络"]').click()`);
    await run('document.querySelector(".relation-graph-node").dispatchEvent(new KeyboardEvent("keydown", {key:"Enter",bubbles:true}))');
    await until('!!document.querySelector(".relation-graph-focus")', 'paper connection summary');
    await run(`document.querySelector('[aria-label="关闭文献信息"]').click()`);
    await capture('02-relations-network');
    await run(`(()=>{const picker=document.querySelector('[aria-label="图谱布局"]');picker.value='chronology';picker.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    await until('!!document.querySelector(".relation-axes")', 'chronology axes');
    assert.equal(await run('document.querySelectorAll(".relation-graph-node").length'), 3);
    await capture('02b-relations-chronology');
    await run(`document.querySelector('[aria-label="导出图谱 SVG"]').click()`);
    for (let attempt = 0; attempt < 50 && !fs.existsSync(path.join(out, 'relation-graph.svg')); attempt++) await sleep(100);
    const exportedSvg = fs.readFileSync(path.join(out, 'relation-graph.svg'), 'utf8');
    assert.match(exportedSvg, /<svg/);
    assert.match(exportedSvg, /年份和被引数坐标轴/);
    assert.match(exportedSvg, /Benchmark design for industrial scheduling experiments/);
    await run(`(()=>{const picker=document.querySelector('[aria-label="图谱布局"]');picker.value='network';picker.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    await until('!document.querySelector(".relation-axes")', 'network view restored');
    await run(`document.querySelector('.relation-manual-form button').click()`);
    await until('document.querySelectorAll(".relation-manual-list article").length===1', 'manual relation added');
    await until('document.querySelectorAll(".relation-graph-edge").length>=4', 'manual edge visible');
    await run('document.querySelectorAll(".relation-graph-edge")[document.querySelectorAll(".relation-graph-edge").length-1].dispatchEvent(new MouseEvent("click", {bubbles:true}))');
    await until('document.querySelector(".relation-discovery-detail")?.textContent.includes("研究者手工记录")', 'manual edge explanation');
    await run('document.querySelector(".relation-graph-edge").dispatchEvent(new MouseEvent("click", {bubbles:true}))');
    await until('document.querySelectorAll(".relation-evidence-list article").length>=2', 'dual evidence panel');
    await capture('03-relations-evidence');
    await run(`document.querySelector('[aria-label="关系列表视图"]').click()`);
    await until('document.querySelectorAll(".relation-arc").length>0', 'list fallback');
    await run(`document.querySelector('[aria-label="网络图视图"]').click()`);
    await until('document.querySelectorAll(".relation-graph-edge").length>0', 'network view restored');
    await run(`(()=>{const button=[...document.querySelectorAll('button')].find(item=>item.textContent.trim().startsWith('确认'));button.click();})()`);
    const sessionId = await run('window.research.call("relations.list").then(values=>values[0].id)');
    await until(`window.research.call('relations.results',{sessionId:${JSON.stringify(sessionId)},status:'confirmed'}).then(values=>values.length>0)`, 'confirmed relation');
    await run(`(()=>{const button=[...document.querySelectorAll('button')].find(item=>item.textContent.includes('生成综述笔记'));button.click();})()`);
    await until('window.research.call("notes.list").then(values=>values.some(value=>value.title.includes("关联综述")))', 'synthesis note');
    report.assertions.push({name: 'Local cross-paper analysis, dual evidence, confirmation and synthesis note', passed: true});
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
