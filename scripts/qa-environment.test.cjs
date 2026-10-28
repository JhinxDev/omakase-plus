const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const {qaRuntime, qaOutput, qaPython, qaBrowser} = require('./qa-environment.cjs');

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
test('every optional title/collection QA entry uses the portable helper', () => {
  for (const name of fs.readdirSync(__dirname).filter(name => /^check_.*\.cjs$/.test(name))) {
    const source = fs.readFileSync(path.join(__dirname, name), 'utf8');
    assert.ok(source.includes('qaRuntime()'), name);
    assert.ok(source.includes('qaBrowser()'), name);
  }
});
