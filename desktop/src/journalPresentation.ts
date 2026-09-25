import type {Data} from './api';

export type Fact = {label: string; value: string; note?: string; href?: string};
export const missing = '暂无数据';
export const sourceNames: Record<string, string> = {
  jcr: 'JCR', fqb: '中科院分区', xr: '新锐分区', scimago: 'SCImago', cwts: 'CWTS',
  openalex: 'OpenAlex', ccf: 'CCF', gjqk: '预警记录', doaj: 'DOAJ', crossref: 'Crossref',
  wikidata: 'Wikidata', jufo: 'JUFO', submission: '投稿资料', comments: '作者反馈',
};
export const sourceOrder = ['jcr', 'fqb', 'xr', 'scimago', 'cwts', 'ccf', 'gjqk', 'openalex', 'doaj', 'crossref', 'jufo', 'wikidata'];
export function present(value: unknown) {return value !== null && value !== undefined && value !== '' && !(typeof value === 'string' && !value.trim());}
export function first(...values: any[]) {return values.find(present);}
export function record(value: any): Data {return value && typeof value === 'object' && !Array.isArray(value) ? value : {};}
export function list(value: any): any[] {return Array.isArray(value) ? value : [];}
export function text(value: any): string {return typeof value === 'string' && value.trim() ? value.trim() : typeof value === 'number' && Number.isFinite(value) ? String(value) : missing;}
export function numberText(value: any, digits = 3): string {
  if (!present(value) || typeof value === 'boolean' || !['string', 'number'].includes(typeof value)) return missing;
  const number = Number(String(value).replace(/,/g, ''));
  return Number.isFinite(number) ? number.toLocaleString('zh-CN', {maximumFractionDigits: digits}) : missing;
}
export function flag(value: any): boolean | null {
  if (value === true || value === 1 || /^(true|yes|y|1|是|top)$/i.test(String(value))) return true;
  if (value === false || value === 0 || /^(false|no|n|0|否)$/i.test(String(value))) return false;
  return null;
}
export function yesNo(value: any, yes = '是', no = '否') {const state = flag(value); return state === null ? missing : state ? yes : no;}
export function joinText(value: any): string {
  return Array.isArray(value) ? [...new Set(value.map(text).filter(v => v !== missing))].join('、') || missing : text(value);
}
const countryAliases: Record<string, string> = {'UNITED STATES':'US','UNITED STATES OF AMERICA':'US','USA':'US','UNITED KINGDOM':'GB','UK':'GB','CHINA':'CN','GERMANY':'DE','NETHERLANDS':'NL','FRANCE':'FR','SWITZERLAND':'CH','JAPAN':'JP','CANADA':'CA','AUSTRALIA':'AU','SOUTH KOREA':'KR','INDIA':'IN','FINLAND':'FI'};
const countries = new Intl.DisplayNames(['zh-CN'], {type:'region', fallback:'none'});
export function countryName(value: any) {
  const raw = text(value); if (raw === missing) return raw;
  const code = countryAliases[raw.toUpperCase()] || raw.toUpperCase();
  try {return /^[A-Z]{2}$/.test(code) ? countries.of(code) || '地区待补充' : raw;} catch {return raw;}
}
export function languageName(value: any): string {
  const names: Data = {english:'英语',en:'英语',chinese:'中文',zh:'中文',french:'法语',fr:'法语',german:'德语',de:'德语',spanish:'西班牙语',es:'西班牙语',japanese:'日语',ja:'日语',russian:'俄语',ru:'俄语',portuguese:'葡萄牙语',pt:'葡萄牙语'};
  if (typeof value==='string' && /[、;,]/.test(value)) return joinText(value.split(/[、;,]/).map(v=>languageName(v.trim())));
  return Array.isArray(value) ? joinText(value.map(languageName)) : names[String(value).toLowerCase()] || text(value);
}
export function dateLabel(value: any) {
  if (!present(value)) return missing;
  const raw = String(value); if (/^\d{4}$/.test(raw)) return raw + ' 年';
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(raw);
  if (match && Number(match[2]) >= 1 && Number(match[2]) <= 12 && Number(match[3]) >= 1 && Number(match[3]) <= 31) return `${match[1]}年${Number(match[2])}月${Number(match[3])}日`;
  return missing;
}
export function yearLabel(value: any) {return /^\d{4}$/.test(String(value)) ? value + ' 年' : '年份未提供';}
export function safeUrl(value: any) {if (typeof value !== 'string') return undefined; try {const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined;} catch {return undefined;}}
export function fact(label: string, value: any, note?: string, format: (v: any) => string = text): Fact {return {label, value: format(value), note};}
export function linkFact(label: string, value: any): Fact {const href = safeUrl(value); return {label, value: href ? '打开' + label : missing, href};}
export function main(data: Data, source: string): Data {const s = record(data.sources?.[source]); return record(s.main || s);}
export function categoryName(value: any) {
  const names: Data = {ONCOLOGY:'肿瘤学（Oncology）',MEDICINE:'医学（Medicine）','MULTIDISCIPLINARY SCIENCES':'综合性科学（Multidisciplinary Sciences）'};
  return names[String(value).toUpperCase()] || text(value);
}
export function quartile(value: any) {
  if (!present(value)) return missing;
  const match = /^([1-4])(?:\s*区)?(?:\s*\[(\d+)\/(\d+)\])?$/.exec(String(value).trim());
  return match ? `${match[1]} 区${match[2] ? `（${numberText(match[2])} / ${numberText(match[3])}）` : ''}` : text(value);
}
const journalTypes: Data = {journal:'学术期刊', 'book series':'丛书', 'book-series':'丛书', conference:'会议论文集', repository:'知识库', 'ebook platform':'电子书平台', other:'其他'};

