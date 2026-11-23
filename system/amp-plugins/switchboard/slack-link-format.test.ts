import { expect, test } from 'bun:test'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { slackBareUrls, slackBashMessageTexts, slackBashMrkdwnTexts, slackBlocksTexts } from '../switchboard-guardrails'

// The shared Slack link cases. system/scripts/test_slack_format.py
// runs the same fixture against slack_format.py, so the two detectors stay aligned.
// Run: bun test system/amp-plugins/switchboard/slack-link-format.test.ts

type Cases = {
  text: Array<{ name: string; text: string; bare_urls: string[] }>
  blocks: Array<{ name: string; blocks: unknown; bare_urls: string[] }>
}
const CASES: Cases = JSON.parse(readFileSync(`${import.meta.dir}/test-fixtures/slack-link-format-cases.json`, 'utf8'))

const dedupe = (items: string[]) => [...new Set(items)]
const blocksBareUrls = (blocks: unknown) => dedupe(slackBlocksTexts(blocks).flatMap(slackBareUrls))

test('text cases match slack_format.py', () => {
  for (const c of CASES.text) expect([c.name, slackBareUrls(c.text)]).toEqual([c.name, c.bare_urls])
})

test('blocks cases match slack_format.py, as objects and as JSON strings', () => {
  for (const c of CASES.blocks) {
    expect([c.name, blocksBareUrls(c.blocks)]).toEqual([c.name, c.bare_urls])
    expect([c.name, blocksBareUrls(JSON.stringify(c.blocks))]).toEqual([c.name, c.bare_urls])
  }
})

test('a --blocks "$(cat file)" payload is parsed as Block Kit, not read as raw prose', async () => {
  // The raw file text carries "url": "https://…" for a labelled link, which must not read as a bare URL.
  const labelled = CASES.blocks.find((c) => c.name === 'labelled rich_text link')!
  const dir = mkdtempSync(join(tmpdir(), 'switchboard-slack-blocks-'))
  const file = join(dir, 'blocks.json').replace(/\\/g, '/')
  writeFileSync(file, JSON.stringify(labelled.blocks))
  for (const cmd of [
    `switchboard slack post --channel C0123456789 --text "fallback" --blocks "$(cat ${file})"`,
    `switchboard slack post --channel C0123456789 --text "fallback" --blocks="$(cat '${file}')"`,
  ]) {
    const texts = await slackBashMessageTexts(cmd)
    expect(texts.flatMap(slackBareUrls)).toEqual([])
    expect(texts.join('\n')).toContain('the review note')
    expect(texts.join('\n')).not.toContain('"url"')
  }
  const unlabelled = CASES.blocks.find((c) => c.name === 'unlabelled rich_text link')!
  writeFileSync(file, JSON.stringify(unlabelled.blocks))
  const texts = await slackBashMessageTexts(`switchboard slack post --channel C0123456789 --blocks "$(cat ${file})"`)
  expect(dedupe(texts.flatMap(slackBareUrls))).toEqual(unlabelled.bare_urls)
  expect(await slackBashMrkdwnTexts(`switchboard slack post --channel C0123456789 --blocks "$(cat ${file})"`)).toEqual([])
})

test('a $(cat) blocks file that is not JSON is still read raw, so nothing is skipped', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'switchboard-slack-blocks-'))
  const file = join(dir, 'blocks.txt').replace(/\\/g, '/')
  writeFileSync(file, 'not json https://example.com/raw')
  const texts = await slackBashMessageTexts(`switchboard slack post --channel C0123456789 --blocks "$(cat ${file})"`)
  expect(texts.flatMap(slackBareUrls)).toContain('https://example.com/raw')
})
