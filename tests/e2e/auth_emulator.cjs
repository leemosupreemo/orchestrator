// Run only through test_auth_emulator.py: SDK and emulator are isolated in its temporary directory.
'use strict';
const assert = require('node:assert/strict');
const firebase = require('firebase/compat/app');
require('firebase/compat/auth');
const reset = require(process.env.ORCHESTRATOR_RESET_MODULE);

(async () => {
  const projectId = 'demo-orchestrator-auth-test';
  const host = process.env.FIREBASE_AUTH_EMULATOR_HOST || '';
  assert.match(host, /^127\.0\.0\.1:\d+$/, 'Only a loopback Auth emulator is allowed');
  const base = `http://${host}`;
  const app = firebase.initializeApp({ projectId, apiKey: 'fake-api-key', authDomain: `${projectId}.firebaseapp.com` });
  const auth = app.auth();
  auth.useEmulator(base, { disableWarnings: true });
  const email = 'reset-check@example.invalid';
  const oldPassword = 'original-password-731';
  const newPassword = 'replacement-password-849';
  try {
    await auth.createUserWithEmailAndPassword(email, oldPassword);
    await auth.signOut();
    assert.equal((await reset.requestReset(auth, email)).ok, true);
    const response = await fetch(`${base}/emulator/v1/projects/${projectId}/oobCodes`);
    assert.equal(response.ok, true);
    const { oobCodes } = await response.json();
    const issued = oobCodes.filter(code => code.email === email && code.requestType === 'PASSWORD_RESET');
    assert.equal(issued.length, 1, 'The emulator must actually issue a reset action');
    const code = issued[0].oobCode;
    const link = reset.readLink(new URL(issued[0].oobLink).search);
    assert.deepEqual(link, { mode: 'resetPassword', code });
    assert.deepEqual(await reset.checkCode(auth, code), { ok: true, email });
    assert.deepEqual(await reset.setPassword(auth, code, newPassword, newPassword), { ok: true });
    const reused = await reset.setPassword(auth, code, 'another-password-943', 'another-password-943');
    assert.equal(reused.ok, false);
    assert.equal(reused.dead, true);
    assert.equal((await reset.checkCode(auth, code)).dead, true);
    await assert.rejects(auth.signInWithEmailAndPassword(email, oldPassword));
    const credential = await auth.signInWithEmailAndPassword(email, newPassword);
    assert.equal(credential.user.email, email);
    await auth.signOut();
    // Unknown accounts use the same neutral result through the real SDK as well.
    const unknown = await reset.requestReset(auth, 'no-account@example.invalid');
    assert.equal(unknown.ok, true);
    assert.match(unknown.message, /^If an account exists/);
    console.log('PASS: reset requested, code checked and consumed, new password accepted, old password and reused code rejected');
  } finally {
    // Delete only this temporary demo project's emulated accounts.
    await fetch(`${base}/emulator/v1/projects/${projectId}/accounts`, { method: 'DELETE' });
    await app.delete();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
