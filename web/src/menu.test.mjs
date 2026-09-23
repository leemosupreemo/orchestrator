import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
const manifest=JSON.parse(fs.readFileSync(new URL('./menu.json',import.meta.url)));
test('browser menu mirrors Python CLI menu contract',()=>{
  const script=`import json\nfrom orchestrator.web.menu import main_menu,configuration_menu\nfrom orchestrator.web.contract import Operation\nprint(json.dumps({'main':[{'label':i.label,'operation':str(i.operation) if i.operation else None} for i in main_menu()],'tools':[{'label':i.label,'operation':str(i.operation)} for i in configuration_menu()],'operations':[str(o) for o in Operation]}))`;
  const actual=JSON.parse(execFileSync('python3',['-c',script],{cwd:new URL('../..',import.meta.url)}).toString());
  assert.deepEqual(manifest,actual);
});
