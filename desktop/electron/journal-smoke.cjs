const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');

exports.run=async({win,rpc,root,project,app})=>{
  const out=process.env.RESEARCH_QA_OUT || path.join(project,'docs/verification/journal-details');
  fs.mkdirSync(out,{recursive:true});
  const report={startedAt:new Date().toISOString(),version:app.getVersion(),library:root,assertions:[],screenshots:[],console:[]};
  const run=code=>win.webContents.executeJavaScript(code,true), sleep=ms=>new Promise(r=>setTimeout(r,ms));
  win.webContents.setBackgroundThrottling(false);win.setContentSize(1586,992);win.showInactive();
  win.webContents.on('console-message',event=>{if(event.level==='error')report.console.push(event.message);});
  async function until(code,message){for(let i=0;i<100;i++){if(await run(code))return;await sleep(120);}throw Error('Timeout: '+message);}
  async function click(label,selector='button'){await run(`(()=>{const target=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(label)});if(!target)throw Error('Missing control: '+${JSON.stringify(label)});target.click();})()`);await sleep(160);}
  async function capture(name){await run('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');await sleep(200);fs.writeFileSync(path.join(out,name+'.png'),(await win.webContents.capturePage()).toPNG());report.screenshots.push({file:name+'.png',viewport:await run('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})')});}
  async function check(name,fn){await fn();report.assertions.push({name,passed:true});}
  async function tab(label){await click(label,'.detail-tabs button');await run('document.querySelector(".journal-detail").scrollTop=0');}
  async function showPanel(){await run('(()=>{const s=document.querySelector(".journal-detail"),p=document.querySelector(".detail-tabs");s.scrollTop+=p.getBoundingClientRect().top-s.getBoundingClientRect().top-16;})()');}
  async function search(q){await until('!!document.querySelector(".journals-page [data-search]")','search mounted');await run(`(()=>{const x=document.querySelector('.journals-page [data-search]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(x,${JSON.stringify(q)});x.dispatchEvent(new Event('input',{bubbles:true}));})()`);await until('document.querySelectorAll(".journal-card").length===1 && !document.querySelector(".journals-page .busy")','one journal');await sleep(300);await run('document.querySelector(".journal-card").click()');await until('!!document.querySelector(".journal-basics")','detail');}
  async function noRawKeys(){const rendered=await run('document.querySelector(".journal-tab-content").innerText');assert.ok(!/all_issns|built_at|has_doaj|rating_confidence|comment_text|source_id|\[object Object\]|"[a-z_]+":/.test(rendered),rendered.slice(0,1000));const values=await run('[...document.querySelectorAll(".journal-facts dd")].map(x=>x.childNodes[0]?.textContent)');assert.ok(!values.some(x=>x==='true'||x==='false'));}
  try{
    await until('!!document.querySelector(".library-header")','app');
    await click('期刊选投','.nav-item');await search('0007-9235');
    await check('CA basic information: Chinese country, year, unique publisher, no raw objects',async()=>{
      const facts=await run('[...document.querySelectorAll(".journal-basics .journal-facts>div")].map(x=>[x.querySelector("dt").textContent,x.querySelector("dd").textContent])');
      assert.equal(facts.find(f=>f[0]==='国家 / 地区')[1],'美国');assert.equal(facts.find(f=>f[0]==='创刊年份')[1],'1950 年');assert.equal(facts.filter(f=>f[0]==='出版商').length,1);assert.equal(facts.length,12);await noRawKeys();
    });
    await capture('01-ca-overview');await showPanel();await capture('02-ca-basic-information');
    await check('OA disagreement is attributed and DOAJ false is shown as not indexed',async()=>{const s=await run('document.querySelector(".journal-access").innerText');assert.match(s,/未收录/);assert.match(s,/OpenAlex/);assert.match(s,/SCImago/);assert.match(s,/差异/);});
    await tab('分区与数据源');await showPanel();
    await check('All available source pages use curated fields',async()=>{
      const names=await run('[...document.querySelectorAll(".journal-source-layout>nav button")].map(x=>x.textContent)');
      for(const name of names){await click(name,'.journal-source-layout>nav button');await noRawKeys();assert.ok(await run('document.querySelectorAll(".journal-source-panel .journal-facts>div").length>0'));}
    });
    await click('JCR','.journal-source-layout>nav button');
    await check('Historical year selector changes displayed metric and year',async()=>{
      await run(`(()=>{const x=document.querySelector('select[aria-label="数据源年份"]');x.value='2024';x.dispatchEvent(new Event('change',{bubbles:true}));})()`);
      const expected=(await rpc('journals.get',{id:13984})).sources.jcr.all_years.find(r=>r.year===2024);
      await until('document.querySelector(".journal-source-panel").textContent.includes("2024 年")','historical year');
      const number=await run(`document.querySelector('.journal-source-panel [data-fact="影响因子"] dd').textContent`);assert.equal(Number(number.replace(/,/g,'')),Number(expected.impact_factor));
    });
    await capture('03-jcr-history');await click('中科院分区','.journal-source-layout>nav button');await capture('04-cas-categories');
    await tab('投稿指南');await showPanel();
    await check('Submission shows availability and a template filename without a fabricated link',async()=>{await noRawKeys();const text=await run('document.querySelector(".journal-submission").innerText');assert.match(text,/暂未保存/);assert.match(text,/USG.cls/);assert.ok(!(await run('[...document.querySelectorAll(".journal-submission a")].some(a=>a.href.endsWith("wiley.zip"))')));});
    await capture('05-submission');await tab('作者反馈');await showPanel();
    await check('Feedback is readable and keeps sample confidence, original text and unknown outcome',async()=>{await noRawKeys();const text=await run('document.querySelector(".journal-feedback").innerText');assert.match(text,/较低/);assert.match(text,/课余时间投了一篇/);assert.match(text,/未明确/);});
    await capture('06-feedback');await tab('相关期刊与论文');await showPanel();await noRawKeys();await capture('07-related');
    await check('Reloading local data preserves the active detail tab',async()=>{await click('重读本地数据');await until('document.querySelector(".journal-tab-content")?.dataset.tab==="related"','related tab restored');});
    await tab('原始数据');
    await check('Complete original response is still available in its dedicated tab',async()=>{assert.ok(await run('document.querySelector(".json").textContent.includes("all_issns")'));});
    await tab('概览与趋势');win.setContentSize(1120,740);await sleep(200);await showPanel();
    await check('Narrow journal detail has no horizontal clipping',async()=>{assert.ok(await run('(()=>{const x=document.querySelector(".journal-detail");return x.scrollWidth<=x.clientWidth+1;})()'));});
    await capture('08-ca-narrow');win.setContentSize(1586,992);
    await click('期刊选投','.detail-breadcrumb button');await until('document.querySelectorAll(".journal-card").length>0','return');
    const sparse=await rpc('journals.get',{id:26583});
    await search(sparse.unified.print_issn || sparse.unified.electronic_issn);
    await check('A different journal with missing fields has explicit empty values',async()=>{await noRawKeys();assert.ok(await run('document.querySelectorAll(".journal-basics .unavailable").length>0'));});
    await showPanel();await capture('09-missing-fields');
    report.finishedAt=new Date().toISOString();report.passed=true;
  }catch(error){report.passed=false;report.error=error.stack;await capture('failure').catch(()=>{});}
  fs.writeFileSync(path.join(out,'journal-smoke-report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));process.exitCode=report.passed?0:1;app.quit();
};
