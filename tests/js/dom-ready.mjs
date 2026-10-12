// jsdom parses a page synchronously but fires DOMContentLoaded itself, one task after
// `new JSDOM()`; until then readyState is "loading". A harness that dispatched its own
// event on top ran the page's init twice, so every listener the init registers ran twice
// too. Call this right after constructing the window, before evaluating the page's
// scripts, and await the promise instead of dispatching anything.
export function domContentLoaded(window) {
  const state = window.document.readyState;
  if (state !== 'loading') {
    throw new Error(`DOMContentLoaded already fired (readyState "${state}"): the page's init `
      + 'would never run. Evaluate the page scripts in the same task as new JSDOM().');
  }
  return new Promise((resolve) => {
    window.document.addEventListener('DOMContentLoaded', resolve, { once: true });
  });
}
