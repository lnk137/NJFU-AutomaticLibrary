// Existing project browser engine only, isolated loopback fixture, no downloads.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {chromium} from '/Users/a0000/.npm/_npx/e41f203b7505f1fb/node_modules/playwright-core/index.mjs';
const [origin,output]=process.argv.slice(2);
assert(new URL(origin).hostname==='127.0.0.1');assert(!['5003','27019'].includes(new URL(origin).port));
const profile=await fs.mkdtemp(path.join(os.tmpdir(),'njfu-native-ui-'));
const result={checks:[],blocked:[],errors:[],screenshots:[]};let context;
try{
  context=await chromium.launchPersistentContext(profile,{headless:true,serviceWorkers:'block',viewport:{width:1440,height:1000},executablePath:'/Users/a0000/Library/Caches/ms-playwright/chromium-1217/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'});
  await context.route('**/*',route=>{if(new URL(route.request().url()).origin===origin)return route.continue();result.blocked.push(route.request().url());return route.abort();});
  const page=await context.newPage();page.on('pageerror',e=>result.errors.push(e.message));
  await page.goto(origin+'/#/pages/login/login');
  await page.getByRole('button',{name:'管理员',exact:true}).click();
  await page.locator('#login-password input').fill('isolated-root-password');
  await page.getByRole('button',{name:'进入管理后台',exact:true}).click();
  await page.getByText('快速执行 · 就绪',{exact:true}).waitFor();result.checks.push('actual_root_login_and_native_ready');
  for(const [device,width,height] of [['desktop',1440,1000],['ipad',834,1112],['phone',390,844]]){
    await page.setViewportSize({width,height});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'horizontal overflow '+device);
    const button=page.getByRole('button',{name:'暂停自动预约',exact:true});assert(await button.isVisible());
    const rect=await button.boundingBox();assert(rect.height>=40);
    await page.screenshot({path:path.join(output,device+'-root.png'),fullPage:true});result.screenshots.push(device+'-root.png');result.checks.push(device+'_root_layout');
  }
  // Explicit root controls exercise the real local API but no plans exist.
  await page.getByRole('button',{name:'暂停自动预约',exact:true}).click();
  await page.getByText('确定',{exact:true}).click();
  await page.getByText('快速执行 · 已暂停',{exact:true}).waitFor();result.checks.push('root_pause_truthful');
  await page.getByRole('button',{name:'继续自动预约',exact:true}).click();
  await page.getByText('确定',{exact:true}).click();
  await page.getByText('快速执行 · 就绪',{exact:true}).waitFor();result.checks.push('root_resume_truthful');
  await page.evaluate(async()=>{const b=await(await fetch('/api/local/bootstrap')).json();await fetch('/api/local/auth/logout',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':b.csrf},body:'{}'});});
  await page.goto(origin+'/#/pages/login/login');
  await page.locator('#login-user input').fill('2312000001');await page.locator('#login-password input').fill('fixture-password');
  await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.getByRole('heading',{name:'我的预约',exact:true}).waitFor();
  assert(!(await page.getByText('快速执行',{exact:false}).count()));assert(!(await page.getByRole('button',{name:'暂停自动预约',exact:true}).count()));result.checks.push('student_original_flow_and_no_native_controls');
  for(const [device,width,height] of [['desktop',1440,1000],['ipad',834,1112],['phone',390,844]]){
    await page.setViewportSize({width,height});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
    await page.screenshot({path:path.join(output,device+'-student.png'),fullPage:true});result.screenshots.push(device+'-student.png');result.checks.push(device+'_student_layout');
  }
  assert.deepEqual(result.blocked,[]);assert.deepEqual(result.errors,[]);
} catch(error){result.failure=String(error);throw error;}
finally{
  if(context)await context.close();
  await fs.writeFile(path.join(output,'browser.json'),JSON.stringify(result,null,2));
  const info=await fs.lstat(profile);assert(info.isDirectory()&&!info.isSymbolicLink()&&info.uid===process.getuid());
  assert(path.dirname(profile)===os.tmpdir()&&path.basename(profile).startsWith('njfu-native-ui-'));
  await fs.rm(profile,{recursive:true});
}
