// Real built H5 with an isolated, read-only fake API; never contacts production.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {chromium} from '/Users/a0000/.npm/_npx/e41f203b7505f1fb/node_modules/playwright-core/index.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const assets=path.join(root,'AL-FrontEnd/dist/build/h5');
const evidence=process.argv[2];
assert(evidence&&path.isAbsolute(evidence),'An absolute evidence directory is required');
await fs.mkdir(evidence,{recursive:true,mode:0o700});
const plan={seat_ids:['s1'],day_offset:1,mode:'day',time_mode:'full_day',begin:'07:30',end:'22:00',week_times:{},trigger:'07:00:00',enabled:true};
const accounts=[{id:'alice',student_id:'99000001',alias:'测试甲',revision:1,credential_revision:1,verified_at:'2026-10-01T00:00:00+08:00',plan,date_exceptions:{}},
  {id:'bob',student_id:'99000002',alias:'测试乙',revision:1,credential_revision:1,verified_at:'2026-10-01T00:00:00+08:00',plan,date_exceptions:{}}];
const state={role:'root',requests:[],unexpected:[],errors:[],failHistory:false,delayAlice:false,releaseAlice:null};
const policy={open:'07:30',close:'22:00',friday_close:'20:00',min_minutes:120,max_minutes:900,day_offset:1,booking_open:'07:00:00'};
const run=(id,account,date,status,message)=>({id,account_id:account,target_date:date,status,message,
  spec:{kind:'scheduled',begin:'07:30',end:'20:00',seat_ids:['s1'],due_at:1793228400},
  attempts:[{seat_id:'s1',timings:{request_started_at:1793228400.123,request_ms:220}}],
  result:{status,records:[],verified_at:status==='success'?'2026-10-29T07:00:01+08:00':null}});
const rows={alice:[run('a1','alice','2026-10-30','success','学校有效预约记录已核实'),
  run('a2','alice','2026-10-29','failed','学校拒绝预约'),run('a3','alice','2026-10-28','uncertain','等待学校核对'),
  run('a4','alice','2026-10-27','success','学校有效预约记录已核实')],
  bob:[run('b1','bob','2026-10-26','failed','测试乙预约失败')]};
