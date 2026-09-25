const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');

exports.run=async({win,root,app,rpc})=>{
  const out=process.env.RESEARCH_QA_OUT;
  const report={library:root,startedAt:new Date().toISOString(),assertions:[],screenshots:[],console:[]};
  const run=code=>win.webContents.executeJavaScript(code,true);
  const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  async function until(code,label){for(let i=0;i<120;i++){if(await run(code))return;await sleep(100);}throw Error('Timeout: '+label);}
  async function capture(name){await sleep(180);fs.writeFileSync(path.join(out,name+'.png'),(await win.webContents.capturePage()).toPNG());report.screenshots.push(name+'.png');}
  const check=name=>report.assertions.push({name,passed:true});
  win.webContents.setBackgroundThrottling(false);win.setContentSize(1586,992);win.showInactive();
  win.webContents.on('console-message',event=>{if(event.level==='error')report.console.push(event.message);});
  try{
    await until('!!document.querySelector(".library-header")','app shell');
    await run(`[...document.querySelectorAll('.nav-item')].find(e=>e.textContent.includes('AI 文献检索')).click()`);
    await until('document.querySelectorAll(".ai-result-card").length===30','real search results');
    assert.match(await run('document.querySelector(".paper-title").textContent'),/guided vehicles/i);
    assert.ok(await run('document.querySelector(".screen-counts").textContent.includes("待核验")'));
    await capture('01-real-results');check('Real FJSP/AGV records render in evidence-first order');
    if(process.env.RESEARCH_QA_JOURNALS==='1'){
      assert.ok(await run('[...document.querySelectorAll(".journal-metric")].some(e=>/JCR Q[1-4] · IF [0-9]/.test(e.textContent))'));
      check('Exact local ISSN matches show year-labeled JCR and IF on result cards');
      await run(`[...document.querySelectorAll('.version-toolbar button')].find(e=>e.textContent.includes('研究矩阵')).click()`);
      await until('document.querySelector(".research-matrix")?.textContent.includes("本地期刊指标")','journal metric matrix');
      assert.ok(await run('document.querySelector(".research-matrix .journal-metric") !== null'));
      await capture('01-journal-matrix');check('Research matrix displays local journal metric column');
      await run(`[...document.querySelectorAll('.version-toolbar button')].find(e=>e.textContent.includes('卡片视图')).click()`);
    }
    await run(`[...document.querySelectorAll('.ai-header-actions button')].find(e=>e.textContent.includes('展开计划')).click()`);
    await until('document.querySelectorAll(".query-editor").length===4','fallback queries');
    assert.ok(await run('document.querySelector(".ai-plan-panel").textContent.includes("AGV / 运输资源")'));
    await capture('02-real-plan');check('Four concise English queries and transport criterion are visible');
    await run(`[...document.querySelectorAll('.result-select')].slice(0,20).forEach(e=>e.click())`);
    await until('document.querySelector(".result-footer").textContent.includes("已选 20")','20 selected');
    assert.equal(await run('document.querySelector(".verification-action button").disabled'),false);
    await run('document.querySelector(".verification-action").scrollIntoView()');
    await capture('03-batch-ready');check('Batch review accepts twenty explicit selections without sending a request');
    await run('document.querySelectorAll(".result-select")[20].click()');
    assert.equal(await run('document.querySelector(".verification-action button").disabled'),true);
    check('Twenty-one selections cannot start an over-limit batch');
    if(process.env.RESEARCH_QA_COLLECTIONS==='1'){
      await run(`[...document.querySelectorAll('.search-save-target button')].find(e=>e.textContent.includes('新建集合')).click()`);
      await run(`(()=>{const input=document.querySelector('input[aria-label="新集合名称"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'QA 检索集合');input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
      await run(`document.querySelector('.search-save-target button[type="submit"]').click()`);
      await until('!!document.querySelector(".search-save-target select")?.value','collection selected');
      const collectionId=await run('document.querySelector(".search-save-target select").value');
      await run('document.querySelector(".ai-result-card .save-action").click()');
      for(let i=0;i<100&&(await rpc('items.list',{collectionId})).total!==1;i++)await sleep(100);
      assert.equal((await rpc('items.list',{collectionId})).total,1);
      check('Result card saves directly to the selected collection');
      await run('document.querySelector(".paper-title").click()');
      await until('!!document.querySelector(".paper-source-overview")','source overview');
      assert.ok(await run('document.querySelector(".paper-source-overview").textContent.includes("摘要")'));
      assert.ok(await run('(()=>{const r=document.querySelector(".paper-source-overview").getBoundingClientRect();return r.width>200&&r.top<innerHeight-100&&r.bottom>100})()'));
      await capture('04-source-overview');check('Paper detail shows abstract and source scope');
      if(process.env.RESEARCH_QA_CITATIONS==='1'){
        await until('!!document.querySelector(".citation-record")','citation record');
        assert.ok(await run('document.querySelector(".citation-record").textContent.includes("关系来源：OpenAlex")'));
        assert.ok(await run('document.querySelector(".citation-record").textContent.includes("DOI")'));
        await run('document.querySelector(".citation-record").scrollIntoView({block:"center"})');
        await capture('05-citation-source');
        check('Citation relationship displays source and linked paper metadata');
      }
      await run(`[...document.querySelectorAll('.evidence-actions button')].find(e=>e.textContent.includes('保存到所选集合')).click()`);
      await until('!!document.querySelector(".library-header") && document.querySelector(".library-header").textContent.includes("QA 检索集合")','saved collection view');
      const items=(await rpc('items.list',{collectionId})).items;
      assert.equal(items.length,1);check('Paper detail opens the selected collection without duplicating the item');
      await run(`[...document.querySelectorAll('.nav-item')].find(e=>e.textContent.includes('AI 文献检索')).click()`);
      await until('document.querySelectorAll(".ai-result-card").length===30','search reopened');
      await run(`(()=>{const select=document.querySelector('select[aria-label="检索结果保存集合"]');select.value=${JSON.stringify(collectionId)};select.dispatchEvent(new Event('change',{bubbles:true}));})()`);
      await run('document.querySelector(".paper-title").click()');
      await until('!!document.querySelector(".evidence-actions")','detail reopened');
      await run(`[...document.querySelectorAll('.evidence-actions button')].find(e=>e.textContent.includes('保存到所选集合')).click()`);
      await until('!!document.querySelector(".library-header")','library reopened');
      assert.equal((await rpc('items.list',{collectionId})).total,1);
      check('Saving an existing paper again keeps one collection membership');
    }
    assert.equal(report.console.length,0,report.console.join('\n'));check('No renderer console errors');
    report.passed=true;
  }catch(error){report.passed=false;report.error=error.stack;await capture('failure').catch(()=>{});}
  report.finishedAt=new Date().toISOString();fs.writeFileSync(path.join(out,'smoke-report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));process.exitCode=report.passed?0:1;app.quit();
};
