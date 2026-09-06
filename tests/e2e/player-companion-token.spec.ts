import { test, expect } from '@playwright/test';
import { createIsolatedSession, expectNoRoleErrors, openRolePage, tokenByName } from './e2e-helpers';

test('player authors and places a companion token with stats', async ({ request, browser }, testInfo) => {
  const session = await createIsolatedSession(request, browser, 'player-companion');
  const player = await openRolePage(browser, session, 'player');

  await player.page.locator('#rail-char-token-btn').click();
  await expect(player.page.locator('#player-companion-creator')).toBeVisible();
  await player.page.locator('#companion-token-name').fill('Sprocket');
  await player.page.locator('#companion-token-kind').selectOption('Turret');
  await player.page.locator('#companion-token-hp').fill('18');
  await player.page.locator('#companion-token-ac').fill('15');
  await player.page.locator('#companion-token-speed').fill('20');
  await player.page.locator('#companion-token-init').fill('2');
  await player.page.locator('#companion-token-passive').fill('12');
  await player.page.locator('#companion-token-notes').fill('Force bolt; immune to poison.');
  await player.page.screenshot({ path: testInfo.outputPath('player-companion-creator.png'), fullPage: true });
  await player.page.getByRole('button', { name: 'Place Companion Token' }).click();

  await expect.poll(() => tokenByName(player.page, 'Sprocket')).not.toBeNull();
  const token = await tokenByName(player.page, 'Sprocket');
  expect(token).toMatchObject({ owner_id: session.playerUserId, tokenType: 'companion', hp: 18, maxHp: 18, ac: 15, speed: 20, initiativeMod: 2, passivePerception: 12 });
  await expectNoRoleErrors(player);
  await player.context.close();
});
