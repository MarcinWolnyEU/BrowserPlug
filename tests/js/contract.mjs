// Validate a hand-written JS payload fixture against tests/fixtures/connections_info_contract.json
// — the same file tests/unit/test_connections_contract.py holds the REAL Python emitter to.
//
// Why this exists: options-map.test.mjs and popup.test.mjs freeze the payload
// get_connections_info() emits. Frozen by hand, those fixtures survive a server-side key
// rename with every assertion green while the real map / real panel break on the renamed
// key. The contract closes the loop from this side: rename a key in the contract (because
// Python renamed it, because its own test just failed) and every fixture still spelling it
// the old way fails here as an UNKNOWN key.
//
// Two checks, deliberately different in strength:
//   * unknown keys are ALWAYS fatal — that is the rename detector, and it costs a partial
//     fixture nothing.
//   * missing REQUIRED keys are fatal only under { requireAll: true }. A fixture that is
//     deliberately partial (popup.test.mjs sends stale/pre-upgrade payloads on purpose)
//     passes; a fixture that claims to be "the payload as the server builds it"
//     (options-map.test.mjs) is held to the whole thing.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

export const CONTRACT = JSON.parse(readFileSync(
  new URL('../fixtures/connections_info_contract.json', import.meta.url), 'utf8'));

// JS has one number type, so "int" means integral and "number" means any finite number.
// `undefined` is MISSING, never a value — a key spelled with undefined is a typo's twin.
const CHECKS = {
  string: (v) => typeof v === 'string',
  int: (v) => typeof v === 'number' && Number.isInteger(v),
  number: (v) => typeof v === 'number' && Number.isFinite(v),
  bool: (v) => typeof v === 'boolean',
  object: (v) => v !== null && typeof v === 'object' && !Array.isArray(v),
  array: (v) => Array.isArray(v),
  null: (v) => v === null,
};

const typeOk = (value, allowed) => allowed.some((t) => {
  const check = CHECKS[t];
  assert.ok(check, `contract uses an unknown type "${t}" (see _types in the contract)`);
  return check(value);
});

const describe = (v) => (Array.isArray(v) ? 'array' : v === null ? 'null' : typeof v);

/**
 * Assert `value` conforms to the contract object named `objectName`.
 * @param {object}  value       the fixture (or a slice of it)
 * @param {string}  objectName  a key of contract.objects
 * @param {object}  opts        { requireAll, path }
 */
export function assertMatchesContract(value, objectName, { requireAll = false, path = objectName } = {}) {
  const spec = CONTRACT.objects[objectName];
  assert.ok(spec, `no "${objectName}" object in the contract`);
  assert.ok(CHECKS.object(value), `${path}: expected an object, got ${describe(value)}`);

  const required = spec.required || {};
  const optional = spec.optional || {};
  const known = { ...required, ...optional };

  // (1) The rename detector. A key the contract does not know is either a typo or a
  // spelling the emitter has moved on from — both are the bug this file exists to catch.
  for (const key of Object.keys(value)) {
    assert.ok(Object.prototype.hasOwnProperty.call(known, key),
      `${path}.${key} is not in the contract — either the fixture invented it, or `
      + `get_connections_info() renamed/dropped it and ${objectName} was not updated `
      + `(tests/fixtures/connections_info_contract.json)`);
  }

  // (2) Types, and — for a fixture that claims completeness — presence.
  for (const [key, allowed] of Object.entries(known)) {
    const present = Object.prototype.hasOwnProperty.call(value, key) && value[key] !== undefined;
    if (!present) {
      const isRequired = Object.prototype.hasOwnProperty.call(required, key);
      assert.ok(!(requireAll && isRequired),
        `${path}.${key} is missing — the contract requires it of every ${objectName}`);
      continue;
    }
    assert.ok(typeOk(value[key], allowed),
      `${path}.${key} is ${describe(value[key])} (${JSON.stringify(value[key])}); `
      + `the contract allows ${allowed.join(' | ')}`);
  }

  // (3) Recurse into arrays of contract objects (heterogeneous ones pick per element).
  for (const [key, arrSpec] of Object.entries(spec.arrays || {})) {
    const items = value[key];
    if (!Array.isArray(items)) continue;
    items.forEach((item, i) => {
      const itemPath = `${path}.${key}[${i}]`;
      let target = arrSpec.of;
      if (arrSpec.discriminator) {
        const tag = item && item[arrSpec.discriminator];
        target = arrSpec.of[tag];
        assert.ok(target, `${itemPath}: ${arrSpec.discriminator}="${tag}" is not a `
          + `kind the contract knows (${Object.keys(arrSpec.of).join(', ')})`);
      }
      assertMatchesContract(item, target, { requireAll, path: itemPath });
    });
  }

  // (4) And into nested objects (null = "not there", which the type check already allowed).
  for (const [key, childName] of Object.entries(spec.children || {})) {
    if (value[key] === null || value[key] === undefined) continue;
    assertMatchesContract(value[key], childName, { requireAll, path: `${path}.${key}` });
  }
}
