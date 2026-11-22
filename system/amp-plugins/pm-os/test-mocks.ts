// Module-mock hygiene for the plugin test suite.
//
// Bun keeps a mock.module() replacement for the rest of the process: the next
// test file that imports the same module gets the mock, and mock.restore()
// does not undo it. One `bun test system/amp-plugins` run loads every file in
// one process, so a file that mocks ./lib with five exports leaves every later
// file reading that five-export lib ("Export named 'HOME' not found").
//
// Every test file that calls mock.module() on one of the modules below adds
//   afterAll(restoreModuleMocks)
// at top level. That puts the real exports back in place when the file ends,
// so modules that already imported them (live bindings) and files that load
// later both see the real module again.
import { mock } from 'bun:test'
import * as childProcess from 'node:child_process'
import * as fsPromises from 'node:fs/promises'
import * as lib from './lib'

// Copies taken when this helper first loads, before any file mocks them:
// a namespace object itself changes in place under mock.module().
const REAL: Record<string, Record<string, unknown>> = {
  './lib': { ...lib },
  'node:fs/promises': { ...fsPromises },
  'node:child_process': { ...childProcess },
}

/** Put every mockable module back to its real exports. */
export function restoreModuleMocks(): void {
  for (const [specifier, real] of Object.entries(REAL)) mock.module(specifier, () => ({ ...real }))
}
