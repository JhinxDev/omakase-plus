/* Offline geometry regression for the actual rendered title template and CSS cascade.
   Args: fixture HTML, output directory, optional installed Playwright module path. */
const fs = require('fs');
const path = require('path');
const {qaRuntime,qaBrowser}=require('../../scripts/qa-environment.cjs');
const { chromium } = require(process.argv[4] || require.resolve('playwright',{paths:[qaRuntime()]}));
const product = path.resolve(__dirname, '../../src/omakase/plus');
const fixture = process.argv[2];
const output = process.argv[3];
if (!fixture || !output) throw Error('Pass synthetic fixture HTML and an output directory.');
const base = fs.readFileSync(path.join(product, 'templates/base.html'), 'utf8');
const styles = [...new Set([...base.matchAll(/href="\/plus\/static\/([^?"/]+\.css)/g)].map(match => match[1]))];
const css = styles.map(name => fs.readFileSync(path.join(product, 'static', name), 'utf8')).join('\n');
const font = fs.readFileSync(path.join(product, 'static/geist-variable.woff2')).toString('base64');
const fontRule = '@font-face{font-family:Geist;src:url(data:font/woff2;base64,' + font + ') format("woff2");font-weight:100 900;font-display:swap}';
const placeholder = 'data:image/svg+xml;base64,' + Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="900"><rect width="600" height="900" fill="#1d2430"/></svg>').toString('base64');
const html = fs.readFileSync(fixture, 'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '').replace(/<link\b[^>]*>/gi, '').replace(/(<img\b[^>]*\bsrc=")[^"]*(")/gi, '$1' + placeholder + '$2');
function requireThat(condition, message) { if (!condition) throw Error(message); }
(async () => {
  fs.mkdirSync(output, {recursive:true});
  const browser = await chromium.launch(qaBrowser());
  const receipts = [];
  try {
    for (const width of [1440,390,320]) {
      const context = await browser.newContext({viewport:{width,height:1000}});
      const page = await context.newPage();
      await page.route('**/*', route => route.abort());
      await page.setContent(html);
      await page.addStyleTag({content:css + '\n' + fontRule});
      await page.evaluate(async () => { await document.fonts.ready; });
      const result = await page.evaluate(() => {
        const actions = document.querySelector('.title-library-actions');
        const primary = document.querySelector('.title-primary-actions');
        const following = Array.from(actions.parentElement.querySelectorAll('.title-library-actions ~ .title-progress-source'));
        const button = actions.querySelector('.button');
        const box = node => { const r=node.getBoundingClientRect(); return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,height:r.height,width:r.width}; };
        return {
          primary:box(primary),actions:box(actions),button:box(button),
          details:following.map(node=>({text:node.textContent.trim(),box:box(node),fontSize:getComputedStyle(node).fontSize,lineHeight:getComputedStyle(node).lineHeight})),
          primaryGap:box(actions).top-box(primary).bottom,
          progressGap:box(following[0]).top-box(actions).bottom,
          detailGap:box(following[1]).top-box(following[0]).bottom,
          secondary:button.classList.contains('button-secondary'),
          action:button.getAttribute('href'),
          overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth,
          fontLoaded:Array.from(document.fonts).some(face=>face.family.includes('Geist')&&face.status==='loaded'),
        };
      });
      requireThat(result.primaryGap>=24,'Primary/library separation below24px at'+width);
      requireThat(result.progressGap>=24,'Library/Up-next separation below24px at'+width);
      requireThat(result.detailGap>=8,'Progress detail lines collide at'+width);
      requireThat(result.button.height>=44,'Removal target below44px at'+width);
      requireThat(result.secondary && result.action==='/plus/library/1/remove','Removal must remain the unchanged secondary action');
      requireThat(result.details.every(detail=>parseFloat(detail.fontSize)>=14),'Progress source text below14px at'+width);
      requireThat(result.overflow===0,'Unexpected horizontal page overflow at'+width);
      requireThat(result.fontLoaded,'Actual Geist face not loaded');
      await page.locator('.title-library-actions').scrollIntoViewIfNeeded();
      const screenshot = path.join(output,'title-actions-'+width+'.png');
      await page.locator('.streaming-title-hero').screenshot({path:screenshot});
      receipts.push({width,...result,screenshot});
      await context.close();
    }
  } finally { await browser.close(); }
  fs.writeFileSync(path.join(output,'geometry-receipt.json'),JSON.stringify({method:'Current title.html synthetic fixture, actual CSS cascade and Geist, offline headless Edge',styles,receipts},null,2));
  console.log(JSON.stringify(receipts.map(({width,primaryGap,progressGap,detailGap,button,overflow,fontLoaded})=>({width,primaryGap,progressGap,detailGap,buttonHeight:button.height,overflow,fontLoaded})),null,2));
})().catch(error=>{console.error(error);process.exit(1);});
