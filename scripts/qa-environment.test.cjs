const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {qaRuntime, qaOutput, qaPython, qaBrowser} = require('./qa-environment.cjs');

// Checks that pin their own browser launch instead of going through qaBrowser().
// Every entry must justify itself, and it is only accepted while the script really
// launches the pinned channel directly: if the script is migrated to the helper the
// stale entry fails, so this list cannot become a blanket permission. Keep it short.
const PINNED_BROWSER_CHECKS = new Map([
  ['check_interface_scrollbars.cjs',
    'Measures native Windows Edge scrollbar rendering, so it pins channel msedge and must keep the default --hide-scrollbars argument removed. qaBrowser() cannot express that and an executable override would silently change what is measured.'],
  ['check_search_result_edges.cjs',
    'Measures native Edge scroll container geometry, so it pins channel msedge and must keep the default --hide-scrollbars argument removed for the same reason as the interface scrollbar check.'],
  ['check_movie_feedback.cjs',
    'Renders the feedback list with native Edge scrollbars, so it pins channel msedge and must keep the default --hide-scrollbars argument removed. It also needs the movie feedback fixture app on loopback before it can run.'],
]);

function checkScripts() {
  return fs.readdirSync(__dirname).filter(name => /^check_.*\.cjs$/.test(name)).sort();
}

// Scripts explain the pinned launch in comments, which may name qaBrowser(). Only
// executable code decides whether an exception is still needed.
function withoutComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1');
}

test('portable QA module and output defaults stay inside the checkout or OS temp', () => {
  assert.equal(qaRuntime(), process.env.OMAKASE_QA_NODE_MODULES || path.resolve(__dirname, '../node_modules'));
  assert.equal(qaOutput('sample'), path.join(os.tmpdir(), 'omakase-qa', 'sample'));
});
test('explicit Python and browser settings are honored without starting a process', () => {
  const names = ['OMAKASE_QA_PYTHON', 'OMAKASE_QA_BROWSER_CHANNEL', 'OMAKASE_QA_BROWSER_EXECUTABLE'];
  const prior = Object.fromEntries(names.map(name => [name, process.env[name]]));
  try {
    process.env.OMAKASE_QA_PYTHON = path.join(os.tmpdir(), 'fixture-python');
    process.env.OMAKASE_QA_BROWSER_CHANNEL = 'msedge';
    delete process.env.OMAKASE_QA_BROWSER_EXECUTABLE;
    assert.equal(qaPython(), process.env.OMAKASE_QA_PYTHON);
    assert.deepEqual(qaBrowser(), {headless: true, channel: 'msedge'});
    process.env.OMAKASE_QA_BROWSER_EXECUTABLE = path.join(os.tmpdir(), 'fixture-browser');
    assert.deepEqual(qaBrowser(), {headless: true, executablePath: process.env.OMAKASE_QA_BROWSER_EXECUTABLE});
    delete process.env.OMAKASE_QA_BROWSER_CHANNEL;
    delete process.env.OMAKASE_QA_BROWSER_EXECUTABLE;
    assert.deepEqual(qaBrowser(), {headless: true});
  } finally {
    for (const name of names) if (prior[name] === undefined) delete process.env[name]; else process.env[name] = prior[name];
  }
});
test('every optional title/collection QA entry resolves modules and browser portably', () => {
  for (const name of checkScripts()) {
    const source = fs.readFileSync(path.join(__dirname, name), 'utf8');
    assert.ok(source.includes('qaRuntime()'), `${name} must resolve modules through qaRuntime()`);
    const pinnedReason = PINNED_BROWSER_CHECKS.get(name);
    if (pinnedReason === undefined) {
      assert.ok(source.includes('qaBrowser()'), `${name} must launch through qaBrowser(), or be listed in PINNED_BROWSER_CHECKS with a reason`);
      continue;
    }
    assert.ok(pinnedReason.trim().length >= 60, `${name} needs a specific reason in PINNED_BROWSER_CHECKS`);
    assert.ok(!withoutComments(source).includes('qaBrowser()'), `${name} is listed in PINNED_BROWSER_CHECKS but now uses qaBrowser(); remove the stale entry`);
    assert.ok(/channel\s*:\s*'msedge'/.test(source), `${name} is listed in PINNED_BROWSER_CHECKS but no longer pins the msedge channel`);
    assert.ok(source.includes("ignoreDefaultArgs"), `${name} is listed in PINNED_BROWSER_CHECKS but no longer removes the default --hide-scrollbars argument`);
  }
});
test('pinned browser exceptions are current, and each one is justified', () => {
  const scripts = new Set(checkScripts());
  for (const [name, reason] of PINNED_BROWSER_CHECKS) {
    assert.ok(scripts.has(name), `PINNED_BROWSER_CHECKS lists ${name}, which is not a check script; remove the stale entry`);
    assert.ok(reason.trim().length >= 60, `${name} needs a specific reason in PINNED_BROWSER_CHECKS`);
  }
});
