/* Optional local browser-QA configuration; no workstation-specific defaults. */
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
function qaRuntime() { return process.env.OMAKASE_QA_NODE_MODULES || path.join(root, 'node_modules'); }
function qaOutput(name) { return path.join(os.tmpdir(), 'omakase-qa', name); }
function qaPython() {
  if (process.env.OMAKASE_QA_PYTHON) return process.env.OMAKASE_QA_PYTHON;
  const candidate = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  if (!fs.existsSync(candidate)) throw new Error('Prepare the locked Python environment or set OMAKASE_QA_PYTHON.');
  return candidate;
}
function qaBrowser() {
  const options = {headless: true};
  if (process.env.OMAKASE_QA_BROWSER_EXECUTABLE) options.executablePath = process.env.OMAKASE_QA_BROWSER_EXECUTABLE;
  else if (process.env.OMAKASE_QA_BROWSER_CHANNEL) options.channel = process.env.OMAKASE_QA_BROWSER_CHANNEL;
  return options;
}
module.exports = {qaRuntime, qaOutput, qaPython, qaBrowser};
