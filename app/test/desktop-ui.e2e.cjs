'use strict';
// Headless, keyless interaction checks. Audio and daemon calls are mocked.
process.chdir(require('node:path').resolve(__dirname, '../..'));
require('node:fs').mkdirSync('.context/review', {recursive:true});
let browser;
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  browser = await chromium.launch({...(process.env.ECHOECHO_TEST_BROWSER_EXECUTABLE ? {executablePath:process.env.ECHOECHO_TEST_BROWSER_EXECUTABLE} : {}),headless:true,args:['--no-sandbox']});
  const page = await browser.newPage({viewport:{width:660,height:920},reducedMotion:'reduce'});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(() => {
    window.__calls=[];
    window.__status={version:'0.2',sha:'review preview',viewer:true,vm:true,loginItem:false,microphonePermission:'granted',preferences:{voiceModel:'gpt-live-1',inputDevice:'MacBook Pro Microphone',outputDevice:'Headphones',recordSessions:false},voice:{phase:'ready',session:'IDLE',model:'gpt-live-1',inputDevice:'MacBook Pro Microphone',outputDevice:'Headphones',captureActive:true,captureAge:.1,inputLevel:.36,configuredVoiceModel:'gpt-live-1',configuredInputDevice:'MacBook Pro Microphone',configuredOutputDevice:'Headphones',recordSessions:false,devices:[{name:'MacBook Pro Microphone',input:true,output:false},{name:'Headphones',input:false,output:true}],tasks:[{id:'t1',title:'Draft a weekend plan',kind:'agent.run',status:'running',progress:'Putting the itinerary together.'},{id:'t2',title:'Organize meeting notes',kind:'agent.run',status:'done',say:'Your notes are ready in the shared workspace.'}]}};
    window.ctl={status:async()=>structuredClone(window.__status),preferences:async p=>{window.__calls.push(['preferences',p]);window.__status.preferences=p;return p;},setLoginItem:async()=>({ok:true}),action:async name=>{window.__calls.push(['action',name]);return window.__failAction?{ok:false,output:'VM unavailable. Check Local Network permission.'}:{ok:true};},command:async data=>{
      window.__calls.push(['command',data]);const v=window.__status.voice;
      if(data.action==='pause') {v.phase='paused';v.captureActive=false;}
      if(data.action==='resume') {v.phase='ready';v.captureActive=true;}
      if(data.action==='wake') {v.phase='conversation';v.session='ACTIVE';v.captureActive=true;}
      if(data.action==='end') {v.phase='ready';v.session='IDLE';}
      if(data.action==='submit') v.tasks.push({id:'t3',title:data.text,kind:'agent.run',status:'queued'});
      if(data.action==='cancel') v.tasks.find(t=>t.id===data.task_id).status='cancelled';
      if(data.action==='configure') {v.configuredVoiceModel=window.__status.preferences.voiceModel;v.recordSessions=window.__status.preferences.recordSessions;}
      return {ok:true};
    }};
  });
  await page.goto('file://'+path.resolve('app/renderer/control.html'));
  await page.getByRole('heading',{name:'Ready when you are'}).waitFor();
  assert.equal(await page.locator('#input-name').textContent(),'MacBook Pro Microphone');
  await page.screenshot({path:'.context/review/control-preview.png',fullPage:true});
  await page.getByRole('button',{name:'Pause listening',exact:true}).click();
  await page.getByRole('heading',{name:'Listening is paused'}).waitFor();
  await page.locator('#task-input').fill('Draft an email <img src=x onerror=alert(1)>');
  await page.getByRole('button',{name:'Send task'}).click();
  await page.getByText('Draft an email <img src=x onerror=alert(1)>',{exact:true}).waitFor();
  assert.equal(await page.locator('#tasks img').count(),0);
  await page.getByRole('button',{name:'Cancel Draft a weekend plan'}).click();
  await page.getByText('Canceled',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Resume listening',exact:true}).click();
  await page.getByRole('heading',{name:'Ready when you are'}).waitFor();
  await page.getByRole('button',{name:'Talk now',exact:true}).click();
  await page.getByRole('heading',{name:'I’m listening'}).waitFor();
  await page.getByRole('button',{name:'End conversation',exact:true}).click();
  await page.getByRole('heading',{name:'Ready when you are'}).waitFor();
  await page.locator('#settings summary').click();
  await page.locator('#voice-model').selectOption('gpt-realtime-2.1');
  await page.waitForTimeout(900);
  assert.equal(await page.locator('#voice-model').inputValue(),'gpt-realtime-2.1');
  await page.locator('#record').check();
  await page.getByRole('button',{name:'Save voice settings',exact:true}).click();
  await page.getByText('Saved. During a conversation, model and device changes apply to the next one.').waitFor();
  assert.equal(await page.evaluate(()=>window.__status.preferences.voiceModel),'gpt-realtime-2.1');
  assert.equal(await page.evaluate(()=>window.__status.preferences.recordSessions),true);
  await page.evaluate(()=>window.__failAction=true);
  await page.getByRole('button',{name:'Open shared VM',exact:false}).click();
  await page.getByText('VM unavailable. Check Local Network permission.',{exact:true}).waitFor();
  assert.equal(await page.locator('#note.error').count(),1);
  await page.evaluate(()=>{window.__status.voice.captureAge=5;});
  await page.getByRole('heading',{name:'Waiting for microphone audio'}).waitFor();
  await page.screenshot({path:'.context/review/control-settings.png',fullPage:true});
  assert.deepEqual(errors,[]);
  fs.writeFileSync('.context/review/ui-result.json',JSON.stringify({passed:true,checks:['actual renderer loads','pause/resume','typed tasks while paused','untrusted text stays inert','task cancellation','manual wake/end','settings survive polling','private preferences saved','VM failure is visible','stalled microphone is visible'],pageErrors:errors},null,2));
  await browser.close();console.log('Desktop UI: 10 functional checks passed, no page errors.');
})().finally(async()=>{if(browser)await browser.close();}).catch(e=>{console.error(e);process.exit(1);});
