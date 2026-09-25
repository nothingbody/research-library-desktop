const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

exports.run = async ({win, rpc, project, app}) => {
  const out = process.env.RESEARCH_QA_OUT || path.join(project, 'docs/verification/deepseek-user-flow');
  let key = process.env.RESEARCH_QA_API_KEY || '';
  delete process.env.RESEARCH_QA_API_KEY;
  fs.mkdirSync(out, {recursive: true});
  const report = {version: app.getVersion(), startedAt: new Date().toISOString(), passed: false, steps: [], observations: {}, screenshots: [], console: []};
  const execute = async code => {
    const result = await win.webContents.executeJavaScript(`(async()=>{try{return {ok:true,value:await (${code})}}catch(error){return {ok:false,error:String(error.stack||error)}}})()`, true);
    if (!result.ok) throw new Error(result.error);
    return result.value;
  };
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const save = () => fs.writeFileSync(path.join(out, 'user-flow-report.json'), JSON.stringify(report, null, 2));
  const until = async (check, label, timeout = 30000) => {
    const started = Date.now();
    while (Date.now() - started < timeout) {if (await check()) return; await sleep(250);}
    throw new Error('等待超时：' + label);
  };
  const ui = (expression, label, timeout) => until(() => execute(expression), label, timeout);
  const step = async (name, work) => {
    report.currentStep = name; save();
    const started = Date.now();
    await work();
    report.steps.push({name, passed: true, elapsedMs: Date.now() - started});
    delete report.currentStep; save();
  };
  const nav = label => execute(`(()=>{const button=[...document.querySelectorAll('.nav-item')].find(item=>item.textContent.includes(${JSON.stringify(label)}));if(!button)throw Error('找不到导航：'+${JSON.stringify(label)});button.click();})()`);
  const capture = async name => {await sleep(250);fs.writeFileSync(path.join(out, name + '.png'), (await win.webContents.capturePage()).toPNG());report.screenshots.push(name + '.png');};
  const waitJob = async (id, timeout = 180000) => {
    let result;
    await until(async () => {const jobs = await rpc('jobs.list');result = jobs.find(job => job.id === id);return result && ['completed', 'failed', 'cancelled'].includes(result.state);}, '后台任务 ' + id, timeout);
    if (result.state !== 'completed') throw new Error('后台任务失败：' + (result.error?.message || result.state));
    return result;
  };
  let sessionId, candidateIds, itemIds, attachmentId, collectionId, conversationId, relationId, confirmedRelationId;
  win.webContents.setBackgroundThrottling(false);
  win.setContentSize(1586, 992);
  win.showInactive();
  win.webContents.on('console-message', event => {if (event.level === 'error') report.console.push(event.message);});
  try {
    assert.ok(key, 'QA 凭据未传入');
    await step('启动并配置 DeepSeek', async () => {
      await ui('!!window.research && !!document.querySelector(".library-header")', '桌面界面');
      await rpc('assistant.settings', {assistantBaseUrl: 'https://api.deepseek.com', assistantModel: 'deepseek-flash', assistantTimeout: 120});
      await rpc('assistant.configure', {apiKey: key});
      key = '';
      await nav('设置');
      await ui('document.querySelector(".assistant-settings")?.textContent.includes("服务、模型与访问密钥均已就绪")', '助手配置状态');
      await execute(`(()=>{const button=[...document.querySelectorAll('.assistant-settings button')].find(value=>value.textContent.includes('测试连接'));button.click();})()`);
      await ui('document.querySelector(".toast")?.textContent.includes("阅读助手连接正常")', '真实服务连接', 90000);
      report.observations.provider = 'DeepSeek deepseek-flash';
    });
    if (process.env.RESEARCH_QA_RESUME === '1') {
      const items = (await rpc('items.list', {limit: 10})).items.slice(0, 2);
      assert.equal(items.length, 2, '续测资料库缺少两篇文献');
      itemIds = items.map(item => item.id);
      report.observations.importedTitles = items.map(item => item.title);
    } else {
    await step('AI 生成检索计划', async () => {
      await nav('AI 文献检索');
      await ui('!!document.querySelector(".ai-composer")', '检索任务输入');
      await execute(`(()=>{const field=document.querySelector('textarea[aria-label="研究任务"]');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(field,'检索多车间多目标柔性作业车间调度的研究，比较完工时间和能耗优化方法，并识别使用遗传算法或强化学习的论文。');field.dispatchEvent(new Event('input',{bubbles:true}));const model=document.querySelector('.model-plan input');if(!model.checked)model.click();})()`);
      await execute(`(()=>{const button=[...document.querySelectorAll('button')].find(value=>value.textContent.includes('生成检索计划'));button.click();})()`);
      await ui('!!document.querySelector(".ai-plan-panel")', '可编辑的 AI 检索计划', 130000);
      const session = (await rpc('aiSearch.list'))[0];
      assert.ok(session, '检索任务未保存');
      sessionId = session.id;
      const detail = await rpc('aiSearch.get', {sessionId});
      report.observations.planGenerator = detail.plan.generator;
      report.observations.planQueries = detail.plan.queries?.length || 0;
      if (detail.plan.generator !== 'configured-model') throw new Error('模型规划回退：' + (detail.planningWarning || '未说明原因'));
      assert.ok(detail.plan.queries?.length > 0);
      await capture('01-model-plan');
    });
    await step('检索公开来源并检查证据', async () => {
      await execute(`(()=>{const button=[...document.querySelectorAll('button')].find(value=>value.textContent.trim().startsWith('运行检索'));button.click();})()`);
      let session;
      await until(async () => {session = await rpc('aiSearch.get', {sessionId});return ['completed', 'partial', 'failed', 'cancelled'].includes(session.state);}, '公开来源检索完成', 180000);
      assert.ok(['completed', 'partial'].includes(session.state), `检索状态：${session.state}`);
      const result = await rpc('aiSearch.results', {sessionId, limit: 30});
      assert.ok(result.total >= 2, '未取得至少两篇候选文献');
      report.observations.searchResults = result.total;
      report.observations.searchState = session.state;
      await ui('document.querySelectorAll(".ai-result-card").length>=2', '候选文献卡片');
      await execute('document.querySelector(".detail-action").click()');
      await ui('document.querySelectorAll(".evidence-list article").length>0', '证据侧栏');
      await capture('02-search-evidence');
    });
    await step('保存两篇文献并用 DeepSeek 核验摘要', async () => {
      const result = await rpc('aiSearch.results', {sessionId, limit: 30});
      const papers = result.items.filter(item => (item.abstract || '').length >= 80).slice(0, 2);
      assert.equal(papers.length, 2, '当前结果缺少两篇有摘要的文献');
      candidateIds = papers.map(item => item.id);
      for (const paper of papers) await execute(`(()=>{const button=[...document.querySelectorAll('.ai-result-card .result-select')].find(value=>value.getAttribute('aria-label')===${JSON.stringify('选择 ' + paper.title)});if(!button)throw Error('找不到结果选择框');button.click();})()`);
      await execute('document.querySelector(".result-footer .primary").click()');
      await until(async () => (await rpc('library.stats')).items >= 2, '保存题录');
      const evidence = await Promise.all(candidateIds.map(candidateId => rpc('aiSearch.evidence', {candidateId})));
      itemIds = evidence.map(value => value.importedItemId);
      assert.ok(itemIds.every(Boolean));
      report.observations.importedTitles = papers.map(item => item.title);
      const job = await rpc('aiSearch.verify', {candidateIds: [candidateIds[0]], allowRemote: true, includeFulltext: false});
      const finished = await waitJob(job.jobId);
      assert.ok(!(finished.result?.failed || []).length, JSON.stringify(finished.result?.failed));
      const verified = await rpc('aiSearch.evidence', {candidateId: candidateIds[0]});
      report.observations.verification = verified.verification;
      await capture('03-imported-and-verified');
    });
    await step('建立专题文献库并整理题录', async () => {
      const collection = await rpc('collections.edit', {action: 'create', name: '多车间调度 · 全流程验收'});
      collectionId = collection.id;
      await rpc('items.bulk', {ids: itemIds, action: 'collection', value: collectionId});
      await rpc('items.bulk', {ids: itemIds, action: 'addTags', value: ['多车间调度', '全流程验收']});
      const grouped = await rpc('items.list', {collectionId, limit: 10});
      assert.equal(grouped.total, 2, '专题集合未包含两篇文献');
      assert.equal((await rpc('items.list', {tag: '多车间调度', limit: 10})).total, 2, '文献标签检索失败');
      report.observations.collectionItems = grouped.total;
      await nav('我的文献');
      await ui('document.querySelectorAll(".document-row").length>=2', '已导入的文献库');
      await capture('03b-library-organization');
    });
    await step('检查开放全文并阅读 PDF', async () => {
      const sources = await rpc('fulltext.sources', {itemId: itemIds[0]});
      report.observations.fulltextCandidates = sources.length;
      const oa = sources.find(value => value.origin === 'oa');
      if (oa) {
        try {
          const job = await rpc('fulltext.obtain', {itemId: itemIds[0], sourceId: oa.id});
          const finished = await waitJob(job.jobId, 100000);
          report.observations.openPdf = finished.result?.attachmentId ? 'attached' : 'no attachment';
        } catch (error) {report.observations.openPdf = 'source unavailable: ' + String(error.message).slice(0, 180);}
      } else report.observations.openPdf = 'no open PDF candidate';
      const fixture = path.join(project, 'docs/verification/runtime/verification.pdf');
      const preview = await rpc('_imports.preview', {paths: [fixture], itemId: itemIds[0]});
      await waitJob((await rpc('imports.commit', {batchId: preview.batchId})).jobId);
      const item = await rpc('items.get', {id: itemIds[0]});
      attachmentId = item.attachments.find(value => value.name === 'verification.pdf')?.id;
      assert.ok(attachmentId, '测试 PDF 未挂接');
      await nav('我的文献');
      await ui('document.querySelectorAll(".document-row").length>=2', '文献库列表');
      const title = item.title;
      await execute(`(()=>{const row=[...document.querySelectorAll('.document-row')].find(value=>value.textContent.includes(${JSON.stringify(title)}));if(!row)throw Error('找不到导入的文献');row.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));})()`);
      await ui('document.querySelector(".pdf-paper canvas")?.width>100', 'PDF 阅读器', 30000);
      await capture('04-pdf-reader');
    });
    await step('DeepSeek 辅助阅读并保存笔记', async () => {
      await execute(`(()=>{const span=[...document.querySelectorAll('.textLayer span')].find(value=>value.textContent.includes('Attention connects'));if(!span)throw Error('PDF 文字层不可用');const range=document.createRange();range.selectNodeContents(span);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);document.querySelector('.pdf-paper').dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));})()`);
      await execute(`document.querySelector('button[aria-label="打开辅助阅读"]').click()`);
      await ui('!!document.querySelector(".reader-assistant .selected-source p")', '所选原文');
      await execute(`(()=>{const button=[...document.querySelectorAll('.assistant-actions button')].find(value=>value.textContent.includes('解释'));button.click();})()`);
      await ui('!!document.querySelector(".assistant-result p") || !!document.querySelector(".assistant-error")', '模型阅读结果', 160000);
      const error = await execute('document.querySelector(".assistant-error")?.textContent || ""');
      assert.equal(error, '', error);
      const content = await execute('document.querySelector(".assistant-result p")?.textContent || ""');
      assert.ok(content.length >= 8, '解释结果为空');
      report.observations.readingAnswerLength = content.length;
      await execute(`(()=>{const button=[...document.querySelectorAll('.assistant-result button')].find(value=>value.textContent.includes('保存为笔记'));button.click();})()`);
      await until(async () => (await rpc('notes.list', {itemId: itemIds[0]})).some(note => note.content.includes('Attention connects')), '带原文链接的笔记');
      await capture('05-reading-assistant');
    });
    await step('阅读进度、阅读卡与术语', async () => {
      const before = await rpc('reading.get', {itemId: itemIds[0]});
      const saved = await rpc('reading.save', {itemId: itemIds[0],
        session: {status: 'reading', goal: '核对多目标调度研究的问题定义', revision: before.session.revision},
        card: {summary: '验收说明：测试 PDF 仅用于验证阅读流程，不代表原论文全文。', revision: before.card.revision}});
      assert.equal(saved.session.status, 'reading');
      assert.ok(saved.card.summary.includes('测试 PDF'));
      await rpc('terms.save', {itemId: itemIds[0], attachmentId, term: 'Attention', translation: '注意力',
        explanation: '测试 PDF 中的术语标记。', source: {page: 1, quote: 'Attention connects queries, keys, and values.'}});
      assert.ok((await rpc('terms.list', {itemId: itemIds[0]})).some(value => value.term === 'Attention'));
      await execute('document.querySelectorAll(".reader-side-tabs button")[1].click()');
      await ui('!!document.querySelector(".reader-assistant textarea")', '阅读卡界面');
      await capture('05c-reading-card');
      await execute('document.querySelectorAll(".reader-side-tabs button")[0].click()');
    });
    await step('原文高亮批注与本地持久化', async () => {
      await execute(`document.querySelector('button[aria-label="选择文字"]').click()`);
      await execute(`(()=>{const span=[...document.querySelectorAll('.textLayer span')].find(value=>value.textContent.includes('Attention connects'));const range=document.createRange();range.selectNodeContents(span);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);document.querySelector('.pdf-paper').dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));})()`);
      await execute(`document.querySelector('button[aria-label="高亮"]').click()`);
      await until(async () => (await rpc('annotations.list', {attachmentId})).length > 0, '高亮批注保存');
      await capture('05b-annotation');
    });
    }
    await step('跨两篇文献向 DeepSeek 提问', async () => {
      await execute('document.querySelector(".workspace-tabs>button").click()');
      await nav('多文献问答');
      await ui('!!document.querySelector(".research-ask-rail>button")', '研究问题侧栏');
      await execute('document.querySelector(".research-ask-rail>button").click()');
      await ui('document.querySelectorAll(".research-ask-picker label").length>=2', '问答材料选择器');
      for (const title of report.observations.importedTitles) await execute(`(()=>{const label=[...document.querySelectorAll('.research-ask-picker label')].find(value=>value.textContent.includes(${JSON.stringify(title)}));if(!label)throw Error('找不到问答文献');label.querySelector('input').click();})()`);
      await execute(`(()=>{const button=[...document.querySelectorAll('.research-ask-create button')].find(value=>value.textContent.includes('建立问答'));button.click();})()`);
      await ui('!!document.querySelector(".research-ask-composer textarea")', '多文献问答输入');
      await execute(`(()=>{const field=document.querySelector('.research-ask-composer textarea');Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(field,'这两篇文献分别研究了什么调度问题？请只依据各自摘要给出有原文引用的比较。');field.dispatchEvent(new Event('input',{bubbles:true}));const button=[...document.querySelectorAll('.research-ask-composer button')].find(value=>value.textContent.includes('提问'));button.click();})()`);
      const conversation = (await rpc('researchAsk.list'))[0];
      conversationId = conversation.id;
      let answer;
      await until(async () => {const value = await rpc('researchAsk.get', {conversationId: conversation.id});answer = value.messages.at(-1);return answer && ['completed', 'failed'].includes(answer.status);}, '跨文献回答', 180000);
      assert.equal(answer.status, 'completed', answer.result?.error || '问答失败');
      const claims = answer.result?.claims || [];
      report.observations.answerClaims = claims.length;
      report.observations.answerCitations = claims.flatMap(value => value.citations || []).length;
      assert.ok(claims.length > 0 && report.observations.answerCitations > 0, '没有生成可核对的引用结论');
      await ui('document.querySelectorAll(".research-claim").length>0', '有引用的回答');
      await capture('06-multi-paper-answer');
      await execute('document.querySelector(".research-claim button").click()');
      await until(async () => (await rpc('notes.list')).some(note => note.title.includes('待核对')), '待核对结论笔记');
    });
    await step('跨文献关联与引用输出', async () => {
      const relation = await rpc('relations.create', {itemIds, mode: 'compare', remoteAI: true, purpose: '比较两篇论文的调度问题、方法和证据局限'});
      relationId = relation.id;
      const job = await rpc('relations.run', {sessionId: relation.id});
      await waitJob(job.jobId, 180000);
      const rows = await rpc('relations.results', {sessionId: relation.id});
      assert.ok(rows.length > 0, '没有产生文献关联');
      report.observations.relationEngines = [...new Set(rows.map(row => row.engine))];
      assert.ok(report.observations.relationEngines.includes('remote-evidence'), '联网 AI 关联未产生有双侧证据的结果');
      await nav('研究关联');
      await ui('!!document.querySelector(".relation-workspace-header")', '关联工作台');
      await ui('document.querySelectorAll(".relation-arc").length>0', '关联候选');
      await execute('document.querySelector(".relation-arc").click()');
      await ui('document.querySelectorAll(".relation-evidence-list article").length>=2', '双侧原文证据');
      await capture('07-relation-review');
      const confirmed = await rpc('relations.confirm', {relationId: rows[0].id, status: 'confirmed', userNote: '验收时核对了双侧原文；此判断仅留在测试库。'});
      confirmedRelationId = rows[0].id;
      assert.equal(confirmed.status, 'confirmed');
      const synthesis = await rpc('relations.export', {sessionId: relation.id});
      assert.ok(synthesis.title && synthesis.content.includes('已确认关联'), '关联综述笔记未生成');
      report.observations.confirmedRelations = 1;
      const citation = await execute(`window.research.call('citation.format',{ids:${JSON.stringify(itemIds)},style:'gb-t-7714-2015',lang:'zh-CN'})`);
      assert.ok(citation.text && citation.text.length > 30, '引文输出为空');
      report.observations.citationLength = citation.text.length;
      const journals = await rpc('journals.link', {itemId: itemIds[0]});
      report.observations.journalMatches = journals.items?.length || 0;
      const knownJournal = await rpc('journals.list', {q: '0007-9235', pageSize: 3});
      assert.ok(knownJournal.total > 0, '本地期刊索引查询失败');
      report.observations.journalIndexMatches = knownJournal.total;
    });
    if (process.env.RESEARCH_QA_RESUME !== '1') await step('重载后核对文献库与研究成果', async () => {
      win.webContents.reload();
      await sleep(1000);
      await ui('!!document.querySelector(".library-header")', '重载后的主界面');
      assert.equal((await rpc('items.list', {collectionId, limit: 10})).total, 2, '专题文献库丢失');
      assert.ok((await rpc('annotations.list', {attachmentId})).length > 0, 'PDF 批注丢失');
      assert.ok((await rpc('notes.list', {itemId: itemIds[0]})).length > 0, '辅助阅读笔记丢失');
      assert.ok((await rpc('notes.list')).some(value => value.title.includes('待核对')), '跨文献结论笔记丢失');
      assert.ok((await rpc('terms.list', {itemId: itemIds[0]})).some(value => value.term === 'Attention'), '术语丢失');
      assert.equal((await rpc('reading.get', {itemId: itemIds[0]})).session.status, 'reading', '阅读进度丢失');
      const reopened = await rpc('researchAsk.get', {conversationId});
      assert.ok(reopened.messages.at(-1)?.result?.claims?.length > 0, '跨文献回答丢失');
      assert.equal((await rpc('relations.evidence', {relationId: confirmedRelationId})).status, 'confirmed', '确认的关联丢失');
      report.observations.persistedAfterReload = true;
      await capture('08-reopened-library');
    });
    assert.equal(report.console.length, 0, report.console.join('\n'));
    report.passed = true;
  } catch (error) {
    report.error = String(error.stack || error).slice(0, 4000);
    try {await capture('failure');} catch {}
  } finally {
    key = '';
    try {await rpc('assistant.clear', {}); report.credentialCleared = true;} catch {report.credentialCleared = false;}
    report.finishedAt = new Date().toISOString();
    save();
    console.log(JSON.stringify({passed: report.passed, steps: report.steps.length, currentStep: report.currentStep, error: report.error, observations: report.observations, credentialCleared: report.credentialCleared, out}));
    process.exitCode = report.passed ? 0 : 1;
    app.quit();
  }
};
