/* Bounded shared-style checks on actual templates and synthetic account data. */
const {qaRuntime, qaOutput, qaPython, qaBrowser} = require('./qa-environment.cjs');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const {chromium} = require(require.resolve('playwright', {paths: [qaRuntime()]}));
const root = path.resolve(__dirname, '..');
const output = process.env.OMAKASE_CONSISTENCY_QA_OUTPUT || qaOutput('web_consistency');
fs.mkdirSync(output, {recursive: true});
const fixture = `import json,runpy
from datetime import datetime,timezone
from pathlib import Path
from jinja2 import Environment,FileSystemLoader,select_autoescape
env=Environment(loader=FileSystemLoader(Path('src/omakase/plus/templates')),autoescape=select_autoescape(['html']))
env.globals['linked_devices_enabled']=lambda:True
env.globals['download_language_settings_enabled']=lambda:True
common=dict(csrf_token='fixture',email='viewer@example.test',display_name='Viewer',asset_version='fixture',request=dict(url=dict(path='/plus/home')),households_enabled=True,household_navigation=dict(available=True,selected_id='personal',libraries=[dict(id='personal',name='Personal library',kind='personal'),dict(id='shared',name='Shared Home',kind='household')]))
title=runpy.run_path('tests/plus/test_streaming_templates.py')['title_context']()
title.update(play_url='/plus/watch/7/11?play=1',play_label='Resume',play_episode=11,pending_confirmations=[])
preview=runpy.run_path('scripts/preview_plus_workspace.py')
stamp=datetime(2026,11,10,tzinfo=timezone.utc)
device=dict(name='Living room',reference='fixture-device',temporary=False,shared=False,last_seen=stamp,network='192.0.2.xxx',expiry=stamp)
household=dict(id='shared',name='Shared Home',role='owner',members_can_add=False,acquisition_requests=False)
member=dict(label='Alex',role='member',member_id='member',joined_at='2026-11-01',can_request_titles=True,can_add_titles=False)
def render(name,**context):return env.get_template(name).render(**common,**context)
pages=dict(home=preview['render']('home'),devices=render('native_tv_library.html',active_page='settings',devices=[device],device_owner='you',device_household=None,device_code='',is_household_owner=False,household_device_count=0),household=render('household.html',active_page='settings',household=household,members=[member],title_requests=[],invitations=[],shared_services=[]),title=render('title.html',active_page='library',title=title),player=render('player.html',active_page='watch',title='Synthetic playback study',episode_number=2,episode_name='Fixture episode name',episode_summary='',description_preview='',library_id='personal',overview_url='/plus/library/7',next_url='/plus/watch/7/3?play=1',previous_url='/plus/watch/7/1',season_number=1))
search=env.get_template('_collection_search.html').module.collection_search('/plus/library','anime','Your library','Search anime',collapsible=True)
pages['search']=pages['home'].replace('<section class="cinema-hero"',str(search)+'<section class="cinema-hero"',1)
pages['player-end']=render('player.html',active_page='watch',title='Synthetic final episode',episode_number=2,library_id='personal',overview_url='/plus/library/7',next_url='',previous_url='/plus/watch/7/1',season_number=1)
pages['home-short']=preview['render']('home',home_titles=preview['fixture_home_titles']()[:1])
print(json.dumps(pages))`;
const pages = JSON.parse(execFileSync(qaPython(), ['-X', 'utf8', '-c', fixture], {
  cwd: root, encoding: 'utf8', env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'},
}));
const staticRoot = path.join(root, 'src/omakase/plus/static');
const files = ['templates/base.html', 'templates/home.html', 'templates/_settings_nav.html',
  'templates/_collection_search.html', 'templates/native_tv_library.html', 'templates/household.html',
  'templates/player.html', 'static/cinema.css', 'static/refinement.css', 'static/devices.css',
  'static/household.css', 'static/interface.css', 'static/series-detail.css', 'static/web-player.css',
  'static/omakase-seal.svg', 'static/product-shell.css', 'static/shelves.js',
  'templates/_home_components.html', 'home_shell.py'];
const receipt = {synthetic: true, sourceSha256: {}, scenarios: [], limitations: ['No live provider, playback or physical-device qualification.']};
const sourceHash = file => crypto.createHash('sha256').update(fs.readFileSync(path.join(root, 'src/omakase/plus', file))).digest('hex');
for (const file of files) receipt.sourceSha256[file] = sourceHash(file);

