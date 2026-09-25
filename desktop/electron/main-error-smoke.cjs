const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

exports.run = async ({win, app}) => {
  const out = process.env.RESEARCH_QA_OUT;
  const report = {passed: false, assertions: []};
  const execute = code => win.webContents.executeJavaScript(code, true);
  try {
    const ready = await execute('Boolean(window.research && document.querySelector(".library-header"))');
    assert.ok(ready, 'renderer mounted');
    report.assertions.push('renderer mounted');
    for (const method of ['researchAsk.list', 'projects.list', 'smartCollections.list']) {
      const result = await execute(`window.research.call(${JSON.stringify(method)})`);
      assert.ok(Array.isArray(result), `${method} returns a list`);
      report.assertions.push(method);
    }
    const missing = await execute(`fetch('app://local/missing-resource.txt').then(response => response.status)`);
    assert.equal(missing, 404);
    report.assertions.push('missing resource returns 404');
    const invalid = await execute(`fetch('app://local/%ZZ').then(response => response.status)`);
    assert.equal(invalid, 500);
    report.assertions.push('protocol exception returns 500');
    const log = fs.readFileSync(path.join(app.getPath('userData'), 'main.log'), 'utf8');
    assert.match(log, /app protocol/);
    report.assertions.push('original protocol exception saved');
    report.passed = true;
  } catch (error) {
    report.error = error.stack;
  }
  if (out) fs.writeFileSync(path.join(out, 'main-error-smoke.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
  process.exitCode = report.passed ? 0 : 1;
  app.quit();
};
