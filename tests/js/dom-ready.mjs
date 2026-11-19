import { JSDOM } from 'jsdom';

// jsdom 31 can return an interactive document before it fires DOMContentLoaded.
// Watch the event from before parsing, then evaluate the page scripts before awaiting it.
export function createTestPage(html) {
  let loaded = false;
  let pageReady;
  const { window } = new JSDOM(html, {
    pretendToBeVisual: true,
    runScripts: 'outside-only',
    beforeParse(window) {
      pageReady = new Promise((resolve) => {
        window.document.addEventListener('DOMContentLoaded', () => {
          loaded = true;
          resolve();
        }, { once: true });
      });
    },
  });
  if (loaded) {
    window.close();
    throw new Error('DOMContentLoaded fired before the test could evaluate the page scripts.');
  }
  return { window, pageReady };
}
