'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { validate, resume } = require('../scripts/resume-promotion.cjs');
const yaml = require('../apps/desktop/node_modules/js-yaml');
function fixture() {
  const plan = {schema:1,version:'v0.3.14',baseTag:'v0.3.13',baseCommit:'d'.repeat(40),source:'a'.repeat(40),
    request:'e'.repeat(32),commit:'b'.repeat(40),mode:'publish',fullDev:{tag:'v0.3.10-dev.26',commit:'f'.repeat(40)},
    features:[{id:'cost'}],patches:[],added:['cost']};
  const run = {status:'completed',conclusion:'failure',event:'workflow_dispatch',head_branch:'master',
    path:'.github/workflows/selective-promotion.yml',repository:{full_name:'duaragha/Serena'},head_sha:plan.source};
  const jobs = [{name:'windows',status:'completed',conclusion:'success'}, {name:'linux',status:'completed',conclusion:'failure'}];
  const inputs = {runId:'12345',source:'c'.repeat(40),request:plan.request,mode:'publish',stable:plan.baseTag,
    devTag:plan.fullDev.tag,devCommit:plan.fullDev.commit,selected:['cost'],tested:['cost']};
  return {plan,run,jobs,inputs};
}
test('resume requires the exact authorized candidate and completed successful Windows gate', () => {
  const {plan,run,jobs,inputs} = fixture();
  assert.doesNotThrow(() => validate(run,jobs,plan,inputs));
  for (const bad of [{status:'in_progress'},{conclusion:'success'},{event:'push'},{head_branch:'feature'},
    {path:'.github/workflows/unrelated.yml'},{head_sha:'0'.repeat(40)},{repository:{full_name:'other/repo'}}])
    assert.throws(() => validate({...run,...bad},jobs,plan,inputs));
  for (const conclusion of ['failure','skipped','cancelled'])
    assert.throws(() => validate(run,[{...jobs[0],conclusion},jobs[1]],plan,inputs));
  for (const bad of [{mode:'verify'},{request:'0'.repeat(32)},{stable:'v0.3.12'},
    {devCommit:'0'.repeat(40)},{selected:[]},{tested:[]}])
    assert.throws(() => validate(run,jobs,plan,{...inputs,...bad}));
});
test('resume checks bundle, ancestry, receipt and baseline before accepting Windows artifacts', t => {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'resume-promotion-'));
  t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const {plan,run,jobs,inputs}=fixture();
  const calls=[];
  let wrongBundle=false;
  const command=(binary,args)=>{
    calls.push([binary,...args]);
    if(binary==='gh'){
      if(args[0]==='run'){
        if(args.includes('candidate-source'))fs.writeFileSync(path.join(root,'promotion-plan.json'),JSON.stringify(plan));
        return '';
      }
      if(args[1].includes('/jobs?'))return JSON.stringify({jobs});
      if(args[1].endsWith('/latest'))return JSON.stringify({tag_name:plan.baseTag});
      return JSON.stringify(run);
    }
    if(args[0]==='rev-parse')return args[1]==='HEAD'?inputs.source:(wrongBundle?'0'.repeat(40):plan.commit);
    if(args[0]==='show')return JSON.stringify(plan);
    return '';
  };
  assert.deepEqual(resume(root,inputs,command),plan);
  assert.ok(calls.some(a=>a.includes('--is-ancestor')&&a.includes(plan.source)&&a.includes(inputs.source)));
  assert.equal(calls.filter(a=>a.includes('candidate-windows')).length,1);
  assert.equal(JSON.parse(fs.readFileSync(path.join(root,'resumed-from.json'))).candidate,plan.commit);
  calls.length=0;wrongBundle=true;
  assert.throws(()=>resume(root,inputs,command),/bundle commit mismatch/);
  assert.equal(calls.filter(a=>a.includes('candidate-windows')).length,0);
});
test('resumed gates restore the exact application tree and still require Linux success', () => {
  const workflow=yaml.load(fs.readFileSync(path.resolve(__dirname,'../.github/workflows/selective-promotion.yml'),'utf8'));
  assert.equal(workflow.jobs.windows.if,"inputs.resume_run == ''");
  assert.ok(workflow.jobs.finish.if.includes("needs.linux.result == 'success'"));
  assert.ok(workflow.jobs.finish.if.includes("inputs.resume_run != '' && needs.windows.result == 'skipped'"));
  assert.deepEqual(workflow.jobs.finish.needs,['linux','windows']);
  const steps=workflow.jobs.linux.steps;
  const restore=steps.findIndex(s=>s.name==='Restore the exact candidate tree before packaging');
  assert.ok(restore>0 && restore<steps.findIndex(s=>s.name==='Build stable AppImage without publishing'));
  assert.ok(steps[restore].run.includes('git diff --exit-code'));
  const prepare=workflow.jobs.prepare.steps.find(s=>s.name==='Validate request and assemble only selected patches');
  assert.ok(prepare.run.includes('node scripts/resume-promotion.cjs'));
});
