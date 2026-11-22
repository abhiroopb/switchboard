import type { PluginAPI } from '@ampcode/plugin'
import names from './pm-os-bundle.json'
import { bundle } from './pm-os/bundle'
import ephemeralClose from './pm-os/ephemeral-close'

export const description = 'PM OS lifecycle and automation plugins in one runtime; original per-module configuration and state keys remain unchanged.'

export default async function (amp: PluginAPI) {
  await bundle(amp, [
    ...names.map(name => [name, async (api: PluginAPI) => {
      const { default: initialize } = await import(`./${name}.ts`)
      await initialize(api)
    }] as [string, (api: PluginAPI) => Promise<void>]),
    ['ephemeral-close', ephemeralClose],
  ])
}
