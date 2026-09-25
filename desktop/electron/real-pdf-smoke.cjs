const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

exports.run = async ({win, rpc, project, app}) => {
  const out = process.env.RESEARCH_QA_OUT || path.join(project, 'docs/verification/deepseek-user-flow');
  fs.mkdirSync(out, {recursive: true});
  const report = {version: app.getVersion(), passed: false, screenshots: [], console: []};
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const execute = async code => {
    const result = await win.webContents.executeJavaScript(`(async()=>{try{return {ok:true,value:await (${code})}}catch(error){return {ok:false,error:String(error.stack||error)}}})()`, true);
    if (!result.ok) throw new Error(result.error);
    return result.value;
  };
  const until = async (check, label, timeout = 30000) => {
    const started = Date.now();
    while (Date.now() - started < timeout) {if (await check()) return; await sleep(250);}
    throw new Error('等待超时：' + label);
  };
  const capture = async name => {await sleep(250);fs.writeFileSync(path.join(out, name + '.png'), (await win.webContents.capturePage()).toPNG());report.screenshots.push(name + '.png');};
  win.webContents.setBackgroundThrottling(false);
  win.setContentSize(1586, 992);
  win.showInactive();
  win.webContents.on('console-message', event => {if (event.level === 'error') report.console.push(event.message);});
  try {
    const paper = (await rpc('items.list', {limit: 50})).items.find(value => value.title.startsWith('CEA-FJSP:'));
    assert.ok(paper, '真实开放论文未入库');
    const detail = await rpc('items.get', {id: paper.id});
    const attachment = detail.attachments.find(value => value.textStatus === 'indexed' && value.pageCount >= 10);
    assert.ok(attachment, '真实 PDF 未完成全文索引');
    await until(() => execute('!!document.querySelector(".library-header")'), '桌面客户端');
    await execute(`(()=>{const button=[...document.querySelectorAll('.nav-item')].find(value=>value.textContent.includes('我的文献'));button.click();})()`);
    await until(() => execute(`[...document.querySelectorAll('.document-row')].some(value=>value.textContent.includes(${JSON.stringify(paper.title)}))`), '真实论文在文献库显示');
    await execute(`(()=>{const row=[...document.querySelectorAll('.document-row')].find(value=>value.textContent.includes(${JSON.stringify(paper.title)}));row.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));})()`);
    await until(() => execute('!!document.querySelector(".pdf-scroll .continuous-page")'), '默认连续阅读');
    await execute(`document.querySelector('button[aria-label="切换连续阅读"]').click()`);
    await until(() => execute('document.querySelector(".pdf-paper canvas")?.width>100 && document.querySelectorAll(".textLayer span").length>20'), '真实 PDF 页面与文字层', 40000);
    await capture('real-oa-pdf-reader');
    await execute(`(()=>{const span=[...document.querySelectorAll('.textLayer span')].find(value=>value.textContent.includes('CEA-FJSP'))||[...document.querySelectorAll('.textLayer span')].find(value=>value.textContent.trim().length>20);if(!span)throw Error('真实 PDF 无可选择文字');const range=document.createRange();range.selectNodeContents(span);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);document.querySelector('.pdf-paper').dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));})()`);
    await execute(`document.querySelector('button[aria-label="高亮"]').click()`);
    await until(async () => (await rpc('annotations.list', {attachmentId: attachment.id})).length > 0, '真实 PDF 批注保存');
    await capture('real-oa-pdf-annotation');
    const notes = await rpc('notes.list', {itemId: paper.id});
    assert.ok(notes.some(value => value.content.includes('research://attachment/' + attachment.id + '?page=1')), '辅助阅读笔记未链接真实 PDF');
    assert.equal(report.console.length, 0, report.console.join('\n'));
    report.passed = true;
    report.title = paper.title;
    report.pages = attachment.pageCount;
    report.annotationCount = (await rpc('annotations.list', {attachmentId: attachment.id})).length;
    report.noteHasPageBacklink = true;
  } catch (error) {
    report.error = String(error.stack || error).slice(0, 3000);
    try {await capture('real-oa-failure');} catch {}
  } finally {
    fs.writeFileSync(path.join(out, 'real-pdf-ui-report.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify({passed: report.passed, pages: report.pages, annotationCount: report.annotationCount, error: report.error}));
    process.exitCode = report.passed ? 0 : 1;
    app.quit();
  }
};
