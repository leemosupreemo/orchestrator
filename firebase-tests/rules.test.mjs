import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { initializeTestEnvironment, assertFails, assertSucceeds } from '@firebase/rules-unit-testing';
import { doc, getDoc, setDoc } from 'firebase/firestore';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
let env;

before(async () => {
  env = await initializeTestEnvironment({
    projectId: 'demo-swift-orch-rules',
    firestore: { rules: fs.readFileSync(resolve(root, 'firestore.rules'), 'utf8') },
  });
});

after(async () => { if (env) await env.cleanup(); });

const owner = () => env.authenticatedContext('owner', { email: 'enmeskin@gmail.com', email_verified: true });
const outsider = () => env.authenticatedContext('outsider', { email: 'other@example.com', email_verified: true });
const command = () => ({ status: 'queued', requested_by: 'enmeskin@gmail.com', project_id: 'test',
  operation: 'job.schedule', job_id: 'job-1', expected_revision: 1, inputs: {} });

test('unsigned and unprovisioned users cannot read jobs', async () => {
  await assertFails(getDoc(doc(env.unauthenticatedContext().firestore(), 'projects/test/jobs/job-1')));
  await assertFails(getDoc(doc(outsider().firestore(), 'projects/test/jobs/job-1')));
  await assertSucceeds(getDoc(doc(owner().firestore(), 'projects/test/jobs/job-1')));
});

test('only the owner can enqueue typed commands', async () => {
  await assertSucceeds(setDoc(doc(owner().firestore(), 'commands/valid'), command()));
  await assertFails(setDoc(doc(outsider().firestore(), 'commands/other'), command()));
  await assertFails(setDoc(doc(owner().firestore(), 'commands/shell'), { ...command(), operation: 'shell.exec' }));
});

test('browser cannot write job state or command results', async () => {
  await assertFails(setDoc(doc(owner().firestore(), 'projects/test/jobs/job-1'), { status: 'completed' }));
  await assertFails(setDoc(doc(owner().firestore(), 'commands/result'), { ...command(), status: 'succeeded' }));
});

test('artifact chunks are owner-only and bounded', async () => {
  const metadata = { name: 'reference.png', content_type: 'image/png', size: 3, chunk_count: 1 };
  const ownerDb = owner().firestore();
  await assertSucceeds(setDoc(doc(ownerDb, 'projects/test/artifacts/ref-1'), metadata));
  await assertSucceeds(setDoc(doc(ownerDb, 'projects/test/artifacts/ref-1/chunks/0000'), { data: 'AQID' }));
  await assertFails(setDoc(doc(outsider().firestore(), 'projects/test/artifacts/ref-1/chunks/0001'), { data: 'AQID' }));
  await assertFails(setDoc(doc(ownerDb, 'projects/test/artifacts/ref-1/chunks/0002'), { data: 'a'.repeat(180001) }));
  await assertFails(setDoc(doc(ownerDb, 'projects/test/artifacts/script'), { ...metadata, content_type: 'application/x-sh' }));
});
