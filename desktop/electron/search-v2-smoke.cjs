const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
exports.run=async({win,root,app})=>{
  const out=process.env.RESEARCH_QA_OUT;
  const report={startedAt:new Date().toISOString(),library:root,synthetic:true,assertions:[],screenshots:[],console:[]};
  const run=code=>win.webContents.executeJavaScript(code,true);
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const click=text=>run(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()===${JSON.stringify(text)});if(!b)throw Error('Missing button');b.click();})()`);
  const change=(label,value)=>run(`(()=>{const e=document.querySelector('[aria-label="${label}"]');Object.getOwnPropertyDescriptor(e.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event(e.tagName==='SELECT'?'change':'input',{bubbles:true}));})()`);
  const check=(name)=>report.assertions.push({name,passed:true});
  async function until(code,name){for(let i=0;i<120;i++){if(await run(code))return;await sleep(100);}throw Error('Timeout '+name+': '+await run('document.body.innerText.slice(-1000)'));}
  async function capture(name){await sleep(250);fs.writeFileSync(path.join(out,name+'.png'),(await win.webContents.capturePage()).toPNG());report.screenshots.push(name+'.png');}
  win.webContents.setBackgroundThrottling(false);win.setContentSize(1586,992);win.showInactive();
  win.webContents.on('console-message',event=>{if(event.level==='error')report.console.push(event.message);});
  try{
    await until('!!document.querySelector(".library-header")','shell');
    await run(`[...document.querySelectorAll('.nav-item')].find(e=>e.textContent.includes('AI 文献检索')).click()`);
    await until('document.querySelectorAll(".ai-result-card").length===30','30 rows');
    assert.ok(await run('document.querySelector(".pager").textContent.includes("231")'));
    await change('结果排序','title');
    await until('document.querySelector(".paper-title").textContent.startsWith("Paper 000")','A-Z');
    await run('document.querySelector(".detail-action").click()');
    await until('document.querySelectorAll(".evidence-list article").length>0','evidence');
    assert.ok(await run('document.querySelector(".result-footer").textContent.includes("已选 0")'));
    await run('document.querySelector(".result-select").click()');
    await until('document.querySelector(".result-footer").textContent.includes("已选 1")','selection');
    await change('人工筛选决定','include');
    await until('document.querySelector(".result-tags").textContent.includes("纳入")','manual decision');
    await capture('01-evidence');check('Independent detail/selection, evidence, manual decision');
    await run('document.querySelector(".result-footer .primary").click()');
    await until('window.research.call("library.stats").then(v=>v.items===1)','import');
    check('Import selected candidate into isolated library');
    for(let i=0;i<7;i++){
      await run('document.querySelector("button[aria-label=下一页]").click()');
      await until(`document.querySelector('.pager').textContent.includes('${i+2} / 8')`,'page');
    }
    await until('document.querySelectorAll(".ai-result-card").length===21','last page');
    assert.ok(await run('document.querySelector(".paper-title").textContent.startsWith("Paper 210")'));check('All 231 candidates accessible with correct A-Z paging');
    await change('条件筛选','excluded');
    await until('document.querySelector(".pager").textContent.includes("77")','excluded count');
    check('Mandatory-condition filter counts across all pages');
    await change('条件筛选','all');await until('document.querySelector(".pager").textContent.includes("231")','all');
    const csv=path.join(out,'matrix.csv');
    const downloaded=new Promise((resolve,reject)=>{win.webContents.session.once('will-download',(_e,item)=>{item.setSavePath(csv);item.once('done',(_e,state)=>state==='completed'?resolve():reject(Error(state)));});});
    await click('导出全部筛选结果');await Promise.race([downloaded,sleep(12000).then(()=>{throw Error('CSV download timeout');})]);
    const content=fs.readFileSync(csv,'utf8');assert.equal(content.trim().split('\r\n').length,232);check('CSV export includes all 231 rows beyond current page');
    await click('研究矩阵');await until('!!document.querySelector(".research-matrix")','matrix');await capture('02-matrix');
    check('Matrix view renders evidence-based extraction');
    await run(`(()=>{const e=document.querySelector('[aria-label="检索结果版本"]');Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,e.options[e.options.length-1].value);e.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    await until('document.querySelector(".screen-counts").textContent.includes("历史快照")','history');
    await until('document.querySelector(".research-matrix").textContent.includes("Historical fixture")','historical rows loaded');
    assert.equal(await run('document.querySelector(".research-matrix input").disabled'),true);check('Historical results frozen and read-only');
    await change('检索结果版本','');await click('卡片视图');await until('document.querySelectorAll(".ai-result-card").length===30','latest');
    await click('展开计划');await until('document.querySelectorAll(".query-editor").length===6','six editable queries');await capture('03-plan');
    await click('收起计划');win.setContentSize(1280,850);await run('document.querySelector(".detail-action").click()');await until('!!document.querySelector(".ai-evidence-panel.has-evidence")','compact evidence');await sleep(300);
    assert.equal(await run('getComputedStyle(document.querySelector(".ai-evidence-panel")).display!=="none"'),true);
    assert.equal(await run('document.documentElement.scrollWidth<=innerWidth'),true);
    assert.ok(await run('document.querySelector(".ai-evidence-panel").getBoundingClientRect().top<innerHeight'));
    await capture('04-compact');check('Compact layout preserves evidence panel without page overflow');
    assert.equal(report.console.length,0,report.console.join('\n'));check('No renderer console errors');
    report.passed=true;
  }catch(error){report.passed=false;report.error=error.stack;await capture('failure').catch(()=>{});}
  report.finishedAt=new Date().toISOString();fs.writeFileSync(path.join(out,'smoke-report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));process.exitCode=report.passed?0:1;app.quit();
};
