/* Offline geometry and reachability regression for the rendered title page: the real
   title template, the real CSS cascade and the real Geist face, no network, no writes.
   The fixture is rendered on demand from tests/plus/test_title_episode_states.py, the same
   source the wired scripts/check_title_details.cjs uses, so no pre-built HTML is needed.
   Usage: node tests/browser/title_action_spacing_browser.cjs [fixture.html] [output dir] [playwright module] [--report|--self-test] */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {qaRuntime, qaBrowser, qaOutput, qaPython} = require('../../scripts/qa-environment.cjs');
const positional = process.argv.slice(2).filter(argument => !argument.startsWith('--'));
const reportOnly = process.argv.includes('--report');
const selfTest = process.argv.includes('--self-test');
const {chromium} = require(positional[2] || require.resolve('playwright', {paths: [qaRuntime()]}));
const root = path.resolve(__dirname, '../..');
const product = path.resolve(root, 'src/omakase/plus');
const output = positional[1] || qaOutput('title_action_spacing');
/* Measured on the rendered fixture at 1440, 390 and 320 before these floors were set:
   primary gap 281 to 315, next element gap 24, removal target 96.375. */
const PRIMARY_GAP = 24;
const NEXT_GAP = 24;
const TARGET_HEIGHT = 44;
const SOURCE_FONT = 14;
function requireThat(condition, message) { if (!condition) throw Error(message); }
function fixtureHtml() {
  if (positional[0]) return fs.readFileSync(positional[0], 'utf8');
  const rendered = spawnSync(qaPython(), ['-X', 'utf8', '-c',
    'import runpy; fixture=runpy.run_path("tests/plus/test_title_episode_states.py"); print(fixture["render_title"]())'],
    {cwd: root, encoding: 'utf8', env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});
  if (rendered.status !== 0) throw Error('Fixture render failed: ' + (rendered.stderr || rendered.stdout));
  return rendered.stdout;
}
/* Every stylesheet the title page actually loads: base.html plus the block extra_head
   in title.html, which is where series-detail.css comes from. */
const styles = [...new Set(['templates/base.html', 'templates/title.html'].flatMap(template =>
  [...fs.readFileSync(path.join(product, template), 'utf8').matchAll(/href="\/plus\/static\/([^?"/]+\.css)/g)].map(match => match[1])))];
/* The shipped face is Inter, declared in cinema.css against a /plus/static URL. Network is
   aborted here, so that declaration is dropped and the same face is inlined from the same file. */
const css = styles.map(name => fs.readFileSync(path.join(product, 'static', name), 'utf8'))
  .join('\n').replace(/@font-face\s*\{[^}]*inter-variable[^}]*\}/gi, '');