function contrast(foreground, background) {
  const luminance = color => color.match(/[\d.]+/g).slice(0, 3).map(Number)
    .map(value => value / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4)
    .reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
  const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  return (values[0] + .05) / (values[1] + .05);
}

async function buttonState(page, selector) {
  const button = page.locator(selector).first();
  await button.scrollIntoViewIfNeeded();
  const colors = () => button.evaluate(element => {
    const style = getComputedStyle(element);
    return {background: style.backgroundColor, foreground: style.color, border: style.borderColor};
  });
  await page.mouse.move(0, 0);
  const normal = await colors();
  await button.hover();
  await page.waitForTimeout(180);
  const hover = await colors();
  await page.mouse.move(0, 0);
  await page.waitForTimeout(180);
  await page.keyboard.press('Tab');
  await button.focus();
  const focus = await button.evaluate(element => {
    const style = getComputedStyle(element);
    return {color: style.outlineColor, width: style.outlineWidth, offset: style.outlineOffset, visible: element.matches(':focus-visible')};
  });
  assert(contrast(normal.foreground, normal.background) >= 4.5);
  assert(contrast(hover.foreground, hover.background) >= 4.5);
  assert(focus.visible);
  return {normal, hover, focus};
}

async function disclosure(page, selector) {
  const control = page.locator(selector).first();
  await control.scrollIntoViewIfNeeded();
  await page.keyboard.press('Tab');
  await control.focus();
  const closed = await control.evaluate(element => {
    const style = getComputedStyle(element), marker = getComputedStyle(element, '::before');
    return {radius: style.borderRadius, outline: style.outlineStyle, outlineOffset: style.outlineOffset,
      listStyle: style.listStyleType, marker: marker.maskImage, markerWidth: marker.width, transform: marker.transform};
  });
  assert.equal(closed.radius, '8px');
  assert.equal(closed.listStyle, 'none');
  assert.equal(closed.markerWidth, '16px');
  assert.match(closed.marker, /data:image\/svg\+xml/);
  assert.notEqual(closed.outline, 'none');
  await page.keyboard.press('Enter');
  assert(await control.evaluate(element => element.parentElement.open));
  const opened = await control.evaluate(element => getComputedStyle(element, '::before').transform);
  assert.notEqual(opened, closed.transform);
  await page.keyboard.press('Enter');
  assert.equal(await control.evaluate(element => element.parentElement.open), false);
  return closed;
}

