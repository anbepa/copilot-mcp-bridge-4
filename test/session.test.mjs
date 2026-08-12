/**
 * Regresión del fallo real (log de Andres, 2026-08-11 01:32):
 *
 *   ✗ La sesión NO persistió: el próximo `run` volverá a pedir login.
 *   ...y acto seguido `run` entró sin pedir credenciales.
 *
 * Causa: verifySession esperaba 25 s por el composer mientras que `run` esperaba 60 s.
 * En un tenant lento eso da un falso negativo. El daño no es cosmético: le dice a alguien
 * que repita un MFA corporativo que no hacía falta.
 *
 * Regla que estos tests protegen: solo afirmamos "expired" si VIMOS la pantalla de login.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { decideSessionOutcome } from '../src/util/session.mjs';

const alive = { status: 'alive' };
const expired = { status: 'expired', detail: 'apareció la pantalla de login' };
const unknown = { status: 'unknown', detail: 'no apareció el chat en 60s' };

test('headless ve el chat → viva, sin reintento', () => {
  const d = decideSessionOutcome(alive, null);
  assert.equal(d.outcome, 'alive');
  assert.equal(d.exitCode, 0);
  assert.equal(d.headlessBlocked, false);
});

test('headless falla pero con ventana funciona → viva y avisa de headless', () => {
  const d = decideSessionOutcome(unknown, alive);
  assert.equal(d.outcome, 'alive');
  assert.equal(d.exitCode, 0);
  assert.equal(d.headlessBlocked, true, 'debe recomendar --headed, no repetir el login');
});

test('EL FALLO REAL: ningún intento concluye → unknown, NUNCA expired', () => {
  const d = decideSessionOutcome(unknown, unknown);
  assert.equal(d.outcome, 'unknown');
  assert.notEqual(d.outcome, 'expired', 'no podemos afirmar que murió sin haberlo visto');
  assert.equal(d.exitCode, 0, 'salir con 0: lo más probable es que solo fuera lentitud');
});

test('solo se declara caducada si se vio la pantalla de login', () => {
  const d = decideSessionOutcome(expired, expired);
  assert.equal(d.outcome, 'expired');
  assert.equal(d.exitCode, 1);
});

test('login visto en headless, indeterminado con ventana → caducada', () => {
  assert.equal(decideSessionOutcome(expired, unknown).outcome, 'expired');
});

test('indeterminado en headless, login visto con ventana → caducada', () => {
  assert.equal(decideSessionOutcome(unknown, expired).outcome, 'expired');
});

test('un "alive" gana siempre a un "expired" previo', () => {
  // La sesión pudo renovarse sola entre un intento y otro: la evidencia positiva manda.
  assert.equal(decideSessionOutcome(expired, alive).outcome, 'alive');
  assert.equal(decideSessionOutcome(alive, expired).outcome, 'alive');
});

test('sin segundo intento, un unknown sigue siendo unknown', () => {
  const d = decideSessionOutcome(unknown);
  assert.equal(d.outcome, 'unknown');
  assert.equal(d.exitCode, 0);
});

test('propaga el detalle para que el mensaje sea accionable', () => {
  assert.match(decideSessionOutcome(unknown, unknown).detail, /no apareció el chat/);
  assert.match(decideSessionOutcome(expired, expired).detail, /pantalla de login/);
});

test('playwright ausente no se confunde con sesión caducada', () => {
  const sinPw = { status: 'unknown', detail: 'Playwright no está instalado' };
  const d = decideSessionOutcome(sinPw, sinPw);
  assert.equal(d.outcome, 'unknown');
  assert.equal(d.exitCode, 0);
});

test('un perfil bloqueado tampoco es sesión caducada', () => {
  const lock = { status: 'unknown', detail: 'no se pudo abrir el perfil: SingletonLock' };
  assert.equal(decideSessionOutcome(lock, lock).outcome, 'unknown');
});
