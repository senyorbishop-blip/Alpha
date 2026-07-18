import { expect, test } from '@playwright/test';
import { createIsolatedSession, openRolePage } from './e2e-helpers';

/**
 * Stream panel — Rewards tier rows and "Recent bridge log" must expand on
 * click (regression: native <details> rows showed an arrow but never opened
 * inside the DM rail shell, so tier tables couldn't be viewed or edited).
 *
 * Twitch OAuth status is mocked so the connected-state panel renders; the
 * rewards config itself comes from the real server over the live WebSocket
 * (dm_chat_rewards_get → shipped defaults).
 */
test('Rewards tiers and bridge log expand; power counts update live', async ({ browser, request }) => {
  const session = await createIsolatedSession(request, browser, 'rewards');
  const dm = await openRolePage(browser, session, 'dm');
  const page = dm.page;

  await page.route('**/api/twitch/status/**', route => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      connected: true,
      channel: 'teststream',
      enabled: true,
      persistence_mode: 'everything',
      bridge: {
        state: 'running', managed: true, retries: 0,
        log: ['[INFO] bridge line one', '[INFO] bridge line two'],
      },
    }),
  }));
  await page.route('**/api/overlay/urls/**', route => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ game_url: 'http://overlay.test/game', arena_url: 'http://overlay.test/arena' }),
  }));

  // Open the Stream mode in the DM rail — mounts the Twitch settings panel.
  await page.click('[data-dm-mode-button="stream"]');

  const rewards = page.locator('#dcx-stream-panel .twitch-rewards-wrap');
  await expect(rewards).toBeVisible({ timeout: 20_000 });

  // Tier rows render collapsed, with a live power count in the header.
  const subHeader = rewards.locator('.tw-collapse-header', { hasText: 'Single sub / resub' });
  await expect(subHeader).toBeVisible();
  await expect(subHeader).toContainText('(5 powers)');

  const subBody = subHeader.locator('xpath=following-sibling::div[contains(@class,"tw-collapse-body")][1]');
  await expect(subBody).toBeHidden();

  // Clicking the row text (not just the arrow glyph) expands the power list.
  await subHeader.click();
  await expect(subBody).toBeVisible();
  await expect(subHeader).toHaveAttribute('aria-expanded', 'true');
  await expect(subBody.getByText('Pebble Toss')).toBeVisible();
  await expect(subBody.getByText('Healing Spark')).toBeVisible();
  await expect(subBody.getByText('Fireball', { exact: true })).toBeVisible();

  // Ticking powers updates the "(N powers)" count live.
  const fireball = subBody.locator('label').filter({ hasText: /^Fireball$/ }).locator('input');
  await fireball.check();
  await expect(subHeader).toContainText('(6 powers)');
  await fireball.uncheck();
  await expect(subHeader).toContainText('(5 powers)');

  // The five newest powers are offered in the checklist too — the list is
  // generated from the server's power defs, so nothing is hardcoded here.
  await expect(subBody.getByText('Lightning Bolt', { exact: true })).toBeVisible();
  await expect(subBody.getByText('Magic Missile', { exact: true })).toBeVisible();
  await expect(subBody.getByText('Sleep', { exact: true })).toBeVisible();

  // Clicking again collapses.
  await subHeader.click();
  await expect(subBody).toBeHidden();
  await expect(subHeader).toHaveAttribute('aria-expanded', 'false');

  // Gift and bits tier rows expand the same way.
  const giftHeader = rewards.locator('.tw-collapse-header', { hasText: 'Gift subs ×5+' });
  await giftHeader.click();
  await expect(
    giftHeader.locator('xpath=following-sibling::div[contains(@class,"tw-collapse-body")][1]'),
  ).toBeVisible();

  const bitsHeader = rewards.locator('.tw-collapse-header', { hasText: 'Bits 1000+' });
  await bitsHeader.click();
  await expect(
    bitsHeader.locator('xpath=following-sibling::div[contains(@class,"tw-collapse-body")][1]'),
  ).toBeVisible();

  // "Recent bridge log" uses the same pattern and must expand too.
  const logHeader = page.locator('#dcx-stream-panel .tw-collapse-header', { hasText: 'Recent bridge log' });
  await expect(logHeader).toBeVisible();
  const logBody = logHeader.locator('xpath=following-sibling::div[contains(@class,"tw-collapse-body")][1]');
  await expect(logBody).toBeHidden();
  await logHeader.click();
  await expect(logBody).toBeVisible();
  await expect(logBody).toContainText('bridge line one');

  // Offline/CDN 4xx noise is environment-dependent; fail only on script
  // errors (pageerror) or anything implicating the Twitch settings panel.
  const relevant = dm.errors.filter(e => /\[pageerror\]|twitch_settings|tw-collapse/i.test(e));
  expect(relevant, `page errors: ${relevant.join(' | ')}`).toEqual([]);
  await dm.context.close();
});
