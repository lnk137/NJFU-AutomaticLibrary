import test from 'node:test';
import assert from 'node:assert/strict';
import {createDraft,isDirty,applySavedAccount,applySavedPlan,persistableDraft,withSchoolDefaults} from '../../AL-FrontEnd/services/drafts.mjs';

const account={id:'alice',student_id:'fixture01',alias:'Alice',revision:1,plan:{seat_ids:['1'],day_offset:1,mode:'day',begin:'09:00',end:'12:00',week_times:{},trigger:'20:00:00',enabled:false}};
test('segmented same-day drafts protect unsaved changes without changing the saved plan',()=>{
  const draft=createDraft(account),original=JSON.stringify(draft.plan);
  draft.todayEditor={account_id:'alice',date:'2026-11-03',segments:[{begin:'14:00',end:'18:00',seat_ids:['2']}],dirty:true};
  assert.equal(isDirty(draft),true);assert.equal(JSON.stringify(draft.plan),original);
  const restored=persistableDraft(draft);assert.equal(restored.todayEditor.segments[0].seat_ids[0],'2');
  draft.todayEditor.dirty=false;draft.todayEditor.pending={batch_id:'one',expires_at:'2026-11-03T08:02:00+08:00'};
  assert.equal(isDirty(draft),false);assert.equal(persistableDraft(draft).todayEditor.pending.batch_id,'one');
});
test('saving credentials preserves unsaved plan and edits made during request',()=>{
  const draft=createDraft(account);draft.plan.begin='10:00';draft.password='first-password';
  const sent=structuredClone(draft);draft.alias='edited during request';draft.password='new input';
  const next=applySavedAccount(draft,{...account,revision:2},sent);
  assert.equal(next.plan.begin,'10:00');assert.equal(next.alias,'edited during request');assert.equal(next.password,'new input');assert.equal(next.revision,2);assert.equal(isDirty(next),true);
});
test('saving plan preserves account draft and newly edited plan fields',()=>{
  const draft=createDraft(account);draft.alias='Bob';draft.password='unsaved-password';draft.plan.begin='10:00';
  const sent=structuredClone(draft);draft.plan.end='14:00';
  const next=applySavedPlan(draft,{...account,revision:2,plan:{...account.plan,begin:'10:00'}},sent);
  assert.equal(next.alias,'Bob');assert.equal(next.password,'unsaved-password');assert.equal(next.plan.end,'14:00');assert.equal(isDirty(next),true);
});
test('saved part becomes clean and passwords never enter cached drafts',()=>{
  const d=createDraft(account);d.password='temporary';
  const saved=applySavedAccount(d,{...account,revision:2},structuredClone(d));
  assert.equal(isDirty(saved),false);assert.equal(saved.password,'');
  assert.equal(persistableDraft({...saved,password:'hidden'}).password,'');
});

const policy={booking_open:'07:00:00',day_offset:1,open:'07:30',close:'22:00',friday_close:'20:00'};
test('official defaults fill only a new plan and keep a saved custom plan intact',()=>{
  const fresh=createDraft(null,policy);
  assert.equal(fresh.plan.trigger,'07:00:00');assert.equal(fresh.plan.time_mode,'full_day');
  assert.equal(fresh.plan.day_offset,1);assert.equal(fresh.plan.begin,'07:30');
  assert.deepEqual(createDraft(account,policy).plan,account.plan);
});
test('explicit school preset keeps seat choices without mutating the existing plan',()=>{
  const next=withSchoolDefaults(account.plan,policy);
  assert.deepEqual(next.seat_ids,['1']);assert.equal(next.trigger,'07:00:00');
  assert.equal(next.time_mode,'full_day');assert.equal(account.plan.trigger,'20:00:00');
});

test('same-day draft edits are tracked without altering the scheduled plan',()=>{
  const d=createDraft(account,policy);
  d.today={date:'2026-10-23',begin:'09:00',end:'20:00',seat_ids:['1']};
  d.todayBaseline=JSON.stringify(d.today);d.bookingScope='today';
  assert.equal(isDirty(d),false);
  d.today.seat_ids=['2'];
  assert.equal(isDirty(d),true);
  assert.deepEqual(d.plan,account.plan);
  const cached=persistableDraft({...d,password:'secret'});
  assert.equal(cached.password,'');assert.deepEqual(cached.today.seat_ids,['2']);
});