// Each display section opts into meaningful fields. Unknown source keys remain in raw data.
export function journalView(data: Data) {
  const u = record(data.unified), oa = main(data, 'openalex'), wd = main(data, 'wikidata'), jufo = main(data, 'jufo');
  const publisher = first(u.crossref_publisher, u.publisher, main(data, 'crossref').publisher, oa.host_organization_name);
  const country = countryName(first(u.country, oa.country_code, wd.country_label));
  const inception = first(u.wikidata_inception, wd.inception, jufo.year_start);
  const language = first(list(data.sources?.wikidata?.languages).map(v => v.language_label).filter(present).join('、'), jufo.language, main(data, 'xr').language);
  const basic: Fact[] = [
    fact('期刊名称', first(data.name, u.canonical_name, u.name)), fact('中文名称', wd.label_zh),
    fact('出版商', publisher), fact('国家 / 地区', country), fact('创刊年份', inception, undefined, v => /^\d{4}/.test(String(v)) ? String(v).slice(0,4) + ' 年' : missing),
    fact('期刊类型', journalTypes[String(first(u.type, oa.type))] || (present(first(u.type, oa.type)) ? '其他' : null)),
    fact('出版语言', language, undefined, languageName), fact('期刊缩写', first(wd.iso4_abbreviation, oa.abbreviated_title)),
    fact('纸本 ISSN', first(u.print_issn, u.issns?.print), undefined, joinText), fact('电子 ISSN', first(u.electronic_issn, u.issns?.electronic), undefined, joinText),
    fact('关联 ISSN（ISSN-L）', first(u.issn_l, oa.issn_l, wd.issn_l)), linkFact('期刊官网', first(data.homepage, oa.homepage_url)),
  ];
  const openalexOa = first(oa.is_oa, u.openalex_is_oa);
  const doajFlag = first(u.doaj_is_in_doaj, u.oa?.doaj?.in_doaj, oa.is_in_doaj);
  const scimagoOa = main(data, 'scimago').open_access;
  const access = [fact('开放获取', openalexOa, '来源：OpenAlex', yesNo), fact('DOAJ 收录', doajFlag, '按已保存的收录状态', v => yesNo(v, '已收录', '未收录'))];
  if (present(scimagoOa)) access.push(fact('开放获取', scimagoOa, `来源：SCImago · ${yearLabel(main(data,'scimago').year)}`, yesNo));
  const oaConflict = flag(openalexOa) !== null && flag(scimagoOa) !== null && flag(openalexOa) !== flag(scimagoOa);
  const jcr=main(data,'jcr'), fqb=main(data,'fqb');
  const impact=present(jcr.impact_factor)?{value:jcr.impact_factor,year:jcr.year}:{value:u.jcr_impact_factor,year:u.jcr_year};
  const division=present(fqb.major_quartile)?{value:fqb.major_quartile,year:fqb.year,category:fqb.major_category}:{value:u.fqb_major_quartile,year:u.fqb_year,category:u.fqb_major_category};
  const jcrDivision=present(jcr.if_quartile)?{value:jcr.if_quartile,year:jcr.year}:{value:u.jcr_quartile,year:u.jcr_year};
  const headlines = [
    fact('影响因子', impact.value, `JCR · ${yearLabel(impact.year)}`, numberText),
    fact('中科院分区', division.value, `${text(division.category)} · ${yearLabel(division.year)}`, v => {const q=quartile(v);return q.split('（')[0];}),
    fact('JCR 分区', jcrDivision.value, `JCR · ${yearLabel(jcrDivision.year)}`),
    fact('H 指数', first(oa.summary_stats_h_index, u.openalex_h_index), 'OpenAlex · 累计统计', numberText),
    fact('累计发文量', first(oa.works_count, u.openalex_works_count), 'OpenAlex · 累计统计', numberText),
  ];
  return {basic, access, oaConflict, headlines, publisher:text(publisher), country};
}

