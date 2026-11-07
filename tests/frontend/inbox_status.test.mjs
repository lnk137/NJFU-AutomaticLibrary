import test from 'node:test';import assert from 'node:assert/strict';
import {receiptState,inboxTime} from '../../AL-FrontEnd/services/inbox_status.mjs';
test('receipt and failure never claim an effective plan',()=>{
  for(const state of ['pending','leased','failed','unknown','applied']){
    assert.equal(receiptState({state}).effective,false);
  }
  assert.equal(receiptState({state:'applied',applied_revision:4}).effective,true);
  assert.match(receiptState({state:'pending'}).label,/尚未生效/);
});
test('missing sync evidence remains unknown and times use Beijing',()=>{
  assert.equal(inboxTime(null),'尚无记录');
  assert.match(inboxTime(Date.parse('2026-10-25T06:55:00+08:00')/1000),/06:55:00/);
});
test('an empty account snapshot is not a pending submission',()=>{
  for(const snapshot of [null,{student_id:'fixture',version:0,state:null},{student_id:'fixture',applied_version:0}]){
    assert.equal(receiptState(snapshot).kind,'none');
  }
  for(const snapshot of [{state:null,version:2},{state:'unknown',version:0},{state:'applied',applied_revision:0}]){
    assert.equal(receiptState(snapshot).effective,false);
    assert.equal(receiptState(snapshot).kind,'pending');
  }
});
