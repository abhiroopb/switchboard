import type { PluginAPI } from '@ampcode/plugin'
import names from './switchboard-bundle.json'
import { bundle } from './switchboard/bundle'
import ephemeralClose from './switchboard/ephemeral-close'

export const description = 'switchboard lifecycle and automation plugins in one runtime; original per-module configuration and state keys remain unchanged.'

export default async function (amp: PluginAPI) {
  await bundle(amp, [
    ...names.map(name => [name, async (api: PluginAPI) => {
      const { default: initialize } = await import(`./${name}.ts`)
      await initialize(api)
    }] as [string, (api: PluginAPI) => Promise<void>]),
    ['ephemeral-close', ephemeralClose],
  ])
}