export function availableSources(data: Data) {return sourceOrder.filter(k => present(data.sources?.[k]));}
export function sourceFacts(source: string, r: Data, data: Data): Fact[] {
  const f = (label: string, key: string, format = text) => fact(label, r[key], undefined, format);
  const base = [f('数据年份','year',yearLabel)];
  if (source === 'jcr') return [...base, f('影响因子','impact_factor',numberText), f('JCR 分区','if_quartile'), f('学科','category',categoryName), f('学科排名','if_rank'), f('收录索引','web_of_science')];
  if (source === 'fqb' || source === 'xr') {
    const result = [...base, fact('大类学科',first(r.major_category_cn,r.major_category),undefined,categoryName), f('大类分区','major_quartile',quartile), f('TOP 期刊','top',yesNo)];
    for(let n=1;n<=6;n++) if(present(r['subcategory_'+n])) result.push(fact('小类学科 '+n,first(r['subcategory_'+n+'_cn'],r['subcategory_'+n]),undefined,categoryName),f('小类分区 '+n,'subcategory_'+n+'_quartile',quartile));
    result.push(f('收录索引',source==='fqb'?'web_of_science':'database_src'), f('备注','annotation'));
    return result;
  }
  if (source === 'scimago') return [...base,f('SJR','sjr',numberText),f('最佳分区','sjr_best_quartile'),f('H 指数','h_index',numberText),f('学科及分区','categories'),f('当年发文量','total_docs_year',numberText),f('三年发文量','total_docs_3years',numberText),f('三年被引次数','total_citations_3years',numberText),f('开放获取','open_access',yesNo),f('覆盖年份','coverage')];
  if (source === 'cwts') return [...base,f('SNIP','snip',numberText),f('IPP','ipp',numberText),f('统计论文数','p',numberText)];
  if (source === 'ccf') return [...base,f('CCF 等级','ccf_rank'),f('学科领域','field'),f('目录类别','ccf_category'),f('期刊缩写','abbreviation'),linkFact('目录链接',r.url)];
  if (source === 'gjqk') return [...base,f('预警等级','warning_level'),f('预警原因','warning_reason')];
  if (source === 'openalex') return [f('累计发文量','works_count',numberText),f('累计被引次数','cited_by_count',numberText),f('H 指数','summary_stats_h_index',numberText),f('i10 指数','summary_stats_i10_index',numberText),f('两年平均被引次数','summary_stats_2yr_mean_citedness',numberText),f('开放获取论文数','oa_works_count',numberText),f('开放获取','is_oa',yesNo),f('DOAJ 收录','is_in_doaj',v=>yesNo(v,'已收录','未收录')),f('最早发表年份','first_publication_year',yearLabel),f('最近发表年份','last_publication_year',yearLabel),f('来源更新日期','updated_date',dateLabel),linkFact('期刊官网',r.homepage_url)];
  if (source === 'crossref') return [f('来源刊名','title'),f('出版商','publisher'),f('登记 DOI 总数','counts_total_dois',numberText),f('当前卷期 DOI 数','counts_current_dois',numberText),f('回溯卷期 DOI 数','counts_backfile_dois',numberText)];
  if (source === 'jufo') return [f('芬兰等级','level_fi'),f('挪威等级','level_no'),f('丹麦等级','level_dk'),f('创刊年份','year_start',yearLabel),f('出版语言','language',languageName),f('出版商','publisher'),f('来源更新日期','modified_at',dateLabel),linkFact('期刊官网',r.website)];
  if (source === 'doaj') return [f('出版商','publisher_name'),f('国家 / 地区','publisher_country',countryName),f('开放获取起始年份','oa_start',yearLabel),f('收取文章处理费','apc_has_apc',yesNo),f('提供费用减免','waiver_has_waiver',yesNo),f('发表周期（周）','publication_time_weeks',numberText),f('作者保留版权','copyright_author_retains',yesNo),f('来源更新日期','last_updated',dateLabel),linkFact('作者指南',r.ref_author_instructions),linkFact('费用说明',r.apc_url)];
  if (source === 'wikidata') return [f('中文名称','label_zh'),f('英文名称','label_en'),f('期刊缩写','iso4_abbreviation'),f('创刊年份','inception',dateLabel),f('国家 / 地区','country_label',countryName),f('中文简介','description_zh'),fact('出版语言',list(data.sources?.wikidata?.languages).map(v=>languageName(v.language_label)),undefined,joinText),fact('收录数据库',list(data.sources?.wikidata?.indexed_in).map(v=>v.database_label),undefined,joinText)];
  return [];
}

