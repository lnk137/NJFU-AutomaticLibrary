import test from 'node:test';
import assert from 'node:assert/strict';
import {beginRequest,canApplyResponse,chooseAccount,canApplySnapshot,canApplyRunPage} from '../../AL-FrontEnd/services/workspace.mjs';

test('late account response cannot replace the selected account',()=>{
  const request=beginRequest('alice',1);
  const next=chooseAccount({activeAccountId:'alice',requestGeneration:1,accounts:['alice','bob'],drafts:{alice:{begin:'09:00'}},runs:[1]},'bob');
  assert.equal(canApplyResponse(request,next.activeAccountId,next.requestGeneration),false);
  assert.deepEqual(next.runs,[1]);assert.deepEqual(next.drafts.alice,{begin:'09:00'});
});
test('new page generation rejects an old page response',()=>{
  assert.equal(canApplyResponse(beginRequest('alice',2),'alice',3),false);
  assert.equal(canApplyResponse(beginRequest('alice',3),'alice',3),true);
});
test('older snapshot cannot overwrite a later query or a changed plan',()=>{
  const request=beginRequest('alice',1,2);
  assert.equal(canApplySnapshot(request,3,4,4),false);
  assert.equal(canApplySnapshot(request,2,5,4),false);
  assert.equal(canApplySnapshot(request,2,4,4),true);
});
test('old overview filter and old pagination cannot affect a new filter',()=>{
  assert.equal(canApplyRunPage({key:'alice',sequence:1},'bob',2),false);
  assert.equal(canApplyRunPage({key:'bob',sequence:1},'bob',2),false);
  assert.equal(canApplyRunPage({key:'bob',sequence:2},'bob',2),true);
});
