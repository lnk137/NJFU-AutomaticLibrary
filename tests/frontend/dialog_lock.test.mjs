import test from 'node:test';
import assert from 'node:assert/strict';
import * as lock from '../../AL-FrontEnd/services/dialog_lock.mjs';
const acquire=lock.acquireScrollLock||(()=>()=>{});
test('overlapping dialogs keep scroll locked until the final owner releases',()=>{
  const body={style:{overflow:''}};
  const first=acquire(body),second=acquire(body);
  assert.equal(body.style.overflow,'hidden');
  first();assert.equal(body.style.overflow,'hidden');
  second();assert.equal(body.style.overflow,'');
});
test('closing out of order and repeated cleanup preserve the original overflow',()=>{
  const body={style:{overflow:'auto'}};
  const first=acquire(body),second=acquire(body);
  second();second();assert.equal(body.style.overflow,'hidden');
  first();first();assert.equal(body.style.overflow,'auto');
});