const font = fs.readFileSync(path.join(product, 'static/inter-variable.woff2')).toString('base64');
const fontRule = '@font-face{font-family:Inter;src:url(data:font/woff2;base64,' + font + ') format("woff2");font-style:normal;font-weight:100 900;font-display:swap}';
const placeholder = 'data:image/svg+xml;base64,' + Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="900"><rect width="600" height="900" fill="#1d2430"/></svg>').toString('base64');
const html = fixtureHtml()
  .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
  .replace(/<link\b[^>]*>/gi, '')
  .replace(/(<img\b[^>]*\bsrc=")[^"]*(")/gi, '$1' + placeholder + '$2');
/* Serialized into the page. Probes document order, not next siblings: the band is the last
   child of its own group in every revision measured, so a sibling-only probe finds nothing. */
const measure = () => {
  const actions = document.querySelector('.title-library-actions');
  const primary = document.querySelector('.title-primary-actions');
  const box = node => { const r = node.getBoundingClientRect(); return {top: r.top, bottom: r.bottom, left: r.left, right: r.right, height: r.height, width: r.width}; };
  const paints = node => { const r = node.getBoundingClientRect(); return r.height > 0 && r.width > 0 && getComputedStyle(node).visibility !== 'hidden'; };
  const successor = () => {
    let node = actions;
    while (node && node !== document.body) {
      for (let sibling = node.nextElementSibling; sibling; sibling = sibling.nextElementSibling) if (paints(sibling)) return sibling;
      node = node.parentElement;
    }
    return null;
  };
  const disclosures = [];
  for (let parent = actions.parentElement; parent; parent = parent.parentElement) if (parent.tagName === 'DETAILS') disclosures.push(parent);
  const button = actions.querySelector('.button');
  const next = successor();
  return {
    primary: box(primary), actions: box(actions), button: box(button),
    nextElement: next ? next.tagName.toLowerCase() + '.' + String(next.className || '').split(/\s+/).filter(Boolean).join('.') : null,
    primaryGap: box(actions).top - box(primary).bottom,
    nextGap: next ? box(next).top - box(actions).bottom : null,
    openDisclosures: disclosures.length,
    insideTitleViewProgress: Boolean(actions.closest('.title-view-progress')),
    groupedWithWatchControls: actions.parentElement.classList.contains('series-management'),
    sourceFontSizes: Array.from(actions.querySelectorAll('.title-progress-source')).map(node => parseFloat(getComputedStyle(node).fontSize)),
    role: actions.getAttribute('role'), label: actions.getAttribute('aria-label'),
    secondary: button.classList.contains('button-secondary'), action: button.getAttribute('href'),
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    stack: getComputedStyle(document.querySelector('.title-view-hero') || document.body).fontFamily,
    fontLoaded: Array.from(document.fonts).some(face => face.family.replace(/["']/g, '') === 'Inter' && face.status === 'loaded'),
  };
};
function check(result, width) {
  requireThat(result.openDisclosures === 0, 'Library actions must stay reachable without opening a disclosure at ' + width);
  requireThat(result.insideTitleViewProgress, 'Library actions left the styled hero, so removal.css no longer applies at ' + width);
  requireThat(result.groupedWithWatchControls, 'Library actions left the watch control group, so the series-management grid no longer applies at ' + width);
  requireThat(result.primaryGap >= PRIMARY_GAP, 'Primary/library separation below ' + PRIMARY_GAP + 'px at ' + width);
  requireThat(result.nextGap === null || result.nextGap >= NEXT_GAP, 'Library actions collide with ' + result.nextElement + ' at ' + width);
  requireThat(result.button.height >= TARGET_HEIGHT, 'Removal target below ' + TARGET_HEIGHT + 'px at ' + width);
  requireThat(result.secondary && /^\/plus\/library\/\d+\/remove$/.test(result.action || ''), 'Removal must remain the unchanged secondary action, saw ' + result.action);
  requireThat(result.role === 'group' && result.label === 'Library actions', 'Library actions lost its named group at ' + width);
  requireThat(result.sourceFontSizes.every(size => size >= SOURCE_FONT), 'Progress source text below ' + SOURCE_FONT + 'px at ' + width);
  requireThat(result.overflow === 0, 'Unexpected horizontal page overflow at ' + width);
  requireThat(result.fontLoaded, 'Actual Inter face not loaded');
  requireThat(/Inter/.test(result.stack), 'The hero does not select the shipped Inter stack, saw ' + result.stack);
}
(async () => {
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch(qaBrowser());
  const receipts = [];
  try {
    for (const width of [1440, 390, 320]) {
      const context = await browser.newContext({viewport: {width, height: 1000}});
      const page = await context.newPage();
      await page.route('**/*', route => route.abort());
      await page.setContent(html);
      await page.addStyleTag({content: css + '\n' + fontRule});
      await page.evaluate(async () => { await document.fonts.ready; });
      const result = await page.evaluate(measure);
      if (!reportOnly) check(result, width);
      await page.locator('.streaming-title-hero').screenshot({path: path.join(output, 'title-actions-' + width + '.png')});
      if (selfTest) {
        /* A gate is evidence only if it rejects the failures it exists for. Each case breaks one
           measured contract on a fresh copy of the same page and must be refused, for that reason. */
        const negatives = [
          {name: 'collision with the next block', expect: /collide with/,
            break: () => { const next = document.querySelector('details.title-technical-details'); next.style.position = 'relative'; next.style.top = '-20px'; }},
          {name: 'removal target below the touch floor', expect: /below 44px/,
            style: '.title-library-actions .button{min-height:0 !important;height:1px !important;padding:0 !important;line-height:1 !important}'},
          {name: 'removal action rewritten', expect: /secondary action/,
            break: () => { document.querySelector('.title-library-actions .button').setAttribute('href', '/plus/library/1/remove-now'); }},
          {name: 'band moved out of the styled hero', expect: /left the styled hero/,
            break: () => { document.body.appendChild(document.querySelector('.title-library-actions')); }},
          {name: 'band behind a disclosure, which is the regression this gate exists for', expect: /without opening a disclosure/,
            break: () => { document.querySelector('details.title-technical-details').appendChild(document.querySelector('.title-library-actions')); }},
        ];
        for (const negative of negatives) {
          await page.setContent(html);
          await page.addStyleTag({content: css + '\n' + fontRule});
          await page.evaluate(() => document.fonts.ready);
          if (negative.style) await page.addStyleTag({content: negative.style});
          if (negative.break) await page.evaluate(negative.break);
          let rejected = null;
          try { check(await page.evaluate(measure), width); } catch (error) { rejected = error.message; }
          requireThat(rejected, 'Self-test: ' + negative.name + ' was accepted at ' + width);
          requireThat(negative.expect.test(rejected), 'Self-test: ' + negative.name + ' was refused for the wrong reason: ' + rejected);
          console.log(JSON.stringify({width, negative: negative.name, refused: rejected}));
        }
      }
      receipts.push({width, ...result});
      await context.close();
    }
  } finally { await browser.close(); }
  fs.writeFileSync(path.join(output, 'geometry-receipt.json'), JSON.stringify({
    method: 'Rendered tests/plus/test_title_episode_states.py fixture, actual title.html CSS cascade including block extra_head, inlined Geist, offline headless Edge',
    styles, floors: {primaryGap: PRIMARY_GAP, nextGap: NEXT_GAP, targetHeight: TARGET_HEIGHT, sourceFont: SOURCE_FONT}, receipts}, null, 2));
  if (reportOnly) console.log(JSON.stringify(receipts.map(({width, primaryGap, nextGap, nextElement, button, openDisclosures, insideTitleViewProgress, groupedWithWatchControls}) => ({width, primaryGap, nextGap, nextElement, buttonHeight: button.height, openDisclosures, insideTitleViewProgress, groupedWithWatchControls})), null, 2));
  else console.log(JSON.stringify({result: selfTest ? 'PASS (with negative cases)' : 'PASS', cases: receipts.length, widths: receipts.map(receipt => receipt.width)}));
})().catch(error => { console.error(error); process.exit(1); });
