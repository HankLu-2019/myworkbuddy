import "@testing-library/jest-dom/vitest";

// jsdom has no layout engine. Return a Promise like current Chromium does, so an effect that returns
// scrollIntoView()'s result fails here instead of blanking the page in production.
Element.prototype.scrollIntoView = (() => Promise.resolve()) as unknown as Element["scrollIntoView"];

// jsdom replaces AbortController/AbortSignal, and Node's fetch Request (undici) rejects jsdom's signals; the data
// router builds a Request for every navigation, which then throws on Node 24 ("Expected signal to be an
// instance of AbortSignal"). The app has no route loaders to abort, so tests drop the signal.
const NodeRequest = globalThis.Request;
globalThis.Request = class extends NodeRequest {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    const { signal: _signal, ...rest } = init ?? {};
    super(input, rest);
  }
} as typeof Request;
