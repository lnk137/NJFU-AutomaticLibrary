import test from 'node:test';
import assert from 'node:assert/strict';
import {parseSeatTags,librarySnapshot,groupSeatSnapshot,favoriteEntries,mergeLibraryAccount} from '../../AL-FrontEnd/services/seat_preferences.mjs';

const seats=[{id:'1',name:'A001',location:'2F'},{id:'2',name:'A002',location:'3F'}];
test('custom tags trim, deduplicate and accept Chinese separators without adding presets',()=>{
  assert.deepEqual(parseSeatTags(' 自定义，安静,自定义\n 早上 '),['自定义','安静','早上']);
  assert.deepEqual(parseSeatTags(''),[]);
  assert.throws(()=>parseSeatTags('1,2,3,4,5,6'));assert.throws(()=>parseSeatTags('长'.repeat(21)));
});
test('group application makes a frozen ordered draft and refuses unavailable seats',()=>{
  const group={id:'g',name:'自己命名',seat_ids:['2','1']},selected=groupSeatSnapshot(group,seats);
  group.seat_ids.reverse();assert.deepEqual(selected,['2','1']);
  for(const ids of [[],['1','1'],['missing']])assert.throws(()=>groupSeatSnapshot({...group,seat_ids:ids},seats));
});
test('favorites can be found by custom tag and unavailable saved entries remain visible',()=>{
  const data={favorites:[{seat_id:'1',tags:['我的标签']},{seat_id:'retired',tags:['旧收藏']}],groups:[]};
  assert.deepEqual(favoriteEntries(data,seats,'我的标签').map(s=>s.id),['1']);
  const retired=favoriteEntries(data,seats,'旧收藏')[0];assert.equal(retired.available,false);assert.equal(retired.id,'retired');
});
test('library snapshots do not share arrays with saved account metadata',()=>{
  const account={id:'a',seat_preferences_revision:3,seat_preferences:{favorites:[{seat_id:'1',tags:['原标签']}],groups:[]}};
  const data=librarySnapshot(account);data.favorites[0].tags.push('新标签');
  assert.deepEqual(account.seat_preferences.favorites[0].tags,['原标签']);assert.equal(data.revision,3);
});
test('merging library metadata cannot change schedule revision or replace a newer snapshot',()=>{
  const plan={seat_ids:['1']},account={id:'a',revision:7,plan,date_exceptions:{'2099-01-01':{action:'skip'}},seat_preferences_revision:1};
  const saved={account_id:'a',revision:2,favorites:[],groups:[{id:'g',name:'我的组',seat_ids:['2','1']}]};
  const merged=mergeLibraryAccount(account,saved);
  assert.equal(merged.revision,7);assert.equal(merged.plan,plan);assert.equal(merged.date_exceptions,account.date_exceptions);
  saved.groups[0].seat_ids.reverse();assert.deepEqual(merged.seat_preferences.groups[0].seat_ids,['2','1']);
  assert.equal(mergeLibraryAccount(merged,{...saved,revision:1}),merged);
  assert.equal(mergeLibraryAccount(account,{...saved,account_id:'b'}),account);
});
