const test = require('node:test');
const assert = require('node:assert/strict');
const {settingsMenuTypes} = require('../../src/omakase/plus/static/web-player.js');

class Node {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.attrs={};this.listeners=new Map();this.dataset={};this.classList={values:new Set(),add(...v){v.forEach(x=>this.values.add(x));},remove(v){this.values.delete(v);},contains(v){return this.values.has(v);},toggle(v,on){on?this.values.add(v):this.values.delete(v);}};}
  appendChild(n){this.children.push(n);n.parentNode=this;return n;} prepend(n){this.children.unshift(n);n.parentNode=this;}
  setAttribute(n,v){this.attrs[n]=v;}getAttribute(n){return this.attrs[n];}
  addEventListener(n,f){this.listeners.set(n,f);}removeEventListener(n){this.listeners.delete(n);}
  querySelector(){return null;}querySelectorAll(selector){return this.children.filter(x=>selector==='[data-quality-choice]'?x.dataset.qualityChoice!==undefined:x.dataset.subtitleChoice!==undefined);}
  remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(n=>n!==this);}
  click(){this.listeners.get('click')?.({stopPropagation(){}});}focus(){document.activeElement=this;}
}
const document={createElement:t=>new Node(t),activeElement:null};
class Settings {
  constructor(parent,controls){this.parent=parent;this.player=controls.player;this.isSubMenuOpened=false;this.button=new Node('button');this.menu=new Node();this.nameSpan=new Node();this.currentSelection=new Node();this.backSpan=new Node();this.backButton=new Node('button');this.menu.appendChild(this.backButton);this.backButton.addEventListener('click',()=>{controls.close();this.button.focus();});controls.menus.push(this);}
  checkAvailability(){this.button.classList.toggle('shaka-hidden',this.isSubMenuOpened);}
  release(){}
}
class Audio extends Settings {
  updateLocalizedStrings(){}
  checkAvailability(){super.checkAvailability();if(!this.player.getAudioTracks().length)this.button.classList.add('shaka-hidden');}
}
function fixture(onSelect=null){
  const state={ready:true,failed:false,captionLoading:false};const selected=[];
  const selector={value:'off',options:[{value:'off',textContent:'Off'},{value:'native:0',textContent:'English'},{value:'ass:71',textContent:'Japanese · ASS'}],get selectedOptions(){return this.options.filter(x=>x.value===this.value);}};
  const controls={menus:[],player:{addEventListener(){},removeEventListener(){},getVariantTracks:()=>[{id:1,videoId:1,width:1920,height:1080,active:true}],getAudioTracks:()=>[{id:2},{id:3}]},open(){this.menus.forEach(m=>{m.isSubMenuOpened=true;m.checkAvailability();});},close(){this.menus.forEach(m=>{m.isSubMenuOpened=false;m.checkAvailability();});}};
  const types=settingsMenuTypes({ui:{SettingsMenu:Settings,AudioLanguageSelection:Audio}},{document,find:n=>n==='ass-select'?selector:null,state:()=>state,register(){},unregister(){},selectSubtitle:onSelect || (async value=>{selected.push(value);selector.value=value;controls.menus.forEach(m=>m.checkAvailability());})});
  const menus=[new types.QualityMenu(new Node(),controls),new types.AudioMenu(new Node(),controls),new types.SubtitlesMenu(new Node(),controls)];
  return {controls,menus,selector,selected,state};
}

test('supplied submenu lifecycle hides every root sibling through selection and restores Back/Escape focus',async()=>{
  global.document=document;
  const f=fixture();f.controls.open();
  assert.ok(f.menus.every(m=>m.button.classList.contains('shaka-hidden')));
  const subtitles=f.menus[2];const rows=subtitles.menu.querySelectorAll('[data-subtitle-choice]');
  assert.deepEqual(rows.map(r=>r.dataset.subtitleChoice),['off','native:0','ass:71']);
  assert.equal(subtitles.menu.children.some(n=>n.tagName==='SELECT'),false);
  rows[1].click();await Promise.resolve();assert.deepEqual(f.selected,['native:0']);
  assert.ok(f.menus.every(m=>m.button.classList.contains('shaka-hidden')));
  assert.equal(rows[1].getAttribute('aria-checked'),'true');
  subtitles.backButton.click();assert.ok(f.menus.every(m=>!m.button.classList.contains('shaka-hidden')));assert.equal(document.activeElement,subtitles.button);
  f.controls.open();let prevented=false;
  subtitles.menu.listeners.get('keydown')({key:'Escape',preventDefault(){prevented=true;},stopPropagation(){}});
  assert.equal(prevented,true);assert.equal(document.activeElement,subtitles.button);assert.ok(!subtitles.button.classList.contains('shaka-hidden'));
});

test('direct subtitle rows preserve focus/identity while state changes; no tracks is Off only',()=>{
  global.document=document;const f=fixture(),menu=f.menus[2];
  const rows=menu.menu.querySelectorAll('[data-subtitle-choice]');rows[2].focus();f.selector.value='ass:71';menu.checkAvailability();assert.equal(document.activeElement,rows[2]);assert.equal(rows[2].getAttribute('aria-checked'),'true');
  f.selector.options=f.selector.options.slice(0,1);f.selector.value='off';menu.checkAvailability();assert.deepEqual(menu.menu.querySelectorAll('[data-subtitle-choice]').map(r=>r.dataset.subtitleChoice),['off']);
  f.state.captionLoading=true;menu.checkAvailability();assert.ok(menu.menu.querySelectorAll('[data-subtitle-choice]').every(r=>r.disabled));
});

test('empty audio keeps its direct trigger disabled without offering invented tracks',()=>{
  global.document=document;const f=fixture();f.controls.player.getAudioTracks=()=>[];
  f.menus[1].checkAvailability();assert.equal(f.menus[1].button.disabled,true);assert.equal(f.menus[1].button.classList.contains('shaka-hidden'),false);
  f.controls.open();f.controls.close();assert.equal(f.menus[1].button.disabled,true);
});

test('a sole real audio track keeps direct Audio access while absent audio stays unavailable',()=>{
  global.document=document;const f=fixture();f.controls.player.getAudioTracks=()=>[{id:2}];
  f.menus[1].checkAvailability();assert.equal(f.menus[1].button.disabled,false);assert.equal(f.menus[1].button.classList.contains('shaka-hidden'),false);
});

test('async direct subtitle handoff restores its visible row after body fallback but never steals Back focus',async()=>{
  for(const back of [false,true]){
    let release;const wait=new Promise(resolve=>{release=resolve;});
    const f=fixture(async value=>{await wait;f.selector.value=value;f.controls.menus.forEach(m=>m.checkAvailability());});
    f.controls.open();const menu=f.menus[2],row=menu.menu.querySelectorAll('[data-subtitle-choice]')[2];row.focus();row.click();
    if(back)menu.backButton.click();else{document.body=new Node('body');document.activeElement=document.body;}
    release();await new Promise(resolve=>setImmediate(resolve));
    assert.equal(document.activeElement,back?menu.button:row);
    assert.equal(menu.button.classList.contains('shaka-hidden'),!back);
  }
});