export function submissionView(data: Data) {
  const s = record(data.sources?.submission), r = record(s.main), u = {...record(data.unified?.submission),...record(data.submission)}, f = record(s.fields_json), latex = record(s.latex), doaj = main(data,'doaj');
  const reviewNames:Data={'single-blind':'单盲审稿','double-blind':'双盲审稿',single_blind:'单盲审稿',double_blind:'双盲审稿',open:'开放审稿'};
  const linkValues = [['投稿系统', f.submission?.system_url], ['作者指南',first(s.guide_master,r.guide_master,f.guide_master,doaj.ref_author_instructions)], ['费用说明',doaj.apc_url], ['LaTeX 模板',first(latex.url,latex.download_url,latex.master_zip)]];
  const links = linkValues.map(([label,url])=>linkFact(label,url)).filter(x=>x.href);
  const requirements = [
    fact('正文篇幅（字数）',first(f.word_limit,r.word_limit),undefined,numberText), fact('页数限制',first(f.page_limit,r.page_limit),undefined,numberText),
    fact('摘要字数',f.abstract?.word_limit,undefined,numberText), fact('审稿方式',reviewNames[String(first(f.peer_review?.model,r.peer_review_model))] || first(f.peer_review?.model,r.peer_review_model)),
    fact('可提交文件格式',list(f.submission?.file_formats).map(v=>text(v).toUpperCase()),undefined,joinText),fact('参考文献格式',first(f.reference_style?.list,r.reference_style_list),undefined,joinText),
    fact('文内引用',({superscript:'上标编号','author-year':'作者—年份',numeric:'数字编号'} as Data)[f.reference_style?.intext] || f.reference_style?.intext),
    fact('图片格式',f.figures?.formats,undefined,joinText),fact('LaTeX 模板类',first(latex.class,u.latex_class)),
    fact('模板文件',safeUrl(latex.master_zip)?null:latex.master_zip),
  ];
  const summary = [fact('投稿要求资料',first(r.has_guideline,u.has_guideline),undefined,v=>yesNo(v,'已保存','暂未保存')),fact('LaTeX 模板记录',first(r.has_latex,u.has_latex),undefined,v=>yesNo(v,'有记录','暂无记录'))];
  return {summary,requirements,links,hasLinks:links.length>0};
}

export function feedbackView(data: Data) {
  const s = record(data.sources?.comments), u = {...record(data.unified?.comments),...record(data.comments)};
  const confidence:Data = {low:'较低',medium:'中等',high:'较高'};
  const count=first(s.comment_count,u.count), rating=first(s.journal_rating_0_5,u.rating);
  return {
    summary:[fact('反馈数量',count,undefined,numberText),fact('综合评分',rating,'满分 5 分',numberText),fact('平均处理时间',s.avg_total_handling_days,'天',numberText),fact('评分可信度',confidence[first(s.rating_confidence,u.rating_confidence)] || null)],
    comments:list(s.recent).map(c=>({
      author:text(c.publisher_display_name)===missing?'匿名作者':text(c.publisher_display_name), content:text(c.comment_text), date:dateLabel(c.published_at_iso),
      source:text(c.source_name), url:safeUrl(c.source_url), tags:list(c.tags).filter(v=>typeof v==='string'),
      facts:[fact('评分',c.overall_rating_0_5,'满分 5 分',numberText),fact('处理时间',c.total_handling_days,'天',numberText),fact('首轮审稿',c.first_review_days,'天',numberText),fact('投稿结果',({accepted:'已接收',rejected:'已拒稿',desk_reject:'编辑退稿',revision:'修改中',pending:'处理中',unknown:'未明确'} as Data)[c.paper_status] || '未明确')],
    })),
  };
}

export function relatedView(value: any) {
  const r=record(value);
  return {
    journals:list(r.same_category).map(j=>({id:Number(j.journal_id || j.id),name:text(first(j.canonical_name,j.name,j.title)),category:categoryName(first(j.major_category,j.category)),impact:numberText(first(j.jcr_impact_factor,j.impact_factor,j.impact))})),
    papers:list(r.recent_papers).map(p=>({title:text(first(p.title,p.display_name)),year:yearLabel(first(p.publication_year,p.year)),authors:joinText(list(p.authors).map(a=>typeof a==='string'?a:first(a.display_name,a.name))),url:safeUrl(first(p.url,p.doi && (/^https?:/.test(p.doi)?p.doi:'https://doi.org/'+p.doi),p.primary_location?.landing_page_url))})),
    hubs:[['学科专题',r.category_hub],['国家 / 地区专题',r.country_hub]].map(([label,hub]:any)=>({label,name:text(first(hub?.name,hub?.label,hub?.title)),url:safeUrl(hub?.url)})).filter(h=>h.name!==missing),
  };
}
