import test from 'node:test';
import assert from 'node:assert/strict';
import {nextPlanWindow,createDraft,isDirty} from '../../AL-FrontEnd/services/drafts.mjs';
import * as exceptions from '../../AL-FrontEnd/services/date_exceptions.mjs';

const policy={open:'07:30',close:'22:00',friday_close:'20:00',min_minutes:120,max_minutes:900,day_offset:1,booking_open:'07:00:00'};
const plan={seat_ids:['1'],day_offset:1,mode:'day',time_mode:'custom',begin:'09:00',end:'12:00',week_times:{},trigger:'07:00:00',enabled:true};
const now=new Date('2026-10-24T06:00:00+08:00');
test('preview skips use dates and reaches the first date after a long leave',()=>{
  const exceptions={};for(let day=25;day<=31;day++)exceptions[`2026-10-${day}`]={action:'skip'};
  for(let day=1;day<=13;day++)exceptions[`2026-11-${String(day).padStart(2,'0')}`]={action:'skip'};
  const next=nextPlanWindow(plan,policy,now,exceptions);
  assert.equal(next.date,'2026-11-14');assert.equal(next.release_date,'2026-11-13');
  assert.equal(next.begin,'09:00');assert.equal(next.end,'12:00');
});
test('preview adds an inactive weekday with its own interval and ordered seats',()=>{
  const weekly={...plan,mode:'week',week_times:{1:['09:00','12:00']}};
  const next=nextPlanWindow(weekly,policy,now,{'2026-10-25':{action:'override',begin:'14:00',end:'18:00',seat_ids:['2','1']}});
  assert.equal(next.date,'2026-10-25');assert.equal(next.begin,'14:00');assert.equal(next.end,'18:00');
  assert.deepEqual(next.seat_ids,['2','1']);assert.equal(next.source,'exception');
  assert.equal(nextPlanWindow(weekly,policy,now,{}).date,'2026-10-26');
});
test('preview handles Friday clipping and distinguishes an invalid short override',()=>{
  const exceptions={'2026-10-30':{action:'override',begin:'18:00',end:'22:00',seat_ids:['2']}};
  let next=nextPlanWindow(plan,policy,new Date('2026-10-29T06:00:00+08:00'),exceptions);
  assert.equal(next.end,'20:00');assert.equal(next.adjusted,true);
  exceptions['2026-10-30'].begin='19:00';
  next=nextPlanWindow(plan,policy,new Date('2026-10-29T06:00:00+08:00'),exceptions);
  assert.equal(next.available,false);
});
test('preview uses the use date across New Year and never points to a released date',()=>{
  assert.equal(nextPlanWindow(plan,policy,new Date('2026-12-31T06:00:00+08:00'),{'2027-01-01':{action:'skip'}}).date,'2027-01-02');
  assert.equal(nextPlanWindow(plan,policy,new Date('2026-10-24T07:01:00+08:00'),{}).date,'2026-10-26');
});
test('an unsaved date editor participates in the existing leave protection',()=>{
  const draft=createDraft({student_id:'99000001',revision:1,plan},policy);
  draft.dateEditor={action:'override',dirty:true};assert.equal(isDirty(draft),true);
  draft.dateEditor.dirty=false;assert.equal(isDirty(draft),false);
});
test('calendar one-day draft uses the selected weekday and saved override without changing the account',()=>{
  assert.equal(typeof exceptions.calendarDateEditor,'function');
  const account={plan:{...plan,mode:'week',week_times:{5:['18:00','22:00']}},date_exceptions:{'2026-11-09':{action:'override',begin:'14:00',end:'18:00',seat_ids:['2','1']}}};
  const saved=JSON.stringify(account),clock=new Date('2026-11-06T06:00:00+08:00');
  const friday=exceptions.calendarDateEditor('2026-11-13',account,policy,clock);
  assert.equal(friday.date,'2026-11-13');assert.equal(friday.begin,'18:00');assert.equal(friday.end,'20:00');
  const override=exceptions.calendarDateEditor('2026-11-09',account,policy,clock);
  assert.equal(override.begin,'14:00');assert.deepEqual(override.seat_ids,['2','1']);
  override.seat_ids.reverse();assert.equal(JSON.stringify(account),saved);
});
test('calendar draft rejects malformed and already released use dates',()=>{
  assert.equal(typeof exceptions.calendarDateEditor,'function');
  for(const date of ['2026-02-30','2026-2-03','not-date','2026-11-06','2026-11-07']){
    assert.throws(()=>exceptions.calendarDateEditor(date,{plan},policy,new Date('2026-11-06T07:01:00+08:00')));
  }
  assert.equal(exceptions.calendarDateEditor('2026-11-07',{plan},policy,new Date('2026-11-06T06:00:00+08:00')).date,'2026-11-07');
});
test('calendar draft reads selected segmented weekday instead of its legacy shadow interval',()=>{
  assert.equal(typeof exceptions.calendarDateEditor,'function');
  const segmented={...plan,mode:'week',week_segments:{1:[{id:'afternoon',begin:'14:00',end:'18:00',seat_ids:['2']},{id:'morning',begin:'09:00',end:'12:00',seat_ids:['3']}]}};
  const editor=exceptions.calendarDateEditor('2026-11-09',{plan:segmented},policy,new Date('2026-11-06T06:00:00+08:00'));
  assert.equal(editor.begin,'09:00');assert.equal(editor.end,'12:00');assert.deepEqual(editor.seat_ids,['3']);
});

test('ordinary and segmented base plans preview all segments of a date override',()=>{
  const segments=[{id:'am',begin:'09:00',end:'12:00',seat_ids:['1']},{id:'pm',begin:'14:00',end:'18:00',seat_ids:['2','1']}];
  const rule={'2026-11-09':{action:'override',segments}};
  for(const p of [plan,{...plan,segments:[{id:'base',begin:'08:00',end:'12:00',seat_ids:['1']}]}]){
    const next=nextPlanWindow(p,policy,new Date('2026-11-08T06:00:00+08:00'),rule);
    assert.equal(next.available,true);assert.deepEqual(next.segments,segments);
    const model=exceptions.calendarDateEditor('2026-11-09',{id:'a',revision:7,plan:p,date_exceptions:rule},policy,new Date('2026-11-06T06:00:00+08:00'));
    assert.deepEqual(model.segments,segments);assert.equal(model.base_revision,7);assert.equal(model.account_id,'a');
  }
});
test('non-Friday single-date late segments are valid and Friday truncation is checked',()=>{
  const segments=[{id:'late',begin:'19:00',end:'22:00',seat_ids:['1']}];
  assert.equal(exceptions.dateEditorError({date:'2026-11-08',segments},policy),'');
  assert.match(exceptions.dateEditorError({date:'2026-11-13',segments},policy),/周五/);
});
