import { test, expect, type Page } from '@playwright/test';
import { createIsolatedSession, expectNoRoleErrors, openRolePage, waitForItemLibrarySync } from './e2e-helpers';

// First-run sessions auto-open the onboarding walkthrough, whose overlay
// intercepts pointer events. Skip it (also marks it seen) if it appears.
async function dismissOnboarding(page: Page) {
  const overlay = page.locator('#ob-overlay.ob-open');
  try {
    await overlay.waitFor({ state: 'visible', timeout: 7000 });
    await page.locator('#ob-skip-btn').click();
    await overlay.waitFor({ state: 'hidden', timeout: 5000 });
  } catch {
    // Walkthrough never auto-opened (already seen) — nothing to dismiss.
  }
}

// Regression: the player map quickbar must expose Notes (journal flyout with the
// personal-notes companion panel + sticky notes popout). Players lost this when
// the legacy left rail (which carried rail-journal-btn) was hidden for the
// player role by the map-first role-panels redesign.
test('player quickbar Notes opens journal with personal notes and sticky popout', async ({ browser, request }) => {
  const session = await createIsolatedSession(request, browser, 'qb-notes');
  const player = await openRolePage(browser, session, 'player');
  await waitForItemLibrarySync(player.page);
  await dismissOnboarding(player.page);

  await expect(player.page.locator('#topbar-role')).toContainText(/Player/i);

  // Quickbar shows the full tool set, including the restored Notes + Recenter.
  const quickbar = player.page.locator('#rp-quickbar');
  await expect(quickbar).toBeVisible();
  for (const id of ['rp-tool-move', 'rp-tool-ruler', 'rp-tool-shapes', 'rp-tool-ping', 'rp-tool-notes', 'rp-tool-recenter', 'rp-tool-sound']) {
    await expect(quickbar.locator(`#${id}`)).toBeVisible();
  }

  // Notes opens the journal flyout with the player companion panel (Personal Notes).
  await player.page.locator('#rp-tool-notes').click();
  await expect(player.page.locator('#flyout-journal')).toBeVisible();
  const companion = player.page.locator('#player-companion-panel');
  await expect(companion).toBeVisible();
  await expect(companion.locator('#player-companion-personal')).toBeVisible();

  // Sticky launcher in the Personal Notes header opens the sticky notes popout.
  await companion.getByRole('button', { name: 'Sticky' }).click();
  await expect(player.page.locator('#sticky-notes-widget')).toBeVisible();
  await expect(player.page.locator('#sticky-notes-private')).toBeVisible();

  // Tapping Notes again closes the flyout (same toggle contract as the old rail button).
  await player.page.locator('#sticky-notes-close').click();
  await player.page.locator('#rp-tool-notes').click();
  await expect(player.page.locator('#flyout-journal')).toBeHidden();

  // Recenter resets the camera without errors.
  await player.page.locator('#rp-tool-recenter').click();

  // Optional 3D-dice CDN assets (three.js/cannon-es) are unreachable in
  // offline/proxied dev sandboxes; that noise is unrelated to this flow.
  // Real page errors and app-server failures still fail the test.
  const cdnNoise = /cdn\.jsdelivr\.net|net::ERR_TUNNEL_CONNECTION_FAILED|net::ERR_CONNECTION_RESET/i;
  player.errors = player.errors.filter(e => !cdnNoise.test(e));
  await expectNoRoleErrors(player);
});
