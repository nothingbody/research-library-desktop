import test from 'node:test';
import assert from 'node:assert/strict';
import {buildTranslationLayout, findAlignedRange, findSelectedParagraph} from '../src/translationLayout.ts';

const viewport = {convertToViewportPoint: (x, y) => [x, y]};
const item = (str, x, y, width, height = 10) => ({
  str, transform: [height, 0, 0, height, x, y], width, height,
});

test('keeps superscripts with their author row and separates a nearby column', () => {
  const layout = buildTranslationLayout({items: [
    item('Shoujing Zhang', 36, 100, 80), item('1', 117, 98, 3, 6),
    item(', Tiantian Hou', 121, 100, 80),
    item('Citation: Zhang et al.', 36, 140, 90, 8),
    item('Abstract: Scheduling problem', 166, 140, 180, 9),
    item('under dual resource constraints', 166, 153, 180, 9),
  ]}, viewport, 1);
  assert.equal(layout.paragraphs.length, 3);
  assert.match(layout.paragraphs[0].text, /Shoujing Zhang\s?1, Tiantian Hou/);
  assert.ok(layout.paragraphs.some(paragraph => paragraph.text === 'Citation: Zhang et al.'));
  assert.ok(layout.paragraphs.some(paragraph => /Abstract: Scheduling problem under dual resource constraints/.test(paragraph.text)));
});

test('preserves crowded table cells while retaining prose and caption', () => {
  const layout = buildTranslationLayout({items: [
    item('A paragraph before the table.', 30, 100, 220),
    item('Table 1. Scheduling results', 30, 140, 180),
    item('Machine', 30, 170, 55), item('Factory', 130, 170, 55), item('Runtime', 230, 170, 55),
    item('M1', 30, 183, 15), item('F1', 130, 183, 15), item('1/3', 230, 183, 15),
    item('A paragraph after the table.', 30, 220, 220),
  ]}, viewport, 1);
  assert.deepEqual(layout.paragraphs.map(paragraph => paragraph.text), [
    'A paragraph before the table.', 'Table 1. Scheduling results',
    'A paragraph after the table.',
  ]);
  assert.equal(layout.lines.length, 3);
});

test('locates the source paragraph by PDF selection rectangle', () => {
  const paragraphs = [
    {id: '1', text: 'Introduction', left: 30, top: 90, width: 100, height: 14},
    {id: '2', text: 'The scheduling method improves results.', left: 160, top: 100, width: 300, height: 55},
  ];
  assert.equal(findSelectedParagraph(paragraphs, [[205, 117, 250, 130]], 'scheduling')?.id, '2');
  assert.equal(findSelectedParagraph(paragraphs, [], 'Introduction')?.id, '1');
});

test('highlights the aligned phrase and disambiguates repeated target words', () => {
  const source = 'The method improves scheduling. Another scheduling method is tested.';
  const translated = '该方法改进了调度。另一种调度方法也经过测试。';
  assert.deepEqual(findAlignedRange(source, 'Another scheduling', translated, '调度'), {start: 12, end: 14});
  assert.deepEqual(findAlignedRange(source, 'scheduling', translated, '调度', .7), {start: 12, end: 14});
  assert.equal(findAlignedRange(source, 'scheduling', translated, '不存在'), null);
  assert.equal(findAlignedRange(source, 'scheduling', translated, translated), null);
});

test('keeps an inline abstract label with its first sentence', () => {
  const layout = buildTranslationLayout({items: [
    item('Abstract:', 54, 100, 48), item('The current global production environment', 121, 100, 270),
    item('requires new scheduling strategies.', 54, 114, 250),
  ]}, viewport, 1);
  assert.equal(layout.paragraphs.length, 1);
  assert.match(layout.paragraphs[0].text, /^Abstract: The current global production environment/);
});
