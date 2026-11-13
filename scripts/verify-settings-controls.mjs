/* Run against the actual synthetic app, never an account with writable settings:
 * playwright_cli.ps1 -s=settings-controls open http://127.0.0.1:4415/plus/settings/downloads --browser msedge
 * playwright_cli.ps1 -s=settings-controls run-code --filename scripts/verify-settings-controls.mjs
 * Screenshots use the CLI working directory's existing output/playwright folder.
 */
async page => {
  const url = new URL(page.url());
  if (!['127.0.0.1', 'localhost'].includes(url.hostname) || !url.pathname.endsWith('/plus/settings/downloads')) {
    throw new Error('Use the local synthetic Download Criteria preview.');
  }
  // Toggle only DOM state. Reject every attempted write, including accidental submits.
  await page.route('**/*', route => ['GET', 'HEAD'].includes(route.request().method()) ? route.fallback() : route.abort());
  const results = [];
  for (const width of [1440, 1101, 390, 320]) {
    await page.setViewportSize({width, height:1000});
    await page.reload();
    const switches = page.locator('.policy-switches');
    await switches.waitFor();
    const rows = await switches.locator('label').evaluateAll(labels => labels.map(label => {
      const input = label.querySelector('input[type=checkbox]');
      const rect = label.getBoundingClientRect();
      const box = input.getBoundingClientRect();
      const range = document.createRange();
      range.selectNodeContents(label);
      const textRects = Array.from(label.childNodes).filter(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim()).flatMap(node => {
        range.selectNodeContents(node);
        return Array.from(range.getClientRects()).map(r => ({width:r.width,height:r.height}));
      });
      return {name:input.name,label:{width:rect.width,height:rect.height},box:{width:box.width,height:box.height},textRects};
    }));
    if (rows.length !== 3) throw new Error(`Expected three preference switches at ${width}px`);
    for (const row of rows) {
      if (row.box.width < 16 || row.box.width > 24 || Math.abs(row.box.width-row.box.height) > 1) {
        throw new Error(`Oversized or distorted checkbox at ${width}px: ${JSON.stringify(row)}`);
      }
      if (row.label.width < 140 || row.label.height < 44 || row.label.height > 120 || row.textRects.length > 5 || !row.textRects.some(r=>r.width > 100)) {
        throw new Error(`Unreadable switch label at ${width}px: ${JSON.stringify(row)}`);
      }
    }
    const overflow = await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth);
    if (overflow > 1) throw new Error(`Horizontal overflow ${overflow}px at ${width}px`);
    // Clicking the text side of the wrapping label must activate its native input.
    for (const label of await switches.locator('label').all()) {
      const input = label.locator('input');
      const initial = await input.isChecked();
      const rect = await label.boundingBox();
      await label.click({position:{x:Math.min(rect.width-8,80),y:rect.height/2}});
      if (await input.isChecked() === initial) throw new Error(`Label did not toggle checkbox at ${width}px`);
      await input.setChecked(initial);
    }
    const number = page.getByRole('spinbutton', {name:'Maximum release size'});
    if ((await number.boundingBox()).width < 100) throw new Error(`Numeric field was compressed at ${width}px`);
    await switches.scrollIntoViewIfNeeded();
    await page.screenshot({path:`output/playwright/settings-controls-${width}.png`});
    results.push({width,overflow,rows,labelActivation:true});
  }
  return {passed:true,results};
}