(async () => {
  const browser = await chromium.launch(qaBrowser());
  try {
    for (const scenario of [{width: 1440}, {width: 390}, {width: 1440, textScale: 2}, {width: 390, textScale: 2}]) {
      const context = await browser.newContext({viewport: {width: scenario.width, height: 1000}, reducedMotion: 'reduce'});
      await context.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin !== 'http://consistency.test' || route.request().method() !== 'GET') return route.abort();
        if (pages[url.pathname.slice(1)]) return route.fulfill({contentType: 'text/html', body: pages[url.pathname.slice(1)]});
        if (url.pathname === '/plus/api/notifications') return route.fulfill({json: {items: [], unread_count: 0}});
        if (url.pathname.startsWith('/plus/static/')) {
          const file = path.resolve(staticRoot, url.pathname.slice('/plus/static/'.length));
          if (file.startsWith(staticRoot + path.sep) && fs.existsSync(file)) {
            if (file.endsWith('web-player.js') || file.endsWith('shaka-player-5.3.3.js')) return route.fulfill({contentType: 'application/javascript', body: ''});
            return route.fulfill({path: file});
          }
        }
        if (/\/(poster|banner)$/.test(url.pathname)) return route.fulfill({contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450"><rect width="800" height="450" fill="#292738"/></svg>'});
        return route.fulfill({status: 404, body: ''});
      });
      const page = await context.newPage();
      const evidence = {...scenario, ctas: {}, disclosures: {}, rails: {}};
      for (const surface of ['home', 'devices', 'household', 'title', 'player', 'search']) {
        await page.goto('http://consistency.test/' + surface, {waitUntil: 'networkidle'});
        if (scenario.textScale) await page.addStyleTag({content: 'html{font-size:200%}'});
        if (surface === 'home') {
          assert.match(await page.locator('.cinema-brandmark').getAttribute('src'), /^\/plus\/static\/omakase-seal\.svg/);
          assert.equal(await page.locator('.cinema-wordmark').count(), 0);
          evidence.ctas.home = await buttonState(page, '.cinema-actions .button-primary');
          if (scenario.width === 1440 && !scenario.textScale) {
            const rail = page.locator('#rail-continue-anime');
            const arrow = page.locator('button.shelf-arrow-next[aria-controls="rail-continue-anime"]');
            const previous = page.locator('button.shelf-arrow-previous[aria-controls="rail-continue-anime"]');
            await arrow.waitFor({state: 'visible', timeout: 4000});
            assert(await arrow.isEnabled() && await previous.isDisabled(), 'Overflowing Home rail starts at its leading edge');
            assert(await rail.evaluate(el => el.scrollWidth > el.clientWidth + 2), 'Home fixture must overflow');
            await arrow.focus();
            await page.keyboard.press('Enter');
            await page.waitForFunction(() => document.querySelector('#rail-continue-anime')?.scrollLeft > 2);
            await previous.click();
            await page.waitForFunction(() => document.querySelector('#rail-continue-anime')?.scrollLeft <= 2);
            const style = await arrow.evaluate(element => {
              const css = getComputedStyle(element);
              return {fill: css.backgroundColor, border: css.borderColor, width: element.getBoundingClientRect().width};
            });
            assert(Number(style.fill.match(/[\d.]+/g)[3]) >= .85);
            assert(style.width >= 44);
            // A rail with no overflow must not present an active next button.
            const shortPage = await context.newPage();
            await shortPage.goto('http://consistency.test/home-short', {waitUntil: 'networkidle'});
            const shortRail = shortPage.locator('#rail-continue-anime');
            assert.equal(await shortRail.locator('.om-card').count(), 1, 'Non-overflowing fixture renders exactly one real card');
            assert(await shortRail.evaluate(el => el.scrollWidth <= el.clientWidth + 2));
            const shortArrow = shortPage.locator('button.shelf-arrow-next[aria-controls="rail-continue-anime"]');
            assert(await shortArrow.isDisabled() && !await shortArrow.isVisible(), 'Non-overflowing rail hides Next');
            await shortPage.close();
            evidence.shelfArrow = {...style, keyboard: true, noOverflowHidesControls: true};
          }
        }
        if (surface === 'devices') {
          evidence.ctas.devices = await buttonState(page, '.device-pair-form .button-primary');
          evidence.disclosures.rename = await disclosure(page, '.device-rename > summary');
        }
        if (surface === 'household') {
          evidence.ctas.household = await buttonState(page, '.household-panel .button-primary:visible');
          evidence.disclosures.permissions = await disclosure(page, '.household-member-controls > summary');
        }
        if (surface === 'title') evidence.ctas.title = await buttonState(page, '.series-hero-actions .button-primary');
        if (surface === 'player') {
          await page.locator('[data-player-reload]').evaluate(button => {
            button.closest('.web-player-error').hidden = false;
            button.hidden = false;
          });
          evidence.ctas.player = await buttonState(page, '[data-player-reload]');
          evidence.disclosures.playback = await disclosure(page, '.web-player-tools > summary');
          const episodes = page.getByRole('navigation', {name: 'Episode navigation'});
          const links = [
            ['Next episode', '/plus/watch/7/3?play=1'],
            ['Previous episode', '/plus/watch/7/1'],
            ['All episodes', '/plus/library/7'],
          ];
          for (const [name, href] of links) {
            const link = episodes.getByRole('link', {name, exact: true});
            assert(await link.isVisible(), `${name} remains visible in the episode sidebar`);
            assert.equal(await link.getAttribute('href'), href);
            await page.keyboard.press('Tab');
            await link.focus();
            assert(await link.evaluate(el => el.matches(':focus-visible')));
          }
          evidence.episodeLinks = links.map(([name]) => name);
          if (scenario.width === 1440 && !scenario.textScale) {
            const finalEpisode = await context.newPage();
            await finalEpisode.goto('http://consistency.test/player-end', {waitUntil: 'networkidle'});
            const finalNav = finalEpisode.getByRole('navigation', {name: 'Episode navigation'});
            assert(await finalNav.getByRole('link', {name: 'All episodes', exact: true}).isVisible());
            assert.equal(await finalNav.getByRole('link', {name: 'Next episode', exact: true}).count(), 0);
            await finalEpisode.close();
            evidence.finalEpisodeHasNoNextLink = true;
          }
        }
        if (surface === 'search') evidence.disclosures.search = await disclosure(page, '.collection-search-disclosure > summary');
        if (scenario.width === 1440 && ['devices', 'household'].includes(surface)) {
          const nav = page.getByRole('navigation', {name: 'Settings navigation'});
          const before = await nav.evaluate(element => ({windowY: scrollY, client: element.clientHeight, scroll: element.scrollHeight, overflow: getComputedStyle(element).overflowY, scrollbar: getComputedStyle(element).scrollbarWidth}));
          assert.equal(before.overflow, 'auto');
          assert(before.scroll > before.client);
          assert.notEqual(before.scrollbar, 'none');
          const advanced = nav.locator('section').last().locator('a');
          assert.equal(await advanced.count(), 7);
          for (const link of await advanced.all()) await link.focus();
          const bottom = await nav.evaluate(element => {element.scrollTop = element.scrollHeight; const last = element.querySelector('section:last-child a:last-child'); return {clearance: element.getBoundingClientRect().bottom - last.getBoundingClientRect().bottom, windowY: scrollY};});
          assert(bottom.clearance >= 16);
          if (!scenario.textScale) await nav.screenshot({path: path.join(output, `${surface}-advanced-bottom.png`)});
          await nav.evaluate(element => {element.scrollTop = 0;});
          const box = await nav.boundingBox();
          await page.mouse.move(box.x + box.width / 2, box.y + 30);
          const parentY = await page.evaluate(() => scrollY);
          await page.mouse.wheel(0, 250);
          await page.waitForTimeout(120);
          assert((await nav.evaluate(element => element.scrollTop)) > 0);
          assert.equal(await page.evaluate(() => scrollY), parentY);
          evidence.rails[surface] = {before, bottom, independentWheel: true, advancedLinks: 7};
        }
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), surface + ' page overflow');
        if (['home', 'devices', 'household'].includes(surface)) {
          await page.evaluate(() => {document.activeElement?.blur(); scrollTo(0, 0);});
          await page.screenshot({path: path.join(output, `${surface}-${scenario.width}${scenario.textScale ? '-text200' : ''}.png`)});
        }
      }
      receipt.scenarios.push(evidence);
      await context.close();
    }
    for (const evidence of receipt.scenarios) {
      // The Home banner deliberately uses a transparent border on its pill
      // buttons; elsewhere the common iris border is part of the base style.
      const sharedColors = ({normal, hover}) => ({
        normal: {background: normal.background, foreground: normal.foreground},
        hover: {background: hover.background, foreground: hover.foreground},
      });
      for (const [name, state] of Object.entries(evidence.ctas)) {
        assert.deepEqual(sharedColors(state), sharedColors(evidence.ctas.home), `${name} primary colors match Home`);
        // Player controls deliberately use a lighter, wider-offset outline
        // against the separate playback surface.
        const focus = name === 'player'
          ? {color: 'rgb(210, 194, 255)', width: '2px', offset: '4px', visible: true}
          : evidence.ctas.home.focus;
        assert.deepEqual(state.focus, focus, `${name} primary keyboard focus`);
        const border = name === 'home' ? 'rgba(0, 0, 0, 0)' : evidence.ctas.devices.normal.border;
        assert.equal(state.normal.border, border, `${name} primary border`);
        assert.equal(state.hover.border, border, `${name} primary hover border`);
      }
      assert.deepEqual(evidence.ctas.home.focus, {color: 'rgb(195, 177, 255)', width: '2px', offset: '2px', visible: true}, 'Shared keyboard focus stays visible');
      assert.notEqual(evidence.ctas.devices.normal.border, 'rgba(0, 0, 0, 0)', 'Regular primary buttons retain their border');
    }
    for (const file of files) assert.equal(sourceHash(file), receipt.sourceSha256[file], `Source changed during browser verification: ${file}`);
    fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify({...receipt, status: 'passed'}, null, 2));
    console.log(JSON.stringify({passed: receipt.scenarios.length, output}));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
