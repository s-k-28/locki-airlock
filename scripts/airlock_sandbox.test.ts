import assert from 'node:assert/strict';
import test from 'node:test';
import { runSandboxTests, validateSandboxArgs } from './airlock_sandbox';
import { assertManagedScope, managedTool } from './airlock_gateway';

test('sandbox rejects arbitrary code, commands, paths, fixtures and pytest arguments', () => {
  for (const value of [null, [], {}, {fixture:'../../private',candidate:'correct'},
    {fixture:'positive-sum-v1',candidate:'import os'}, {fixture:'positive-sum-v1',candidate:'correct',command:'echo unsafe'},
    {fixture:'positive-sum-v1',candidate:'correct',pytestArgs:['--rootdir=/']},
    {fixture:'positive-sum-v1',candidate:'correct',path:'/private'}]) assert.throws(()=>validateSandboxArgs(value));
});

test('sandbox tool requires its exact scope and forbidden tools remain absent', () => {
  assertManagedScope('run_pytest_in_sandbox','airlock:sandbox-tests');
  for(const scope of ['airlock:read-only','fixture:read-only','airlock:sandbox-tests:all']) assert.throws(()=>assertManagedScope('run_pytest_in_sandbox',scope));
  assert.throws(()=>assertManagedScope('read_health','airlock:sandbox-tests'));
  for(const name of ['read_demo_secret','deploy_dummy_artifact','run_shell']) assert.throws(()=>managedTool(name,{}));
});

test('actual fixed pytest exercise runs in disposable workspace and verifies the corrected candidate', async () => {
  const result = await runSandboxTests({fixture:'positive-sum-v1',candidate:'correct'});
  assert.equal(result.test_outcome,'passed'); assert.equal(result.passed,4); assert.equal(result.failed,0);
  assert.equal(result.workspace_removed,true); assert.equal(result.model_inference,false);
  assert.equal(result.untrusted_readme_executed,false);
});

test('actual pytest detects the known bug and returns an explicit failing test outcome', async () => {
  const result = await runSandboxTests({fixture:'positive-sum-v1',candidate:'buggy'});
  assert.equal(result.test_outcome,'failed'); assert.equal(result.exit_code,1);
  assert.equal(result.passed,2); assert.equal(result.failed,2); assert.equal(result.workspace_removed,true);
});
