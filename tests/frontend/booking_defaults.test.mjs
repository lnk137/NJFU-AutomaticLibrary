import test from 'node:test';
import assert from 'node:assert/strict';
import * as drafts from '../../AL-FrontEnd/services/drafts.mjs';
const policy={booking_open:'07:00:00',day_offset:1,open:'07:30',close:'22:00',friday_close:'20:00',min_minutes:120};
test('weekly preset fills all seven days and Friday closing without changing the input',()=>{
  const original={seat_ids:['1'],mode:'day',enabled:true,trigger:'08:28:00'};
  const weekly=drafts.withWeeklyDefaults(original,policy);
  assert.equal(weekly.mode,'week');assert.equal(weekly.trigger,'07:00:00');
  assert.deepEqual(weekly.week_times['5'],['07:30','20:00']);
  assert.deepEqual(weekly.week_times['6'],['07:30','22:00']);
  assert.equal(Object.keys(weekly.week_times).length,7);
  assert.deepEqual(weekly.seat_ids,['1']);assert.equal(weekly.enabled,true);
  assert.equal(original.trigger,'08:28:00');
});
test('today default uses Beijing date and the next half hour on Friday',()=>{
  const today=drafts.todayDefaults(policy,new Date('2026-10-22T23:41:00Z'));
  assert.equal(today.date,'2026-10-23');assert.equal(today.begin,'08:00');assert.equal(today.end,'20:00');
  assert.equal(today.available,true);
});
test('today default starts at opening before the library opens',()=>{
  const today=drafts.todayDefaults(policy,new Date('2026-10-20T21:10:00Z'));
  assert.equal(today.date,'2026-10-21');assert.equal(today.begin,'07:30');assert.equal(today.end,'22:00');
});
test('less than two hours before Friday closing is marked unavailable',()=>{
  assert.equal(drafts.todayDefaults(policy,new Date('2026-10-23T10:01:00Z')).available,false);
});

test('time presets keep seats, trigger and enabled state while custom remains editable',()=>{
  const original={seat_ids:['1'],mode:'day',time_mode:'full_day',begin:'07:30',end:'22:00',trigger:'08:28:00',enabled:true};
  const morning=drafts.withTimePreset(original,policy,'morning');
  assert.deepEqual([morning.begin,morning.end,morning.time_mode],['07:30','12:00','custom']);
  assert.equal(morning.trigger,'08:28:00');assert.deepEqual(morning.seat_ids,['1']);assert.equal(morning.enabled,true);
  assert.equal(original.time_mode,'full_day');
  const custom=drafts.withTimePreset({...morning,begin:'09:00'},policy,'custom');
  assert.equal(custom.begin,'09:00');
});

test('next window preview uses Beijing release date and caps Friday in custom mode',()=>{
  const plan={mode:'day',time_mode:'custom',begin:'07:30',end:'22:00',trigger:'07:00:00',day_offset:1};
  const preview=drafts.nextPlanWindow(plan,policy,new Date('2026-10-21T12:00:00Z'));
  assert.equal(preview.release_date,'2026-10-22');assert.equal(preview.date,'2026-10-23');
  assert.equal(preview.end,'20:00');assert.equal(preview.adjusted,true);assert.equal(preview.available,true);
  assert.equal(plan.end,'22:00');
});

test('preview preserves earlier ends and reports invalid short Friday intervals',()=>{
  const plan={mode:'day',time_mode:'custom',begin:'09:00',end:'18:00',trigger:'07:00:00',day_offset:1};
  const now=new Date('2026-10-21T12:00:00Z');
  assert.equal(drafts.nextPlanWindow(plan,policy,now).end,'18:00');
  assert.equal(drafts.nextPlanWindow({...plan,begin:'19:00',end:'22:00'},policy,now).available,false);
});

test('weekly preview skips empty days and shows the effective Friday window',()=>{
  const plan={mode:'week',time_mode:'custom',begin:'09:00',end:'12:00',trigger:'07:00:00',day_offset:1,week_times:{'5':['10:00','22:00']}};
  const preview=drafts.nextPlanWindow(plan,policy,new Date('2026-10-19T12:00:00Z'));
  assert.equal(preview.date,'2026-10-23');assert.equal(preview.release_date,'2026-10-22');
  assert.equal(preview.begin,'10:00');assert.equal(preview.end,'20:00');
});
