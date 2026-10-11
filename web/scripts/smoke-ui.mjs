/** End-to-end checks against a running camhal server.
 * CAMHAL_BASE_URL selects an isolated server; the suite creates simulation runs.
 * CHROME_EXECUTABLE optionally uses an installed Chrome instead of Playwright Chromium.
 * CAMHAL_SCREENSHOTS optionally saves desktop/mobile screenshots for visual review.
 */
import { chromium } from 'playwright'
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'

const base = process.env.CAMHAL_BASE_URL ?? 'http://127.0.0.1:8010'
const screenshots = process.env.CAMHAL_SCREENSHOTS
const browser = await chromium.launch({ headless: true, ...(process.env.CHROME_EXECUTABLE ? { executablePath: process.env.CHROME_EXECUTABLE } : {}) })
const context = await browser.newContext({ viewport: { width: 1512, height: 980 }, locale: 'ko-KR', colorScheme: 'light' })
const page = await context.newPage()
page.setDefaultTimeout(10000)
const errors = []
page.on('pageerror', (error) => errors.push(error.message))
async function shot(name) {
  if (!screenshots) return
  await mkdir(screenshots, { recursive: true })
  await page.screenshot({ path: `${screenshots}/${name}.png`, fullPage: true, animations: 'disabled' })
}
async function noHangul(locator, label) {
  const text = await locator.innerText()
  assert(!/[가-힣]/.test(text), `${label} contains untranslated Korean: ${text.match(/[^\n]*[가-힣][^\n]*/g)?.join('\n')}`)
}
async function fits(label) {
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), `${label} overflows horizontally`)
}
async function route(path) {
  await page.goto(`${base}/#/${path}`)
  await page.locator('.page-intro h1').waitFor()
}
try {
  await page.goto(base)
  await page.locator('.fault-title').waitFor()
  assert.equal(await page.locator('html').getAttribute('lang'), 'ko')
  assert.equal(await page.locator('.scenario-item').count(), 37)
  assert.equal(await page.locator('.metric-card').count(), 4)
  await shot('desktop-ko')
  await page.getByRole('button', { name: 'EN', exact: true }).click()
  assert.equal(await page.locator('html').getAttribute('lang'), 'en')
  await noHangul(page.locator('main'), 'English scenario page')
  await fits('Desktop')
  await shot('desktop-en')
  await page.reload()
  assert.equal(await page.locator('html').getAttribute('lang'), 'en', 'Language preference is not persisted')
  await page.getByRole('textbox', { name: 'Search scenarios' }).fill('oscillation')
  assert.equal(await page.locator('.scenario-item').count(), 1)
  await page.getByRole('textbox', { name: 'Search scenarios' }).fill('')
  await page.getByRole('combobox', { name: 'Category', exact: true }).selectOption('3A Tuning')
  assert.equal(await page.locator('.scenario-item').count(), 3)
  await page.getByRole('combobox', { name: 'Category', exact: true }).selectOption('')
  await route('faults/F04')
  await page.getByRole('spinbutton', { name: 'Seed', exact: true }).fill('11')
  await page.getByRole('button', { name: 'Run comparison', exact: true }).click()
  await page.locator('.run-result').waitFor()
  assert((await page.locator('.run-result').innerText()).includes('AE_OSCILLATION'))
  await page.getByRole('button', { name: 'View evidence', exact: false }).click()
  assert(await page.locator('.run-result pre').count() > 0)
  await page.getByRole('button', { name: 'KO', exact: true }).click()
  assert.equal(await page.getByRole('spinbutton', { name: '시드', exact: true }).inputValue(), '11')
  assert((await page.locator('.run-result').innerText()).includes('계약 위반'))
  await page.getByRole('button', { name: 'EN', exact: true }).click()
  await page.getByRole('button', { name: '✓ Fixed', exact: false }).click()
  assert((await page.locator('.run-result').innerText()).includes('no violations'))
  await shot('comparison-en')
  await page.getByRole('link', { name: 'Link to this run', exact: true }).click()
  await page.locator('.run-result').waitFor()
  await noHangul(page.locator('.run-result'), 'English saved result')
  await route('run')
  await page.getByRole('button', { name: 'Run simulation', exact: false }).click()
  await page.locator('.run-result').waitFor()
  assert((await page.locator('.run-result').innerText()).includes('No violations'))
  await route('sweep')
  await page.getByRole('button', { name: 'Run full sweep (74 runs)', exact: false }).click()
  await page.locator('tbody').waitFor()
  assert.equal(await page.locator('tbody tr').count(), 37)
  assert((await page.locator('main').innerText()).includes('37 / 37'))
  await noHangul(page.locator('main'), 'English validation matrix')
  await shot('matrix-en')
  await route('catalog')
  await noHangul(page.locator('main'), 'English platforms')
  await page.getByRole('button', { name: 'Workloads 37', exact: true }).click()
  await noHangul(page.locator('main'), 'English workloads')
  await page.getByRole('button', { name: 'Checkers 35', exact: true }).click()
  await noHangul(page.locator('main'), 'English checkers')

  // Exercise every fault through the API and its persisted, localized result view.
  const catalog = await (await page.request.get(`${base}/api/catalog`)).json()
  for (const fault of catalog.faults) {
    const response = await page.request.post(`${base}/api/faults/${fault.id}/compare`, { data: { seed: 7 } })
    assert(response.ok(), `${fault.id} comparison failed: ${response.status()}`)
    const { faulty, fixed } = await response.json()
    assert(faulty.verdict.detected, `${fault.id} failed detection`)
    assert(fixed.verdict.clean, `${fault.id} fix has violations`)
    await route(`history/${faulty.id}`)
    await page.locator('.run-result').waitFor()
    // Raw logs and evidence remain in their source language by design.
    const text = await page.locator('.run-result').evaluate((node) => {
      const clone = node.cloneNode(true)
      clone.querySelectorAll('[data-original-content], pre').forEach((el) => el.remove())
      return clone.textContent
    })
    assert(!/[가-힣]/.test(text), `${fault.id} result contains untranslated Korean: ${text.match(/.{0,30}[가-힣].{0,70}/g)}`)
    await route(`faults/${fault.id}`)
    await page.locator('.fault-title').waitFor()
    await noHangul(page.locator('.fault-overview'), `${fault.id} description`)
  }
  console.log('PASS: 37 fault/fix pairs, all descriptions and persisted results localized')
  await page.getByRole('button', { name: 'Dark theme', exact: true }).click()
  assert(await page.locator('html').evaluate((el) => el.classList.contains('dark')))
  await shot('desktop-dark')
  await page.reload()
  assert(await page.locator('html').evaluate((el) => el.classList.contains('dark')), 'Theme preference is not persisted')
  await page.getByRole('button', { name: 'Light theme', exact: true }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await route('faults/F04')
  await page.locator('.fault-title').waitFor()
  await fits('Mobile scenario')
  await shot('mobile-en')
  await page.getByRole('button', { name: 'Open navigation', exact: true }).click()
  assert(await page.locator('.app-sidebar').evaluate((el) => el.classList.contains('is-open')))
  await page.locator('.side-nav').getByRole('link', { name: 'Playground', exact: true }).click()
  assert(!(await page.locator('.app-sidebar').evaluate((el) => el.classList.contains('is-open'))))
  await page.getByRole('button', { name: 'Open navigation', exact: true }).click()
  await page.keyboard.press('Escape')
  assert(!(await page.locator('.app-sidebar').evaluate((el) => el.classList.contains('is-open'))))
  await fits('Mobile playground')
  await page.setViewportSize({ width: 320, height: 720 })
  await fits('Narrow mobile playground')
  await route('catalog')
  await page.getByRole('button', { name: 'Platforms 10', exact: true }).waitFor()
  await fits('Narrow mobile catalog')
  await route('faults/F36')
  await page.locator('.fault-title').waitFor()
  await fits('Narrow mobile scenario')
  await page.getByRole('button', { name: 'KO', exact: true }).click()
  await shot('mobile-ko')
  await page.getByRole('button', { name: 'EN', exact: true }).click()
  await route('history/missing-run-for-ui-test')
  await page.getByRole('alert').waitFor()
  await noHangul(page.getByRole('alert'), 'Missing-run error')
  await page.route('**/api/runs/missing-run-for-ui-test', (request) => request.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ detail: '인증이 필요합니다.' }) }))
  await page.reload()
  await page.getByRole('link', { name: 'Sign in again', exact: true }).waitFor()
  await noHangul(page.getByRole('alert'), 'Expired-session error')
  assert.equal(errors.length, 0, `Browser errors: ${errors.join('; ')}`)
  console.log('PASS: language/theme persistence, filtering, comparison, history, playground, sweep, catalog and responsive layout')
} finally {
  await browser.close()
}
