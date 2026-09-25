const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');

exports.run=async({win,rpc,project,app})=>{
  const out=process.env.RESEARCH_QA_OUT||path.join(project,'docs/verification/workflow-smoke');
  fs.mkdirSync(out,{recursive:true});
  const report={passed:false,checks:[],screenshots:[],error:null};
  const execute=code=>win.webContents.executeJavaScript(code,true);
  const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const until=async(code,label,ms=12000)=>{const start=Date.now();while(Date.now()-start<ms){if(await execute(code))return;await sleep(150);}throw Error('Timeout: '+label+'\n'+await execute('document.body.innerText.slice(-1000)'));};
  const check=(label,value)=>{assert.ok(value,label);report.checks.push(label);};
  try{
    win.setContentSize(1580,980);win.showInactive();
    await until('!!document.querySelector(".library-header")','library');
    const names=['Workflow QA A','Workflow QA B'];
    for(const name of names){const existing=(await rpc('items.list',{q:name,limit:20})).items.find(item=>item.title===name);if(!existing)await rpc('items.create',{data:{title:name,type:'article-journal',abstract:'Continuous reading and comparison fixture'}});}
    const ids=[];
    for(const name of names){const item=(await rpc('items.list',{q:name,limit:20})).items.find(value=>value.title===name);let detail=await rpc('items.get',{id:item.id});if(!detail.attachments?.length){const preview=await rpc('_imports.preview',{paths:[path.join(project,'docs/verification/runtime/verification.pdf')],itemId:item.id});const job=await rpc('imports.commit',{batchId:preview.batchId});for(let i=0;i<100;i++){const state=(await rpc('jobs.list')).find(value=>value.id===job.jobId)?.state;if(state==='completed')break;if(state==='failed')throw Error('PDF import failed');await sleep(100);}detail=await rpc('items.get',{id:item.id});}ids.push(detail.attachments[0].id);}
    const open=async(name)=>{await execute('document.querySelector(".workspace-tabs>button").click()');await execute(`(()=>{const input=document.querySelector('[data-search]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(name)});input.dispatchEvent(new Event('input',{bubbles:true}));})()`);await until(`!![...document.querySelectorAll('.document-row')].find(row=>row.textContent.includes(${JSON.stringify(name)}))`,name+' row');await execute(`(()=>{const row=[...document.querySelectorAll('.document-row')].find(value=>value.textContent.includes(${JSON.stringify(name)}));row.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));})()`);await until('!!document.querySelector(".pdf-scroll .continuous-page")',name+' continuous PDF',20000);await execute(`document.querySelector('button[aria-label="切换连续阅读"]').click()`);await until('!!document.querySelector(".pdf-paper canvas") && document.querySelector(".pdf-paper canvas").width>100',name+' PDF',20000);};
    await open(names[0]);await open(names[1]);
    check('Two PDF tabs',await execute('document.querySelectorAll(".pdf-tab").length===2'));
    await execute(`(()=>{const select=document.querySelector('select[aria-label="双文献对照"]');select.value=${JSON.stringify(ids[0])};select.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    await until('document.querySelectorAll(".reader-compare-grid .reader").length===2','side-by-side PDFs');
    check('Independent PDF panes',await execute(`document.querySelectorAll('.reader-compare-grid .reader-toolbar input[aria-label="PDF 页码"]').length===2`));
    await execute(`document.querySelectorAll('button[aria-label="切换连续阅读"]')[0].click()`);
    await until('document.querySelectorAll(".reader-compare-grid .continuous-page").length>1','continuous pages');
    await until('document.querySelectorAll(".reader-compare-grid .reader")[0]?.querySelector(".continuous-page .textLayer span") && document.querySelectorAll(".reader-compare-grid .reader")[0]?.querySelector(".continuous-page canvas")?.width>100 && document.querySelectorAll(".reader-compare-grid .reader")[1]?.querySelector(".continuous-page canvas")?.width>100','both PDF canvases',20000);
    await sleep(800);
    check('Continuous PDF rendered',true);
    const image=await win.webContents.capturePage();fs.writeFileSync(path.join(out,'workflow-reader.png'),image.toPNG());report.screenshots.push('workflow-reader.png');
    await execute('document.querySelector(".workspace-tabs>button").click()');
    await execute(`(()=>{const button=[...document.querySelectorAll('.nav-item')].find(value=>value.textContent.includes('多文献问答'));button.click();})()`);
    await until('!!document.querySelector(".research-ask")','multi-paper QA');check('Research Q&A page',true);
    await execute(`(()=>{const button=[...document.querySelectorAll('.nav-item')].find(value=>value.textContent.includes('研究整理'));button.click();})()`);
    await until('!!document.querySelector(".research-workspace")','organization');check('Research organization page',true);
    report.passed=true;
  }catch(error){report.error=error.stack;}
  fs.writeFileSync(path.join(out,'workflow-smoke-report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));process.exitCode=report.passed?0:1;app.quit();
};
