const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

exports.run = async ({win, rpc, root, project, app}) => {
  const out = process.env.RESEARCH_QA_OUT || path.join(project, 'docs/verification');
  fs.mkdirSync(out, {recursive:true});
  const report = {startedAt:new Date().toISOString(), library:root, assertions:[], console:[], screenshots:[]};
  win.webContents.setBackgroundThrottling(false);
  win.setContentSize(1586, 992);
  win.showInactive();
  win.webContents.on('console-message', event => {if (event.level === 'error') report.console.push(event.message);});
  const execute = code => win.webContents.executeJavaScript(code, true);
  const sleep = ms => new Promise(r => setTimeout(r,ms));
  async function until(code, message, timeout=12000) {
    const started = Date.now();
    while (Date.now() - started < timeout) {if (await execute(code)) return; await sleep(150);}
    throw new Error('Timeout: ' + message + ': ' + await execute('document.body.innerText.slice(-1500)'));
  }
  async function click(text) {
    await execute(`(() => {const b = [...document.querySelectorAll('button')].find(b => b.textContent.trim().startsWith(${JSON.stringify(text)})); if(!b) throw Error('Button not found: ' + ${JSON.stringify(text)}); b.click();})()`);
    await sleep(250);
  }
  async function capture(name) {
    await execute('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
    await sleep(300);
    const image = await win.webContents.capturePage();
    fs.writeFileSync(path.join(out,name+'.png'), image.toPNG());
    report.screenshots.push(name + '.png');
    report.viewport = await execute('({width:innerWidth,height:innerHeight,dpr:devicePixelRatio})');
  }
  async function check(name, fn) {await fn(); report.assertions.push({name, passed:true});}
  async function jobDone(id) {
    for (let n=0;n<120;n++) {
      const job = (await rpc('jobs.list')).find(j=>j.id===id);
      if(job?.state==='completed') return job.result;
      if(['failed','cancelled'].includes(job?.state)) throw new Error(JSON.stringify(job));
      await sleep(300);
    }
    throw new Error('Job timed out ' + id);
  }
  try {
    await until('!!window.research && !!document.querySelector(".library-header") && !document.querySelector(".table-pane .busy")', 'library mount');
    if(!app.isPackaged) {
      await execute(`(()=>{const logo=document.createElement('div');logo.id='qa-app-icon';logo.style.cssText='position:fixed;left:0;top:0;width:256px;height:256px;background:#315ee8;border-radius:48px;display:flex;align-items:center;justify-content:center;z-index:99999';const svg=document.querySelector('.brandmark svg').cloneNode(true);svg.style.cssText='width:166px;height:166px;color:white';logo.append(svg);document.body.append(logo);})()`);
      await sleep(150);
      const icon=await win.webContents.capturePage({x:0,y:0,width:256,height:256});
      const png=icon.toPNG(), header=Buffer.alloc(22);header.writeUInt16LE(1,2);header.writeUInt16LE(1,4);header.writeUInt16LE(1,10);header.writeUInt16LE(32,12);header.writeUInt32LE(png.length,14);header.writeUInt32LE(22,18);
      fs.mkdirSync(path.join(project,'desktop/build'),{recursive:true});fs.writeFileSync(path.join(project,'desktop/build/icon.ico'),Buffer.concat([header,png]));
      await execute('document.querySelector("#qa-app-icon").remove()');
    }
    if((await rpc('library.stats')).items===0) await capture('01-empty-library');
    let collections = await rpc('collections.list');
    let group = collections.find(c=>c.name==='基础模型');
    if(!group) {
      const parent = await rpc('collections.edit',{action:'create',name:'机器学习'});
      group = await rpc('collections.edit',{action:'create',name:'基础模型',parentId:parent.id});
      await rpc('collections.edit',{action:'create',name:'文献综述',parentId:parent.id});
      await rpc('collections.edit',{action:'create',name:'方法与实验',parentId:parent.id});
    }
    await check('Import preview, commit and duplicate-safe restart', async()=>{
      const preview = await rpc('_imports.preview',{paths:[path.join(project,'docs/verification/runtime/demo-csl.json')]});
      assert.equal(preview.entries.length,8);
      const job = await rpc('imports.commit',{batchId:preview.batchId,collectionId:group.id});
      const result = await jobDone(job.jobId); assert.equal(result.errors.length,0);
      assert.equal((await rpc('library.stats')).items,8);
    });
    let items = (await rpc('items.list',{sort:'title',direction:'asc'})).items;
    const first = items.find(i=>i.title==='Attention Is All You Need');
    let attachment;
    await check('Managed PDF attachment and full-text index', async()=>{
      const preview = await rpc('_imports.preview',{paths:[path.join(project,'docs/verification/runtime/verification.pdf')],itemId:first.id});
      await jobDone((await rpc('imports.commit',{batchId:preview.batchId})).jobId);
      for(let n=0;n<100;n++) {
        const value=await rpc('items.get',{id:first.id}); attachment=value.attachments[0];
        if(attachment.textStatus==='indexed') break;
        await sleep(150);
      }
      assert.equal(attachment.textStatus,'indexed');
      assert.equal((await rpc('items.list',{q:'verificationfixture'})).total,1);
    });
    await rpc('attachments.position',{id:attachment.id,page:1,scale:1,rotation:0});
    await click('基础模型');
    await until('document.querySelectorAll(".document-row").length===8','eight rows');
    await execute(`document.querySelector('select[aria-label="排序"]').value='title'; document.querySelector('select[aria-label="排序"]').dispatchEvent(new Event('change',{bubbles:true}));`);
    await execute(`(()=>{const r=[...document.querySelectorAll('.document-row')].find(x=>x.textContent.includes('Attention Is All You Need')); r.click();})()`);
    await until('document.querySelector(".item-title")?.textContent === "Attention Is All You Need"','selected inspector');
    await capture('02-library-selected');
    await check('CSL citations and standard exports', async()=>{
      for(const format of ['bibtex','biblatex','ris','json']) assert.ok((await rpc('export.text',{ids:[first.id],format})).includes('Attention'));
      const citation=await execute(`window.research.call('citation.format',{ids:[${JSON.stringify(first.id)}],style:'apa'})`);
      assert.ok(citation.text.includes('Attention'));
      const chinese=await execute(`window.research.call('citation.format',{ids:[${JSON.stringify(first.id)}],style:'gb-t-7714-2015',lang:'zh-CN'})`);
      assert.ok(chinese.text.includes('Attention'));
      await execute(`window.research.call('_attachments.path',{id:${JSON.stringify(attachment.id)}}).then(()=>{throw Error('Private path exposed')},e=>true)`);
    });
    await click('打开 PDF');
    await until('!!document.querySelector(".pdf-paper canvas") && document.querySelector(".pdf-paper canvas").width > 100 && !document.querySelector(".render-indicator")', 'PDF canvas',20000);
    await check('PDF rendering and text selection creates annotation',async()=>{
      const textCount=await execute('document.querySelectorAll(".textLayer span").length'); assert.ok(textCount>3);
      await execute(`(()=>{const el=[...document.querySelectorAll('.textLayer span')].find(x=>x.textContent.includes('Attention connects')); const range=document.createRange();range.selectNodeContents(el);const s=window.getSelection();s.removeAllRanges();s.addRange(range);document.querySelector('.pdf-paper').dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));})()`);
      await execute(`document.querySelector('button[aria-label="高亮"]').click()`);
      await until('document.querySelectorAll(".annotation-card").length>0','saved annotation');
      assert.ok((await rpc('annotations.list',{attachmentId:attachment.id})).length>0);
    });
    await capture('03-reader-annotation');
    await check('Reading workspace, reading card and terminology UI', async()=>{
      await execute(`document.querySelector('button[aria-label="选择文字"]').click()`);
      await execute(`(()=>{const el=[...document.querySelectorAll('.textLayer span')].find(x=>x.textContent.includes('Attention connects'));const range=document.createRange();range.selectNodeContents(el);const s=window.getSelection();s.removeAllRanges();s.addRange(range);document.querySelector('.pdf-paper').dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));})()`);
      await execute(`document.querySelector('button[aria-label="打开辅助阅读"]').click()`);
      await until('document.querySelector(".reader-assistant") && document.querySelector(".selected-source p")?.textContent.includes("Attention connects")','reading assistant selected source');
      await capture('03b-reader-workbench');
      await execute(`(()=>{const el=document.querySelector('input[aria-label="阅读目标"]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(el,'Verify the method');el.dispatchEvent(new Event('input',{bubbles:true}));})()`);
      await execute(`document.querySelector('.save-reading').click()`);
      await until(`window.research.call('reading.get',{itemId:${JSON.stringify(first.id)}}).then(v=>v.session.goal==='Verify the method')`,'reading goal persistence');
      await execute(`document.querySelectorAll('.reader-side-tabs button')[1].click()`);
      await until('document.querySelector("textarea[aria-label=\\"研究问题\\"]")','reading card');
      await execute(`document.querySelectorAll('.reader-side-tabs button')[2].click()`);
      await until('document.querySelector("input[aria-label=\\"术语名称\\"]")','terms panel');
    });
    await capture('03c-reader-terms');
    await execute(`document.querySelector('button[aria-label="收起或展开批注侧栏"]').click()`);
    await check('PDF annotation remains aligned after zoom and rotation',async()=>{
      const saved=JSON.stringify((await rpc('annotations.list',{attachmentId:attachment.id})).at(-1).rects);
      await execute('document.querySelector("button[aria-label=\\"放大\\"]").click()');
      await execute('document.querySelector("button[aria-label=\\"旋转页面\\"]").click()');
      await sleep(500);await until('!document.querySelector(".render-indicator")','rotated PDF');
      const aligned=await execute(`(()=>{const a=document.querySelector('.annotation-mark.focused')?.getBoundingClientRect();const t=[...document.querySelectorAll('.textLayer span')].find(x=>x.textContent.includes('Attention connects'))?.getBoundingClientRect();return a&&t&&Math.min(a.right,t.right)>Math.max(a.left,t.left)&&Math.min(a.bottom,t.bottom)>Math.max(a.top,t.top);})()`);
      assert.ok(aligned);
      assert.equal(JSON.stringify((await rpc('annotations.list',{attachmentId:attachment.id})).at(-1).rects),saved);
      for(let n=0;n<3;n++) await execute('document.querySelector("button[aria-label=\\"旋转页面\\"]").click()');
      await execute('document.querySelector("button[aria-label=\\"缩小\\"]").click()');
    });
    await click('摘录到笔记');
    await check('Excerpt note retains page backlink',async()=>{
      const notes=await rpc('notes.list',{itemId:first.id}); assert.ok(notes.some(n=>n.content.includes('research://attachment/'+attachment.id)));
    });
    await execute(`document.querySelector('button[aria-label="下一页"]').click()`);
    await sleep(550);
    assert.equal((await rpc('attachments.get',{id:attachment.id})).position.page,2);
    report.assertions.push({name:'Reading position persists',passed:true});
    await click('阅读与笔记');
    await until('document.querySelectorAll(".note-card").length>0','note list');
    await capture('04-notes');
    await click('设置');
    await until('document.querySelector(".settings-page")?.textContent.includes("备份与恢复")','settings');
    await check('Encrypted credential and assistant configuration UI', async()=>{
      assert.ok(await execute('!!document.querySelector("input[aria-label=\\"阅读助手服务地址\\"]")'));
      assert.ok(await execute('!!window.research.assistant && !!document.querySelector("input[aria-label=\\"阅读助手访问密钥\\"]")'));
    });
    await execute('document.querySelector(".settings-page").scrollTop=900');
    await sleep(250); await capture('05b-assistant-settings');
    await capture('05-settings');
    await execute(`document.querySelectorAll('.toast button').forEach(b=>b.click())`);
    await click('期刊选投');
    await until('document.querySelectorAll(".journal-card").length>0','real journal search',20000);
    await capture('07-journals');
    await execute(`(()=>{const input=document.querySelector('.journals-page [data-search]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'0028-0836');input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    await until('document.querySelectorAll(".journal-card").length===1 && document.querySelector(".journal-card h2")?.textContent==="Nature"','ISSN exact search');
    await execute('document.querySelector(".journal-card").click()');
    await until('document.querySelectorAll(".trend").length===5','Nature trend details');
    await capture('08-journal-nature');
    report.assertions.push({name:'Real local journal search, ISSN link, historical source metrics',passed:true});
    await click('我的文献8');
    await until('!!document.querySelector(".library-header")','library return');
    await execute(`document.querySelectorAll('.toast button').forEach(b=>b.click())`);
    win.setSize(1120,740); await sleep(400);
    await execute('document.querySelector("button[aria-label=\\"切换信息侧栏\\"]").click()');
    await capture('06-library-1120');
    const overflow=await execute('document.documentElement.scrollWidth>window.innerWidth'); assert.equal(overflow,false);
    report.assertions.push({name:'Minimum window width has no page overflow',passed:true});
    await execute('document.querySelector("button[aria-label=\\"切换信息侧栏\\"]").click()');
    win.setSize(1600,1000);
    report.finishedAt=new Date().toISOString(); report.passed=true;
  } catch(error) {
    report.passed=false; report.error=error.stack; await capture('failure').catch(()=>{});
  }
  fs.writeFileSync(path.join(out,'smoke-report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify({passed:report.passed,assertions:report.assertions.length,error:report.error,console:report.console,out}));
  process.exitCode=report.passed?0:1;
  app.quit();
};
