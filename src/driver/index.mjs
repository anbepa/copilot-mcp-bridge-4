import { PlaywrightDriver } from './playwright.mjs';
import { MockDriver } from './mock.mjs';

export function createDriver(kind, cfg, opts = {}) {
  if (kind === 'mock') return new MockDriver({ scenario: opts.scenario, mode: opts.mockMode });
  return new PlaywrightDriver(cfg);
}

export { PlaywrightDriver, MockDriver };
