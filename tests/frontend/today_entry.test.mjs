import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';

const frontend=new URL('../../AL-FrontEnd/',import.meta.url),require=createRequire(new URL('package.json',frontend));
const Vue=require('vue'),{renderToString}=require('@vue/server-renderer');
const {parse:parseSFC}=require('@vue/compiler-sfc'),{parse,compile}=require('@vue/compiler-dom');
const {descriptor}=parseSFC(await readFile(new URL('pages/account_info/account_info.vue',frontend),'utf8'));
function findEntry(node){
  if(node.type===1&&node.tag==='button'&&node.props.some(prop=>prop.type===6&&prop.name==='class'&&prop.value?.content.split(/\s+/).includes('today-entry')))return node;
  for(const child of node.children||[]){const entry=findEntry(child);if(entry)return entry;}
}
const entry=findEntry(parse(descriptor.template.content));
assert.ok(entry,'the plan page must provide a same-day entry button');
const {code}=compile(entry.loc.source,{mode:'function',prefixIdentifiers:true,isCustomElement:tag=>tag==='AppIcon'});
const renderTemplate=new Function('Vue',code)(Vue);
async function render(role,{busy=false,activeAccount={id:'fixture-account'}}={}){
  return renderToString(Vue.createSSRApp({
    setup:()=>({store:{user:{role},activeAccount},busy,setBookingMode:()=>{}}),
    render:renderTemplate
  }));
}
function button(html){
  const match=html.match(/<button\b([^>]*)>([\s\S]*?)<\/button>/);
  assert.ok(match,'the signed-in role must see the same-day entry');
  return {attributes:match[1],label:match[2].replace(/<[^>]*>/g,'').trim()};
}

test('students see the same-day segmented booking entry with an active account',async()=>{
  const entry=button(await render('student'));
  assert.equal(entry.label,'当天分段预约');
  assert.doesNotMatch(entry.attributes,/\bdisabled\b/);
});
test('administrators retain the manual preflight entry label with an active account',async()=>{
  const entry=button(await render('root'));
  assert.equal(entry.label,'当天预约 · 手动预检');
  assert.doesNotMatch(entry.attributes,/\bdisabled\b/);
});
for(const role of ['student','root']){
  test(role+' cannot open same-day booking while the page is busy',async()=>{
    assert.match(button(await render(role,{busy:true})).attributes,/\bdisabled\b/);
  });
  test(role+' cannot open same-day booking without an active account',async()=>{
    assert.match(button(await render(role,{activeAccount:null})).attributes,/\bdisabled\b/);
  });
}
