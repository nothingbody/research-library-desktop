import assert from 'node:assert/strict';
import {test} from 'node:test';
import {candidatePresentation} from '../src/searchPresentation.ts';

test('candidate summary distinguishes missing evidence from a failed requirement', () => {
  const result = candidatePresentation({
    checks: [
      {label: '柔性作业车间', required: true, status: 'pass', references: [{quote: 'Flexible job shop scheduling is studied.'}]},
      {label: '运输资源', required: true, status: 'unknown'},
    ],
    evidence: [{kind: 'title'}],
  });
  assert.match(result.judgement, /已找到 柔性作业车间/);
  assert.match(result.judgement, /待核验 运输资源/);
  assert.match(result.judgement, /缺少摘要/);
  assert.equal(result.material, '仅题名');
  assert.equal(result.quote, 'Flexible job shop scheduling is studied.');

  const contradicted = candidatePresentation({
    abstract: 'single factory only',
    checks: [{label: '多车间', required: true, status: 'fail'}],
  });
  assert.match(contradicted.judgement, /^不符合必需条件：多车间/);
  assert.equal(contradicted.material, '题名与摘要');
});

test('unverified AI text is not used as a validated summary', () => {
  const input = {fields: {summaryZh: {text: '候选中文解读'}}, explanation: '规则解释'};
  assert.equal(candidatePresentation(input).summary, '规则解释');
  assert.equal(candidatePresentation({...input, verification: 'model-with-validated-quotes'}).summary, '候选中文解读');
});
