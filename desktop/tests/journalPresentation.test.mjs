import test from 'node:test';
import assert from 'node:assert/strict';
import {journalView, sourceFacts, main, availableSources, submissionView, feedbackView, relatedView, flag, numberText, countryName, safeUrl, quartile, missing} from '../src/journalPresentation.ts';
// Fictional records exercise the shape of imported journal data without
// redistributing a harvested third-party database or user feedback.
const example={
  id:1,name:'Example Clinical Methods',
  unified:{country:'US',wikidata_inception:'1950',print_issn:'0000-0000',electronic_issn:'0000-0001',
    type:'journal',has_doaj:true,doaj_is_in_doaj:false,
    submission:{has_guideline:false,has_latex:true,latex_class:'Example.cls'},
    comments:{count:1,rating_confidence:'low'}},
  sources:{
    jcr:{main:{year:2025,impact_factor:6.2,if_quartile:'Q1'},all_years:[
      {year:2025,impact_factor:6.2},{year:2024,impact_factor:5.8}]},
    fqb:{main:{year:2025,major_category:'医学',major_quartile:'1 [77/5603]'}},
    scimago:{main:{year:2025,h_index:42,open_access:false}},
    openalex:{main:{is_oa:true,summary_stats_h_index:88,works_count:120}},
    submission:{main:{has_guideline:false,has_latex:true},latex:{class:'Example.cls'},fields_json:{}},
    comments:{comment_count:1,rating_confidence:'low',recent:[{comment_text:'Synthetic feedback for UI testing.',paper_status:'unknown'}]},
  },
};
const exampleSubmission={id:2,name:'Example Interdisciplinary Review',sources:{submission:{fields_json:{submission:{file_formats:['latex','word','rtf','pdf']}}}}};
const fixtures=[example,exampleSubmission,{id:3,name:'Sparse Example',unified:{},sources:{}}];
const value=(facts,label)=>facts.find(f=>f.label===label)?.value;

test('synthetic journal: curated Chinese facts omit duplicate/internal fields',()=>{
  const view=journalView(example);
  assert.equal(value(view.basic,'国家 / 地区'),'美国');
  assert.equal(value(view.basic,'创刊年份'),'1950 年');
  assert.equal(value(view.basic,'纸本 ISSN'),'0000-0000');
  assert.equal(value(view.basic,'电子 ISSN'),'0000-0001');
  assert.equal(value(view.basic,'期刊类型'),'学术期刊');
  assert.equal(view.basic.filter(f=>f.label==='出版商').length,1);
  for(const key of ['built_at','all_issns','identifiers','has_doaj','seo_slug']) assert.ok(!JSON.stringify(view).includes(key));
});
test('record availability is not DOAJ inclusion; OA conflicts remain source-specific',()=>{
  const view=journalView(example);
  assert.equal(example.unified.has_doaj,true);
  assert.equal(value(view.access,'DOAJ 收录'),'未收录');
  assert.deepEqual(view.access.filter(f=>f.label==='开放获取').map(f=>f.value),['是','否']);
  assert.equal(view.oaConflict,true);
  assert.equal(value(journalView({unified:{has_doaj:true}}).access,'DOAJ 收录'),missing);
});
test('false, zero and absent values retain their meanings',()=>{
  assert.equal(flag(false),false);assert.equal(flag('No'),false);assert.equal(flag(null),null);
  assert.equal(numberText(0),'0');assert.equal(numberText(null),missing);assert.equal(numberText(false),missing);
  assert.equal(numberText(911006),'911,006');assert.equal(quartile('1 [77/5603]'),'1 区（77 / 5,603）');
});
test('metrics keep source year and do not merge H indices from different sources',()=>{
  const v=journalView(example);
  assert.equal(v.headlines[0].value,'6.2');assert.match(v.headlines[0].note,/2025/);
  assert.equal(value(sourceFacts('scimago',main(example,'scimago'),example),'H 指数'),'42');
  assert.equal(value(sourceFacts('openalex',main(example,'openalex'),example),'H 指数'),'88');
  const old=example.sources.jcr.all_years.find(r=>r.year!==2025);
  assert.ok(old);assert.equal(value(sourceFacts('jcr',old,example),'数据年份'),old.year+' 年');
  assert.equal(value(sourceFacts('jcr',old,example),'影响因子'),numberText(old.impact_factor));
  const partial={sources:{jcr:{main:{year:2024,impact_factor:null}}},unified:{jcr_impact_factor:7,jcr_year:2025}};
  assert.match(journalView(partial).headlines[0].note,/2025/);
});
test('all known source cards output scalar display values, including sparse journals',()=>{
  for(const item of [...fixtures,{}, {unified:{},sources:{jcr:{main:{}}}}]) {
    for(const source of availableSources(item)) {
      for(const f of sourceFacts(source,main(item,source),item)) {
        assert.equal(typeof f.value,'string');assert.ok(!f.label.includes('_'));assert.notEqual(f.value,'[object Object]');
      }
    }
    assert.ok(journalView(item).basic.every(f=>typeof f.value==='string'));
  }
});
test('template filename is not converted into a fabricated download URL',()=>{
  const view=submissionView(example);
  assert.equal(value(view.summary,'投稿要求资料'),'暂未保存');
  assert.equal(value(view.summary,'LaTeX 模板记录'),'有记录');
  assert.equal(value(view.requirements,'LaTeX 模板类'),'Example.cls');
  assert.ok(!view.links.some(f=>f.label==='LaTeX 模板'));
  const nature=submissionView(exampleSubmission);
  assert.match(value(nature.requirements,'可提交文件格式'),/LATEX.*WORD.*RTF.*PDF/);
});
test('feedback has readable summaries and original text without treating unknown as accepted',()=>{
  const view=feedbackView(example);
  assert.equal(value(view.summary,'反馈数量'),'1');
  assert.equal(value(view.summary,'评分可信度'),'较低');
  assert.equal(view.comments[0].content,example.sources.comments.recent[0].comment_text);
  assert.equal(value(view.comments[0].facts,'投稿结果'),'未明确');
  assert.equal(view.comments[0].date,missing);
});
test('unmapped objects and unknown fields never leak into normal details',()=>{
  const dirty=structuredClone(example);
  dirty.unified.new_internal_object={unsafe_key:'SHOULD_NOT_RENDER'};
  dirty.sources.submission.main.word_limit={unsafe_key:'SHOULD_NOT_RENDER'};
  dirty.sources.comments.recent[0].paper_status='backend_unknown_status';
  const output=JSON.stringify([journalView(dirty),submissionView(dirty),feedbackView(dirty)]);
  assert.ok(!output.includes('SHOULD_NOT_RENDER'));assert.ok(!output.includes('backend_unknown_status'));
});
test('related content uses named cards and only valid web links',()=>{
  const view=relatedView({same_category:[{id:31,name:'A journal',jcr_impact_factor:4}],recent_papers:[{title:'A paper',publication_year:2025,doi:'10.1234/example'}],internal_key:{foo:true}});
  assert.equal(view.journals[0].name,'A journal');assert.equal(view.papers[0].url,'https://doi.org/10.1234/example');
  assert.equal(safeUrl('javascript:alert(1)'),undefined);assert.equal(safeUrl('template.zip'),undefined);
  assert.equal(countryName('US'),'美国');assert.equal(countryName('United States'),'美国');
});