const server=http.createServer(async(req,res)=>{
  try{
    const url=new URL(req.url,'http://127.0.0.1');const relative=url.pathname==='/'?'index.html':url.pathname.slice(1);
    const file=path.resolve(assets,relative);assert(file.startsWith(assets+path.sep));
    const body=await fs.readFile(file);const type={'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png','.woff2':'font/woff2'}[path.extname(file)]||'application/octet-stream';
    res.writeHead(200,{'Content-Type':type});res.end(body);
  }catch{res.writeHead(404);res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin=`http://127.0.0.1:${server.address().port}`;let browser,page;
try{
  browser=await chromium.launch({headless:true,executablePath:'/Users/a0000/Library/Caches/ms-playwright/chromium-1217/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'});
  const context=await browser.newContext({viewport:{width:1440,height:1000},serviceWorkers:'block'});page=await context.newPage();page.setDefaultTimeout(5000);
  page.on('pageerror',e=>state.errors.push(e.message));
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.origin!==origin){state.unexpected.push('external');return route.abort();}
    if(!url.pathname.startsWith('/api/local/'))return route.continue();
    state.requests.push({method:request.method(),path:url.pathname,query:url.search});
    if(request.method()!=='GET'){state.unexpected.push('mutation');return route.abort();}
    const user=state.role==='root'?{role:'root'}:{role:'student',student_id:'99000001',account_id:'alice'};
    let data;
    if(url.pathname==='/api/local/bootstrap')data={csrf:'fixture',user,admin_login:true,public_access:false};
    else if(url.pathname==='/api/local/auth/me')data={user};
    else if(url.pathname==='/api/local/accounts')data={accounts:state.role==='root'?accounts:[accounts[0]]};
    else if(url.pathname==='/api/local/status')data={allow_live:true,scheduler_running:true,clock_ok:true,ready_to_submit:true,capacity:24};
    else if(url.pathname==='/api/local/policy')data=policy;
    else if(url.pathname==='/api/local/seats')data={seats:[{id:'s1',name:'A001',location:'3F'}]};
    else if(url.pathname==='/api/local/admin/users')data={users:accounts.map(a=>({student_id:a.student_id,school_name:a.alias,note:'',enabled:true,revision:1,account:a}))};
    else if(url.pathname==='/api/local/admin/audit')data={items:[]};
    else if(url.pathname==='/api/local/admin/inbox')data={configured:false,accounts:[]};
    else if(url.pathname==='/api/local/runs'){
      const id=url.searchParams.get('account_id'),all=rows[id]||[];
      if(state.delayAlice&&id==='alice'){state.delayAlice=false;await new Promise(resolve=>state.releaseAlice=resolve);}
      if(state.failHistory)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'历史暂不可用'})});
      const second=url.searchParams.has('cursor'),items=id==='alice'?(second?all.slice(3):all.slice(0,3)):all;
      data={items,counts:{total:all.length,success:all.filter(x=>x.status==='success').length,failed:all.filter(x=>x.status==='failed').length,uncertain:all.filter(x=>x.status==='uncertain').length},next_cursor:id==='alice'&&!second?'page2':null};
    }else{state.unexpected.push(url.pathname);return route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({error:'fixture endpoint missing'})});}
    return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.goto(origin+'/#/pages/admin/admin?tab=users');
  await page.getByRole('heading',{name:'用户管理'}).waitFor({timeout:10000});
  await page.locator('.management-row').filter({hasText:'99000001'}).getByRole('button',{name:'计划与记录'}).click();
  await page.getByText('管理账号 · 测试甲',{exact:true}).waitFor({timeout:10000});
  const section=page.getByRole('region',{name:'预约历史'});
  assert(await section.count(),'管理员账号详情缺少预约历史区域');
  await section.getByText('2026-10-30').waitFor({timeout:5000});
  await page.getByRole('button',{name:'预约历史',exact:true}).click();
  await page.waitForFunction(()=>{const r=document.querySelector('[aria-label="预约历史"]').getBoundingClientRect();return r.top>=0&&r.top<innerHeight-80;});
  for(const text of ['已核实成功','失败','待确认','07:00:00.123'])assert(await section.getByText(text,{exact:true}).count(),'历史结果缺少 '+text);
  assert(state.requests.some(x=>x.path==='/api/local/runs'&&new URLSearchParams(x.query).get('account_id')==='alice'),'历史请求没有限定当前账号');
  await section.getByRole('button',{name:'加载更多'}).click();
  await section.getByText('2026-10-27').waitFor();
  for(const [name,width,height] of [['desktop',1440,1000],['ipad',834,1112],['phone',390,844]]){
    await page.setViewportSize({width,height});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'历史页面横向溢出 '+name);
    await page.screenshot({path:path.join(evidence,`history-${name}.png`),fullPage:true});
  }
  state.failHistory=true;await section.getByRole('button',{name:'刷新'}).click();
  await section.getByRole('alert').waitFor();assert(await section.getByText('显示上次结果',{exact:false}).count());
  assert(await section.getByText('2026-10-30').count(),'刷新失败时旧记录消失');
  state.failHistory=false;await section.getByRole('button',{name:'重试'}).click();
  await section.getByText('2026-10-30').waitFor();
  await page.locator('summary').filter({hasText:'切换账号'}).click();
  await page.getByRole('button',{name:/测试乙.*99000002/}).click();
  await section.getByText('2026-10-26').waitFor();assert.equal(await section.getByText('2026-10-30').count(),0);
  state.delayAlice=true;await page.getByRole('button',{name:/测试甲.*99000001/}).click();
  await page.waitForFunction(()=>document.querySelector('[aria-label="预约历史"]')?.textContent.includes('正在读取'));
  await page.getByRole('button',{name:/测试乙.*99000002/}).click();await section.getByText('2026-10-26').waitFor();
  assert(state.releaseAlice,'未观察到延迟的旧账号请求');state.releaseAlice();
  await page.waitForTimeout(150);assert.equal(await section.getByText('2026-10-30').count(),0,'旧账号响应泄漏到当前账号');
  state.role='student';await page.goto(origin+'/#/pages/account_info/account_info?account_id=alice');
  await page.getByRole('heading',{name:'预约计划'}).waitFor();
  assert.equal(await page.getByRole('region',{name:'预约历史'}).count(),0,'学生页面不应显示管理员历史区');
  assert.deepEqual(state.unexpected,[]);assert.deepEqual(state.errors,[]);
  console.log('PASS: root account history, paging, stale handling, account isolation and three device sizes');
}catch(error){
  if(page)await page.screenshot({path:path.join(evidence,'history-browser-failure.png'),fullPage:true});
  console.error(error.stack||String(error));process.exitCode=1;
}finally{
  if(browser)await browser.close();
  await new Promise(resolve=>server.close(resolve));
  await fs.writeFile(path.join(evidence,'browser-requests.json'),JSON.stringify(state,null,2)+'\n');
}
