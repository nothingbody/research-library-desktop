const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

exports.run = async ({win, rpc, project, app}) => {
  const out = process.env.RESEARCH_QA_OUT || path.join(project, 'docs/verification/advanced-search');
  fs.mkdirSync(out, {recursive: true});
  const report = {passed: false, assertions: [], errors: []};
  const execute = code => win.webContents.executeJavaScript(code, true);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function until(code, label) {for (let i=0;i<70;i++) {if (await execute(code)) return; await sleep(120);} throw Error('Timeout: '+label);}
  const setInput = (selector, value) => execute(`(() => {const input=document.querySelector(${JSON.stringify(selector)}); if(!input)throw Error('missing input');const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;setter.call(input,${JSON.stringify(value)});input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  win.showInactive();
  win.webContents.on('console-message', event=>{if(event.level==='error')report.errors.push(event.message);});
  try {
    await until('Boolean(document.querySelector(".library-header"))','library mount');
    const collection = await rpc('collections.edit',{action:'create',name:'高级检索验收'});
    await rpc('items.create',{collectionId:collection.id,data:{title:'Multi workshop scheduling fixture',author:'Wang, Mei',year:'2025'}});
    await rpc('items.create',{data:{title:'Unrelated biology fixture',author:'Li, Jun',year:'2024'}});
    await execute(`(() => {const button=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='高级检索');if(!button)throw Error('advanced search button missing');button.click();})()`);
    await until('Boolean(document.querySelector(".advanced-search"))','advanced modal');
    await setInput('[aria-label="条件内容"]','scheduling');
    await execute(`(() => {const label=[...document.querySelectorAll('.advanced-scope label')].find(x=>x.textContent.includes('高级检索验收'));if(!label)throw Error('collection missing');label.querySelector('input').click();})()`);
    await execute('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    await sleep(300);
    fs.writeFileSync(path.join(out,'advanced-search-form.png'),(await win.webContents.capturePage()).toPNG());
    await execute(`(() => {const button=[...document.querySelectorAll('.advanced-footer button')].find(x=>x.textContent.includes('应用筛选'));button.click();})()`);
    await until('!document.querySelector(".advanced-search")','advanced modal close');
    await until('document.querySelector(".library-content")?.textContent.includes("Multi workshop scheduling fixture")','filtered result');
    const result = await execute('window.research.call("items.list",{advanced:{op:"all",conditions:[{field:"title",operator:"contains",value:"scheduling"}]},collectionIds:['+JSON.stringify(collection.id)+']})');
    assert.equal(result.total,1);
    report.assertions.push('advanced condition and collection union applied');
    await execute(`(() => {const button=[...document.querySelectorAll('button')].find(x=>x.textContent.includes('高级检索')&&x.textContent.includes('已启用'));button.click();})()`);
    await until('Boolean(document.querySelector(".advanced-search"))','reopened advanced modal');
    await setInput('[aria-label="保存为智能集合名称"]','调度论文');
    await execute(`(() => {const button=[...document.querySelectorAll('.advanced-footer button')].find(x=>x.textContent.includes('保存规则'));button.click();})()`);
    await until('!document.querySelector(".advanced-search")','saved rule modal close');
    const saved = await rpc('smartCollections.list',{});
    assert.ok(saved.some(value=>value.name==='调度论文'));
    report.assertions.push('saved dynamic smart collection from UI');
    assert.deepEqual(report.errors,[]);
    report.passed=true;
  } catch(error){report.error=error.stack;}
  fs.writeFileSync(path.join(out,'advanced-smoke-report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify({passed:report.passed,assertions:report.assertions.length,error:report.error,out}));
  process.exitCode=report.passed?0:1;app.quit();
};
