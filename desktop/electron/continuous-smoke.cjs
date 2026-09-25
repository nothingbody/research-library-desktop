const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

exports.run = async ({win, rpc, project, app}) => {
  const out = process.env.RESEARCH_QA_OUT || path.join(project, 'build/continuous-qa');
  const fixture = process.env.RESEARCH_QA_PDF;
  fs.mkdirSync(out, {recursive: true});
  const report = {version: app.getVersion(), passed: false, screenshots: [], checks: []};
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const execute = async code => {
    const result = await win.webContents.executeJavaScript(`(async()=>{try{return {ok:true,value:await (${code})}}catch(error){return {ok:false,error:String(error.stack||error)}}})()`, true);
    if (!result.ok) throw new Error(result.error);
    return result.value;
  };
  const until = async (code, label, timeout = 30000) => {
    const started = Date.now();
    while (Date.now() - started < timeout) {
      try {
        if (await execute(code)) return;
      } catch (error) {
        throw new Error(`${label}: ${error.message}; script=${code}`);
      }
      await sleep(200);
    }
    throw new Error('等待超时：' + label);
  };
  const check = (label, value) => {assert.ok(value, label); report.checks.push(label);};
  const capture = async name => {
    await sleep(300);
    fs.writeFileSync(path.join(out, name + '.png'), (await win.webContents.capturePage()).toPNG());
    report.screenshots.push(name + '.png');
  };
  try {
    assert.ok(fixture && fs.existsSync(fixture), '需要 RESEARCH_QA_PDF 指向多页 PDF');
    // Import detection uses the source filename, so keep a .pdf extension even
    // when the shared fixture lives in the content-addressed object store.
    const namedFixture = path.join(out, 'continuous-fixture.pdf');
    fs.copyFileSync(fixture, namedFixture);
    win.webContents.setBackgroundThrottling(false);
    win.setContentSize(1586, 992);
    win.showInactive();
    const title = 'Continuous reading QA';
    let item = (await rpc('items.list', {q: title, limit: 10})).items.find(value => value.title === title);
    if (!item) item = await rpc('items.create', {data: {title, type: 'article-journal'}});
    let detail = await rpc('items.get', {id: item.id});
    if (!detail.attachments.length) {
      const preview = await rpc('_imports.preview', {paths: [namedFixture], itemId: item.id});
      const job = await rpc('imports.commit', {batchId: preview.batchId});
      let state;
      for (let n = 0; n < 120; n++) {
        state = (await rpc('jobs.list')).find(value => value.id === job.jobId)?.state;
        if (state === 'completed' || state === 'failed') break;
        await sleep(150);
      }
      assert.equal(state, 'completed', '多页 PDF 导入失败');
      for (let n = 0; n < 150; n++) {
        detail = await rpc('items.get', {id: item.id});
        if (detail.attachments?.[0]?.pageCount >= 3) break;
        await sleep(200);
      }
    }
    const attachment = detail.attachments[0];
    assert.ok(attachment.pageCount >= 3, '测试 PDF 至少需要三页');
    await until('!!document.querySelector(".library-header")', '文献库界面');
    await execute(`(()=>{const nav=[...document.querySelectorAll('.nav-item')].find(value=>value.textContent.includes('我的文献'));nav.click();})()`);
    await until(`!![...document.querySelectorAll('.document-row')].find(value=>value.textContent.includes(${JSON.stringify(title)}))`, '测试文献');
    await execute(`(()=>{const row=[...document.querySelectorAll('.document-row')].find(value=>value.textContent.includes(${JSON.stringify(title)}));row.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));})()`);
    await until('!!document.querySelector(".pdf-scroll.bilingual-flow")', '默认连续对照阅读');
    check('默认原文与译文连续对照', await execute(`document.querySelectorAll('.bilingual-page-row').length===${attachment.pageCount}`));
    await until('document.querySelector(".bilingual-page-row .continuous-page canvas")?.width>100 && document.querySelector(".bilingual-page-row .translated-scroll.embedded canvas")?.width>100', '首行双页渲染', 45000);
    if (process.env.RESEARCH_QA_SIDEBAR) {
      const sidebarWidth=await execute(`(()=>{const sidebar=document.querySelector('.app-body>.sidebar'),reader=document.querySelector('.reader');return {hidden:getComputedStyle(sidebar).display==='none',reader:reader.getBoundingClientRect().width}})()`);
      check('进入阅读时默认收起应用主导航',sidebarWidth.hidden);
      await capture('reader-sidebar-collapsed');
      await execute('document.querySelector("button[aria-label=\\"展开主导航栏\\"]").click()');
      await until(`getComputedStyle(document.querySelector('.app-body>.sidebar')).display!=='none'`,'展开应用主导航');
      const expandedWidth=await execute('document.querySelector(".reader").getBoundingClientRect().width');
      check('展开导航后仍保持 PDF 页面',sidebarWidth.reader-expandedWidth>150&&await execute('document.querySelector("input[aria-label=\\"PDF 页码\\"]")?.value==="1"'));
      await capture('reader-sidebar-expanded');
      await execute('document.querySelector("button[aria-label=\\"收起主导航栏\\"]").click()');
      await until(`getComputedStyle(document.querySelector('.app-body>.sidebar')).display==='none'`,'再次收起应用主导航');
      await execute('document.querySelector("button[aria-label=\\"收起页面缩略图和目录\\"]").click()');
      await until('getComputedStyle(document.querySelector(".reader-nav")).display==="none"','收起 PDF 缩略图');
      check('PDF 缩略图可独立折叠',await execute('!!document.querySelector("button[aria-label=\\"展开页面缩略图和目录\\"]")'));
      await execute('document.querySelector("button[aria-label=\\"展开页面缩略图和目录\\"]").click()');
      await until('getComputedStyle(document.querySelector(".reader-nav")).display!=="none"','恢复 PDF 缩略图');
      await execute('document.querySelector(".workspace-tabs>button").click()');
      await until('getComputedStyle(document.querySelector(".app-body>.sidebar")).display!=="none"','离开阅读后恢复主导航');
      await execute('document.querySelector(".pdf-tab>button").click()');
      await until('getComputedStyle(document.querySelector(".app-body>.sidebar")).display==="none"','返回阅读后保持折叠偏好');
      check('导航折叠仅作用于阅读且记住偏好',true);
      report.passed=true;
      return;
    }
    await capture('continuous-first-page');
    const targetPage=Number(process.env.RESEARCH_QA_SELECTION_PAGE)||2;
    await execute(`(()=>{const pane=document.querySelector('.pdf-scroll');const row=pane.querySelectorAll('.bilingual-page-row')[${targetPage-1}];pane.scrollTop+=row.getBoundingClientRect().top-pane.getBoundingClientRect().top;})()`);
    await until(`document.querySelector('input[aria-label="PDF 页码"]')?.value==='${targetPage}'`, '自然滚动到目标页');
    await until(`!!document.querySelectorAll('.bilingual-page-row')[${targetPage-1}]?.querySelector('.translated-scroll.embedded canvas')`, '目标页译文视图');
    if (process.env.RESEARCH_QA_READER_ACTIONS) {
      await until(`document.querySelectorAll('.bilingual-page-row')[${targetPage-1}]?.querySelectorAll('.continuous-page .textLayer span').length>20`, '目标页文字层');
      const target=await execute(`(()=>{const spans=[...document.querySelectorAll('.bilingual-page-row')[${targetPage-1}].querySelectorAll('.continuous-page .textLayer span')];const span=spans.find(node=>{const r=node.getBoundingClientRect();return node.textContent.trim().length>50&&r.width>200&&r.top>220&&r.bottom<800});if(!span)throw Error('找不到文字行');const r=span.getBoundingClientRect();return {start:{x:Math.round(r.left+r.width*.12),y:Math.round((r.top+r.bottom)/2)},end:{x:Math.round(r.left+r.width*.55),y:Math.round((r.top+r.bottom)/2)}}})()`);
      const drag=async()=>{
        await execute('window.getSelection()?.removeAllRanges()');
        win.webContents.sendInputEvent({type:'mouseMove',...target.start});
        win.webContents.sendInputEvent({type:'mouseDown',...target.start,button:'left',clickCount:1});
        for(let n=1;n<=7;n++)win.webContents.sendInputEvent({type:'mouseMove',x:Math.round(target.start.x+(target.end.x-target.start.x)*n/7),y:target.start.y,button:'left'});
        win.webContents.sendInputEvent({type:'mouseUp',...target.end,button:'left'});
        await until('!!document.querySelector(".reader-selection-actions")', '选中文字操作栏');
      };
      await drag();
      await capture('reader-selection-actions');
      const position=await execute(`(()=>{const bar=document.querySelector('.reader-selection-actions').getBoundingClientRect(),rect=window.getSelection().getRangeAt(0).getBoundingClientRect();return {barTop:bar.top,barBottom:bar.bottom,selectionTop:rect.top}})()`);
      check('选区操作栏靠近文字且不遮挡选区',position.barBottom<=position.selectionTop-4&&position.selectionTop-position.barBottom<55);
      await execute('document.querySelector(".pdf-scroll").scrollTop+=65');
      await until(`document.querySelector('.reader-selection-actions').getBoundingClientRect().top<${position.barTop-40}`,'选区操作栏跟随滚动');
      await execute('document.querySelector(".pdf-scroll").scrollTop-=65');
      const selected=await execute('window.getSelection()?.toString()');
      check('实际拖选后显示批注与笔记操作',selected.length>5&&await execute('!!document.querySelector("button[aria-label=\\"批注所选文字\\"]")&&!!document.querySelector("button[aria-label=\\"将所选文字写入笔记\\"]")'));
      const annotationCount=(await rpc('annotations.list',{attachmentId:attachment.id})).length;
      await execute('document.querySelector("button[aria-label=\\"批注所选文字\\"]").click()');
      await until('!!document.querySelector("textarea[aria-label=\\"批注内容\\"]")','批注编辑框');
      check('批注弹窗自动聚焦输入框',await execute('document.activeElement?.getAttribute("aria-label")==="批注内容"'));
      await execute('document.querySelector("textarea[aria-label=\\"批注内容\\"]").focus()');
      win.webContents.insertText('测试批注：需要复核这段方法。');
      await execute(`(()=>{const button=[...document.querySelectorAll('button')].find(node=>node.textContent==='保存批注');button.click()})()`);
      await until(`!document.querySelector('textarea[aria-label="批注内容"]')`, '批注保存');
      const annotations=await rpc('annotations.list',{attachmentId:attachment.id});
      check('批注保存原文、页码和评论',annotations.length===annotationCount+1&&annotations.at(-1)?.quote?.trim()===selected.trim()&&annotations.at(-1)?.comment?.includes('测试批注'));
      await drag();
      await execute('document.querySelector("button[aria-label=\\"将所选文字写入笔记\\"]").click()');
      await until('!!document.querySelector("textarea[aria-label=\\"笔记内容\\"]")','所选文字笔记编辑框');
      check('笔记弹窗自动聚焦标题',await execute('document.activeElement?.getAttribute("aria-label")==="笔记标题"'));
      check('笔记预填原文摘录与返回链接',await execute(`(()=>{const value=document.querySelector('textarea[aria-label="笔记内容"]').value;return value.includes(${JSON.stringify(selected.trim())})&&value.includes('research://attachment/')})()`));
      await execute(`(()=>{const button=[...document.querySelectorAll('button')].find(node=>node.textContent==='保存笔记');button.click()})()`);
      await until('!document.querySelector("textarea[aria-label=\\"笔记内容\\"]")','笔记保存');
      const notes=await rpc('notes.list',{itemId:item.id});
      check('阅读笔记保存到当前文献',notes.some(note=>note.content.includes(selected.trim())&&note.content.includes(`research://attachment/${attachment.id}?page=${targetPage}`)));
      await drag();
      win.webContents.sendInputEvent({type:'keyDown',keyCode:'Escape'});
      win.webContents.sendInputEvent({type:'keyUp',keyCode:'Escape'});
      await until('!document.querySelector(".reader-selection-actions")','Esc 取消文字选择');
      check('Esc 可直接退出选区操作',true);
      const before=await execute(`(()=>{const pane=document.querySelector('.pdf-scroll'),page=pane.querySelector('.bilingual-page-row[data-page="${targetPage}"] .continuous-page'),r=page.getBoundingClientRect();return {width:r.width,x:r.left+r.width*.5,y:r.top+r.height*.35,scrollLeft:pane.scrollLeft,scrollWidth:pane.scrollWidth,clientWidth:pane.clientWidth,zoom:document.querySelector('.reader-toolbar button:not(.icon)')?.textContent}})()`);
      report.widthChain=await execute(`(()=>{let node=document.querySelector('.pdf-scroll');const values=[];while(node&&values.length<7){const r=node.getBoundingClientRect();values.push({class:node.className,width:r.width,clientWidth:node.clientWidth,scrollWidth:node.scrollWidth});node=node.parentElement}return values})()`);
      const wheel=await execute(`(()=>{const point=${JSON.stringify(before)};const node=document.elementFromPoint(point.x,point.y),pane=document.querySelector('.pdf-scroll');const event=new WheelEvent('wheel',{bubbles:true,cancelable:true,ctrlKey:true,deltaY:-100,clientX:point.x,clientY:point.y});node.dispatchEvent(event);return {prevented:event.defaultPrevented,target:node?.className,within:pane.contains(node),x:point.x,y:point.y}})()`);
      report.zoomWheel=wheel;
      check('Ctrl 加滚轮拦截浏览器缩放',wheel.prevented);
      await until(`document.querySelector('.bilingual-page-row[data-page="${targetPage}"] .continuous-page').getBoundingClientRect().width>${before.width*1.08}`,'PDF 放大');
      const after=await execute(`(()=>{const pane=document.querySelector('.pdf-scroll'),r=pane.querySelector('.bilingual-page-row[data-page="${targetPage}"] .continuous-page').getBoundingClientRect();return {width:r.width,x:r.left+r.width*.5,y:r.top+r.height*.35,scrollLeft:pane.scrollLeft,scrollWidth:pane.scrollWidth,clientWidth:pane.clientWidth}})()`);
      report.zoomAnchor={before,after};
      check('缩放保持鼠标附近阅读位置',Math.abs(after.x-before.x)<12&&Math.abs(after.y-before.y)<12);
      await execute(`(()=>{const point=${JSON.stringify(after)};document.elementFromPoint(point.x,point.y).dispatchEvent(new WheelEvent('wheel',{bubbles:true,cancelable:true,ctrlKey:true,deltaY:100,clientX:point.x,clientY:point.y}))})()`);
      await until(`document.querySelector('.bilingual-page-row[data-page="${targetPage}"] .continuous-page').getBoundingClientRect().width<${before.width*1.03}`,'PDF 缩小');
      await execute('document.querySelector("button[aria-label=\\"切换连续阅读\\"]").click()');
      await until('!!document.querySelector(".pdf-paper canvas")?.width','单页阅读渲染');
      const single=await execute(`(()=>{const pane=document.querySelector('.pdf-scroll'),paper=document.querySelector('.pdf-paper'),p=pane.getBoundingClientRect(),r=paper.getBoundingClientRect();return {width:r.width,x:Math.min(p.right-50,Math.max(p.left+50,r.left+r.width*.3)),y:Math.min(p.bottom-50,Math.max(p.top+50,r.top+r.height*.3))}})()`);
      await execute(`(()=>{const point=${JSON.stringify(single)};document.elementFromPoint(point.x,point.y).dispatchEvent(new WheelEvent('wheel',{bubbles:true,cancelable:true,ctrlKey:true,deltaY:-100,clientX:point.x,clientY:point.y}))})()`);
      await until(`document.querySelector('.pdf-paper').getBoundingClientRect().width>${single.width*1.08}`,'单页 PDF 放大');
      check('单页模式也支持 Ctrl 加滚轮缩放',true);
      report.passed=true;
      return;
    }
    if (process.env.RESEARCH_QA_SELECTION_SWEEP) {
      await until(`document.querySelectorAll('.bilingual-page-row')[${targetPage-1}]?.querySelectorAll('.continuous-page .textLayer span').length>20`,'目标页文字层');
      const candidates=await execute(`(()=>[...document.querySelectorAll('.bilingual-page-row')[${targetPage-1}].querySelectorAll('.continuous-page .textLayer span')].map(span=>{const r=span.getBoundingClientRect();return {text:span.textContent,left:r.left,right:r.right,top:r.top,bottom:r.bottom}}).filter(value=>value.text.trim().length>35&&value.right-value.left>200&&value.top>220&&value.bottom<880).slice(0,12))()`);
      report.visibleSpans=await execute(`(()=>[...document.querySelectorAll('.bilingual-page-row')[${targetPage-1}].querySelectorAll('.continuous-page .textLayer span')].map(span=>{const r=span.getBoundingClientRect();return {text:span.textContent?.slice(0,100),left:Math.round(r.left),right:Math.round(r.right),top:Math.round(r.top),bottom:Math.round(r.bottom)}}).filter(value=>value.text?.trim()&&value.top>150&&value.bottom<950).slice(0,100))()`);
      const results=[];
      for(let i=0;i<candidates.length;i++)for(const margin of [-20,0])for(const dy of [-5,0,5])for(const endDy of [-7,0,7]){
        const candidate=candidates[i],mid=(candidate.top+candidate.bottom)/2;
        const start={x:Math.round(candidate.left+margin),y:Math.round(mid+dy)};
        const end={x:Math.round(candidate.left+(candidate.right-candidate.left)*.72),y:Math.round(mid+endDy)};
        await execute('window.getSelection()?.removeAllRanges()');
        win.webContents.sendInputEvent({type:'mouseMove',...start});
        win.webContents.sendInputEvent({type:'mouseDown',...start,button:'left',clickCount:1});
        for(let n=1;n<=6;n++)win.webContents.sendInputEvent({type:'mouseMove',x:Math.round(start.x+(end.x-start.x)*n/6),y:Math.round(start.y+(end.y-start.y)*n/6),button:'left'});
        win.webContents.sendInputEvent({type:'mouseUp',...end,button:'left'});
        await sleep(18);
        const result=await execute(`(()=>{const s=window.getSelection();const rects=s.rangeCount?[...s.getRangeAt(0).getClientRects()].filter(r=>r.width>1):[];return {text:s.toString(),tops:rects.map(r=>r.top),top:rects[0]?.top}})()`);
        const wrongRow=result.tops.some(top=>Math.abs(top-candidate.top)>Math.max(8,candidate.bottom-candidate.top));
        const wrongText=result.text.length>candidate.text.length+10||!result.text.trim();
        results.push({index:i,margin,dy,endDy,wrongRow,wrongText,length:result.text.length,rows:new Set(result.tops.map(top=>Math.round(top/3))).size,targetTop:candidate.top,selectedTop:result.top});
      }
      report.selectionSweep={attempts:results.length,failures:results.filter(result=>result.wrongRow||result.wrongText)};
      const multiLine=[];
      for(let i=0;i<candidates.length-1;i++){
        const first=candidates[i],last=candidates[i+1];
        if(last.top-first.top<8||last.top-first.top>25||Math.abs(last.left-first.left)>35)continue;
        const start={x:Math.round(first.left+(first.right-first.left)*.25),y:Math.round((first.top+first.bottom)/2)};
        const end={x:Math.round(last.left+(last.right-last.left)*.65),y:Math.round((last.top+last.bottom)/2)};
        await execute('window.getSelection()?.removeAllRanges()');
        win.webContents.sendInputEvent({type:'mouseMove',...start});
        win.webContents.sendInputEvent({type:'mouseDown',...start,button:'left',clickCount:1});
        for(let n=1;n<=8;n++)win.webContents.sendInputEvent({type:'mouseMove',x:Math.round(start.x+(end.x-start.x)*n/8),y:Math.round(start.y+(end.y-start.y)*n/8),button:'left'});
        win.webContents.sendInputEvent({type:'mouseUp',...end,button:'left'});
        await sleep(18);
        const result=await execute(`(()=>{const s=window.getSelection(),r=s.rangeCount?[...s.getRangeAt(0).getClientRects()].filter(rect=>rect.width>1):[];return {text:s.toString(),tops:r.map(rect=>rect.top)}})()`);
        multiLine.push({index:i,start:start,end:end,first:first.text,last:last.text,text:result.text,tops:result.tops,length:result.text.length,rows:new Set(result.tops.map(top=>Math.round(top/3))).size,wrong:result.tops.some(top=>top<first.top-5||top>last.bottom+5)||result.tops.length<2});
      }
      report.multiLineSelection={attempts:multiLine.length,failures:multiLine.filter(value=>value.wrong)};
      report.passed=report.selectionSweep.failures.length===0&&report.multiLineSelection.failures.length===0;
      return;
    }
    if (process.env.RESEARCH_QA_SELECTION_DEBUG) {
      const target = await execute(`(()=>{const spans=[...document.querySelectorAll('.bilingual-page-row')[1].querySelectorAll('.continuous-page .textLayer span')];const candidate=spans.map(span=>({span,rect:span.getBoundingClientRect()})).find(({span,rect})=>span.textContent.trim().length>25&&rect.width>150&&rect.top>190&&rect.bottom<900);if(!candidate)throw Error('找不到测试行');const {span,rect}=candidate;return {text:span.textContent,start:{x:Math.round(rect.left+Math.min(12,rect.width*.08)),y:Math.round(rect.top+rect.height/2)},end:{x:Math.round(rect.left+rect.width*.7),y:Math.round(rect.top+rect.height/2)}};})()`);
      const results=[];
      for(const [startOffset,endOffset,yOffset,endYOffset] of [[0,0,0,0],[-10,0,0,0],[-25,0,0,0],[0,10,0,0],[0,0,-6,-6],[0,0,6,6],[0,0,0,30]]) {
        await execute('window.getSelection()?.removeAllRanges()');
        const start={x:target.start.x+startOffset,y:target.start.y+yOffset},end={x:target.end.x+endOffset,y:target.end.y+endYOffset};
        win.webContents.sendInputEvent({type:'mouseMove',...start});
        win.webContents.sendInputEvent({type:'mouseDown',...start,button:'left',clickCount:1});
        for(let n=1;n<=8;n++) win.webContents.sendInputEvent({type:'mouseMove',x:Math.round(start.x+(end.x-start.x)*n/8),y:Math.round(start.y+(end.y-start.y)*n/8),button:'left'});
        win.webContents.sendInputEvent({type:'mouseUp',...end,button:'left'});
        await sleep(50);
        results.push({startOffset,endOffset,yOffset,endYOffset,selected:await execute(`(()=>{const selection=window.getSelection(),range=selection?.rangeCount?selection.getRangeAt(0):null;return {text:selection?.toString(),rects:range?[...range.getClientRects()].map(r=>[r.left,r.top,r.right,r.bottom]):[],anchor:selection?.anchorNode?.parentElement?.textContent?.slice(0,120),focus:selection?.focusNode?.parentElement?.textContent?.slice(0,120)}})()`)});
      }
      report.selectionDebug={target,results};
      const rows = result => new Set(result.selected.rects.map(rect => Math.round(rect[1] / 3))).size;
      check('横向拖选从行首空白开始仍只选择当前行', rows(results[2]) === 1 && results[2].selected.text.length < target.text.length);
      check('纵向拖选仍可跨行', rows(results.at(-1)) > 1);
    }
    check('翻页保留连续滚动容器', await execute('!!document.querySelector(".pdf-scroll.bilingual-flow") && document.querySelectorAll(".bilingual-page-row").length>2'));
    await capture('continuous-second-page');
    await execute("document.querySelector('button[aria-label=\"切换原文译文对照\"]').click()");
    await until('!document.querySelector(".pdf-scroll.bilingual-flow") && !!document.querySelector(".pdf-scroll .continuous-page")', '关闭译文仍连续阅读');
    await execute("document.querySelector('button[aria-label=\"切换原文译文对照\"]').click()");
    await until('!!document.querySelector(".pdf-scroll.bilingual-flow")', '恢复连续对照');
    check('切换译文不改变连续模式', true);
    await execute("document.querySelectorAll('.bilingual-page-row')[1].scrollIntoView({block:'start'})");
    await until("document.querySelectorAll('.bilingual-page-row')[1]?.querySelectorAll('.continuous-page .textLayer span').length>0", '第二页文字层');
    await execute(`(()=>{const page=document.querySelectorAll('.bilingual-page-row')[1];const span=[...page.querySelectorAll('.continuous-page .textLayer span')].find(value=>value.firstChild?.nodeType===Node.TEXT_NODE&&value.textContent.trim().length>8);if(!span)throw Error('第二页没有可选文字');const range=document.createRange();range.setStart(span.firstChild,0);range.setEnd(span.firstChild,Math.min(6,span.firstChild.length));const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);page.querySelector('.continuous-page').dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));})()`);
    await execute("document.querySelector('button[aria-label=\"高亮\"]').click()");
    for (let n=0;n<40;n++) {
      if ((await rpc('annotations.list',{attachmentId:attachment.id})).some(value=>value.pageIndex===1)) break;
      await sleep(150);
    }
    check('连续阅读第二页可选择并批注', (await rpc('annotations.list',{attachmentId:attachment.id})).some(value=>value.pageIndex===1));
    if (process.env.RESEARCH_QA_SELECTION_DEBUG) {
      await execute("document.querySelector('button[aria-label=\"切换连续阅读\"]').click()");
      await until("document.querySelector('.pdf-paper .textLayer span')?.textContent?.length>0", '单页模式文字层');
      const points=await execute(`(()=>{const spans=[...document.querySelectorAll('.pdf-paper .textLayer span')];const item=spans.map(span=>({span,rect:span.getBoundingClientRect()})).find(({span,rect})=>span.textContent.trim().length>25&&rect.width>150&&rect.top>190&&rect.bottom<900);if(!item)throw Error('单页模式找不到测试行');return {start:{x:Math.round(item.rect.left-20),y:Math.round(item.rect.top+item.rect.height/2)},end:{x:Math.round(item.rect.left+item.rect.width*.7),y:Math.round(item.rect.top+item.rect.height/2)}}})()`);
      await execute('window.getSelection()?.removeAllRanges()');
      win.webContents.sendInputEvent({type:'mouseMove',...points.start});
      win.webContents.sendInputEvent({type:'mouseDown',...points.start,button:'left',clickCount:1});
      for(let n=1;n<=8;n++) win.webContents.sendInputEvent({type:'mouseMove',x:Math.round(points.start.x+(points.end.x-points.start.x)*n/8),y:points.start.y,button:'left'});
      win.webContents.sendInputEvent({type:'mouseUp',...points.end,button:'left'});
      const single=await execute(`(()=>{const selection=window.getSelection();return {text:selection.toString(),rows:[...selection.getRangeAt(0).getClientRects()].filter(rect=>rect.width>1).map(rect=>Math.round(rect.top/3))}})()`);
      check('单页模式横向拖选不串行', single.text.length>0 && new Set(single.rows).size===1);
    }
    report.passed = true;
  } catch (error) {
    report.error = String(error.stack || error).slice(0, 3000);
    try {await capture('continuous-failure');} catch {}
  } finally {
    fs.writeFileSync(path.join(out, 'report.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({passed: report.passed, checks: report.checks, error: report.error}));
    process.exitCode = report.passed ? 0 : 1;
    app.quit();
  }
};
