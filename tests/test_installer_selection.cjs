'use strict';
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');
const source = fs.readFileSync(path.join(__dirname, '../packaging/installer_checks.js'), 'utf8');
const samples = [
  ['Apple native', {'hw.optional.arm64': 1, 'hw.machine': 'arm64'}, 'arm64'],
  ['Apple in Rosetta', {'hw.optional.arm64': 1, 'sysctl.proc_translated': 1, 'hw.machine': 'x86_64'}, 'arm64'],
  ['Apple translated fallback', {'sysctl.proc_translated': '1', 'hw.machine': 'x86_64'}, 'arm64'],
  ['Apple machine fallback', {'hw.machine': 'arm64'}, 'arm64'],
  ['Intel', {'hw.optional.arm64': 0, 'hw.machine': 'x86_64'}, 'x86_64'],
  ['Intel missing optional key', {'hw.machine': 'x86_64'}, 'x86_64'],
  ['unknown', {'hw.machine': 'unknown'}, ''],
  ['future unknown', {'hw.optional.arm64': 2, 'hw.machine': 'x86_64'}, ''],
];
for (const [label, hardware, expected] of samples) {
  const context = {system: {version: {ProductVersion: '12.0'},
    compareVersions(a, b) {return Number(a.split('.')[0]) - Number(b.split('.')[0]);},
    sysctl(key) {if (!(key in hardware)) throw new Error('missing key'); return hardware[key];}}, my: {result: {}}};
  vm.createContext(context);
  vm.runInContext(source, context);
  assert.strictEqual(context.targetArchitecture(), expected, label);
  assert.strictEqual(context.checkRequirements(), Boolean(expected), label);
  assert.strictEqual(['arm64', 'x86_64'].filter(arch => context.targetArchitecture() === arch).length,
    expected ? 1 : 0, label + ': components must be exclusive');
  context.system.version.ProductVersion = '11.7';
  assert.strictEqual(context.checkRequirements(), false, label + ': old OS must stop');
}
console.log('PASS: installer chooses one native component; Intel, Apple, Rosetta, unknown and old OS');
