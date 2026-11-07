import test from 'node:test';
import assert from 'node:assert/strict';
const helpers=await import('../../AL-FrontEnd/services/navigation.mjs').catch(e=>{
  if(e.code!=='ERR_MODULE_NOT_FOUND')throw e;
  return {navigationItems:()=>[],navigationRoute:()=>null,activeNavigation:()=>'',schoolRecordLabel:()=>''};
});
test('authenticated student navigation includes account profile and never a root destination',()=>{
  const items=helpers.navigationItems('student');
  assert.deepEqual(items.map(i=>i.id),['overview','plan','profile']);
  assert.equal(helpers.navigationRoute('student','profile'),'/pages/profile/profile');
  assert.equal(helpers.navigationRoute('student','admin'),null);
  assert.equal(helpers.navigationRoute(null,'overview'),null);
});
test('root navigation keeps account inspection within management context',()=>{
  assert.deepEqual(helpers.navigationItems('root').map(i=>i.id),['dashboard','admin','profile']);
  assert.equal(helpers.navigationRoute('root','admin'),'/pages/admin/admin?tab=users');
  assert.equal(helpers.activeNavigation('root','plan'),'admin');
  assert.equal(helpers.activeNavigation('student','records'),'overview');
});
test('only verified numeric school status1027 can be labeled confirmed',()=>{
  assert.equal(helpers.schoolRecordLabel(1027),'学校已确认');
  for(const value of ['1027',1,0,null,undefined])assert.notEqual(helpers.schoolRecordLabel(value),'学校已确认');
});
