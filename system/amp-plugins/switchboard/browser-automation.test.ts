import { expect, test } from 'bun:test'
import { browserAutomationBlockReason, SANCTIONED_BROWSER_PROFILE } from '../switchboard-guardrails'

test('Windows browser executables count as direct launches', () => {
  for (const cmd of [
    '"/c/Program Files/Google/Chrome/Application/chrome.exe" --version',
    '"C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe" --remote-debugging-port=9222',
    "'/c/Program Files/Microsoft/Edge/Application/msedge.exe' https://example.com",
    'chrome.exe https://example.com',
    'brave.exe',
  ]) expect(browserAutomationBlockReason(cmd)).toContain('sanctioned persistent profile')
})

test('reading about a browser executable is not a launch', () => {
  for (const cmd of [
    'tasklist | grep -c brave.exe',
    'ls "/c/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe"',
    'python3 system/scripts/owner_browser.py mode',
  ]) expect(browserAutomationBlockReason(cmd)).toBeNull()
})

test('the sanctioned headless launch still passes', () => {
  expect(browserAutomationBlockReason(`npx chrome-devtools-mcp --headless=true --userDataDir=${SANCTIONED_BROWSER_PROFILE}`)).toBeNull()
})
