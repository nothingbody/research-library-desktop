const CSL = require('citeproc');

function text(value) {
  return String(value || '').replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();
}

function render({clusters, records, styleXml, localeXml, locale = 'zh-CN'}) {
  if (!Array.isArray(clusters)) throw new Error('引文组格式不正确');
  const itemMap = Object.fromEntries(records.map(record => [record.id, record]));
  const sys = {
    retrieveLocale: language => localeXml(language || locale),
    retrieveItem: id => itemMap[id],
  };
  const engine = new CSL.Engine(sys, styleXml, locale);
  const ordered = [...clusters].sort((a, b) => Number(a.ordinal || 0) - Number(b.ordinal || 0));
  const rendered = new Map();
  for (let index = 0; index < ordered.length; index += 1) {
    const cluster = ordered[index];
    const citationItems = (cluster.items || []).map(entry => ({
      id: entry.itemId,
      locator: entry.locator || undefined,
      label: entry.label || undefined,
      prefix: entry.prefix || undefined,
      suffix: entry.suffix || undefined,
      'suppress-author': !!entry.suppressAuthor,
    }));
    if (!citationItems.length) throw new Error('引文组至少需要一篇文献');
    const before = ordered.slice(0, index).map((row, number) => [row.id, number + 1]);
    // citeproc-js only accepts post-citation IDs it has already registered.  Replaying
    // clusters from the start therefore needs no post list: every later cluster will
    // be registered in the next iteration, while the current citation order remains
    // deterministic through `before`.
    const response = engine.processCitationCluster({citationID: cluster.id, citationItems, properties: {noteIndex: index + 1}}, before, []);
    for (const update of response[1] || []) rendered.set(update[2], update[1]);
  }
  const bibliography = engine.makeBibliography();
  const entries = bibliography ? bibliography[1] : [];
  return {
    citations: ordered.map(cluster => ({id: cluster.id, text: text(rendered.get(cluster.id) || ''), ordinal: cluster.ordinal})),
    bibliography: {html: entries.join(''), text: entries.map(text).join('\n')},
  };
}

module.exports = {render};
