import test from 'node:test';import assert from 'node:assert/strict';
import * as drafts from '../../AL-FrontEnd/services/drafts.mjs';
const validate=drafts.planWindowError||(()=>''),policy={open:'07:30',close:'22:00',friday_close:'20:00',min_minutes:120,max_minutes:900};
test('custom daily editor rejects a Friday cap shorter than two hours before saving',()=>{
  assert.match(validate({mode:'day',time_mode:'custom',begin:'19:00',end:'22:00'},policy),/周五.*2/);
  assert.equal(validate({mode:'day',time_mode:'custom',begin:'18:00',end:'22:00'},policy),'');
});
test('weekly editor validates each selected use day without rejecting skipped days',()=>{
  assert.match(validate({mode:'week',week_times:{5:['19:00','22:00']}},policy),/周五/);
  assert.equal(validate({mode:'week',week_times:{4:['19:00','22:00'],5:['','']}},policy),'');
  assert.match(validate({mode:'week',week_times:{}},policy),/至少/);
  assert.match(validate({mode:'week',week_times:{2:['09:00','']}},policy),/完整/);
});
test('time editor preserves earlier endings and rejects malformed or outside hours',()=>{
  assert.equal(validate({mode:'day',begin:'09:00',end:'12:00'},policy),'');
  assert.match(validate({mode:'day',begin:'garbage',end:'22:00'},policy),/HH:MM/);
  assert.match(validate({mode:'day',begin:'06:00',end:'12:00'},policy),/开放/);
});
test('malformed stored weekly intervals produce a validation message rather than crash',()=>{
  for(const value of [42,{},'09:00',null])assert.match(validate({mode:'week',week_times:{2:value}},policy),/完整|至少/);
});
