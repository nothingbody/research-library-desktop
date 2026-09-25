const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');

exports.run=async({win,rpc,project,app})=>{
  const out=process.env.RESEARCH_QA_OUT||path.join(project,'build/translation-layout-qa');
  fs.mkdirSync(out,{recursive:true});
  const report={passed:false,checks:[],error:null};
  const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const execute=async code=>{const result=await win.webContents.executeJavaScript(`(async()=>{try{return {ok:true,value:await (${code})}}catch(error){return {ok:false,error:String(error.stack||error)}}})()`,true);if(!result.ok)throw Error(result.error);return result.value;};
  const until=async(code,label,timeout=30000)=>{const started=Date.now();while(Date.now()-started<timeout){if(await execute(code))return;await sleep(200);}throw Error('等待超时：'+label);};
  const capture=async name=>{fs.writeFileSync(path.join(out,name+'.png'),(await win.webContents.capturePage()).toPNG());};
  try{
    const itemId=process.env.RESEARCH_QA_PAPER_ID;
    assert.ok(itemId,'需要 RESEARCH_QA_PAPER_ID');
    const item=await rpc('items.get',{id:itemId});
    assert.ok(item?.attachments?.length,'测试文献没有附件');
    win.webContents.setBackgroundThrottling(false);win.setContentSize(1690,1050);win.showInactive();
    await until('!!document.querySelector(".library-header")','文献库');
    await execute(`(()=>{const nav=[...document.querySelectorAll('.nav-item')].find(value=>value.textContent.includes('我的文献'));nav.click();})()`);
    await execute(`(()=>{const input=document.querySelector('[data-search]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(item.title)});input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    await until(`!![...document.querySelectorAll('.document-row')].find(value=>value.textContent.includes(${JSON.stringify(item.title)}))`,'测试论文');
    await execute(`(()=>{const row=[...document.querySelectorAll('.document-row')].find(value=>value.textContent.includes(${JSON.stringify(item.title)}));row.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));})()`);
    await until('!!document.querySelector(".pdf-scroll.bilingual-flow")','对照阅读');
    await execute("document.querySelectorAll('.thumbnail')[2].click()");
    await until("document.querySelector('input[aria-label=\"PDF 页码\"]')?.value==='3'",'第 3 页');
    await until("[...document.querySelectorAll('.bilingual-page-row[data-page=\"3\"] .translation-paragraph')].some(node=>node.textContent.length>20)",'本机已保存译文');
    await sleep(500);await capture('translation-page-3');
    report.checks.push('本机缓存译文在双栏阅读中显示');
    const runs=await rpc('assistant.runs',{itemId,task:'align_translation',limit:100});
    const example=runs.find(run=>{try{return Number(run.input?.page)===3 &&
      JSON.parse(String(run.input?.text||'{}')).selectedText?.startsWith('olve the problem. Luo et al.') &&
      String(run.result?.alignedTarget||'').includes('Luo');}catch{return false;}});
    if(example){
      const target=String(example.result.alignedTarget);
      report.alignment=await execute(`(()=>{const row=document.querySelector('.bilingual-page-row[data-page="3"]');const span=[...row.querySelectorAll('.continuous-page .textLayer span')].find(node=>node.textContent.includes('Luo et al.'));const paragraph=[...row.querySelectorAll('.translation-paragraph')].find(node=>node.textContent.includes(${JSON.stringify(target)}));if(!span||!paragraph)return null;const source=document.createRange(),position=span.textContent.indexOf('Luo et al.');source.setStart(span.firstChild,position);source.setEnd(span.firstChild,position+3);const matched=document.createRange(),at=paragraph.textContent.indexOf(${JSON.stringify(target)});matched.setStart(paragraph.firstChild,at);matched.setEnd(paragraph.firstChild,Math.min(at+5,paragraph.firstChild.length));return {sourceTop:source.getBoundingClientRect().top,targetTop:matched.getBoundingClientRect().top,delta:Math.abs(source.getBoundingClientRect().top-matched.getBoundingClientRect().top)}})()`);
      assert.ok(report.alignment && report.alignment.delta<28,`原文译文高亮错位 ${report.alignment?.delta}px`);
      report.checks.push('对应句子保持相近的页面高度');
    }
    report.layout=await execute(`(()=>{const row=document.querySelector('.bilingual-page-row[data-page="3"]');const page=row.querySelector('.continuous-page').getBoundingClientRect();const paragraphs=[...row.querySelectorAll('.translation-paragraph')].map(node=>{const range=document.createRange();range.selectNodeContents(node);const lines=[...range.getClientRects()].filter(r=>r.width>1).map(r=>({top:r.top,bottom:r.bottom}));const r=node.getBoundingClientRect();return {textLength:node.textContent.length,scrollHeight:node.scrollHeight,clientHeight:node.clientHeight,fontSize:getComputedStyle(node).fontSize,lineHeight:getComputedStyle(node).lineHeight,lines:lines.length,contentTop:lines.length?Math.min(...lines.map(line=>line.top)):null,contentBottom:lines.length?Math.max(...lines.map(line=>line.bottom)):null,rect:{top:r.top,bottom:r.bottom,left:r.left,right:r.right}}});return {sourceTop:page.top,paragraphs}})()`);
    for(const page of [1,2,5]){
      await execute(`document.querySelectorAll('.thumbnail')[${page-1}].click()`);
      await until(`[...document.querySelectorAll('.bilingual-page-row[data-page="${page}"] .translation-paragraph')].some(node=>node.textContent.length>20)`,`第 ${page} 页缓存译文`);
      const overflow=await execute(`(()=>{const paragraphs=[...document.querySelectorAll('.bilingual-page-row[data-page="${page}"] .translation-paragraph')].filter(node=>node.textContent.trim().length>100);return paragraphs.flatMap(node=>{const range=document.createRange();range.selectNodeContents(node);const rects=[...range.getClientRects()].filter(rect=>rect.width>1);const box=node.getBoundingClientRect();const outside=rects.filter(rect=>rect.top<box.top-2||rect.bottom>box.bottom+2);return outside.length?[{text:node.textContent.slice(0,60),chars:node.textContent.length,top:box.top,bottom:box.bottom,contentBottom:Math.max(...rects.map(rect=>rect.bottom)),font:getComputedStyle(node).fontSize,lineHeight:getComputedStyle(node).lineHeight}]:[]})})()`);
      if(overflow.length) report[`page${page}Overflow`]=overflow;
      assert.equal(overflow.length,0,`第 ${page} 页译文超出原段落区域`);
    }
    report.checks.push('其余已缓存页面译文无裁切');
    report.passed=true;
  }catch(error){report.error=String(error.stack||error).slice(0,3000);try{await capture('translation-failure');}catch{}}
  finally{fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({passed:report.passed,checks:report.checks,error:report.error}));process.exitCode=report.passed?0:1;app.quit();}
};
