import "@testing-library/jest-dom/vitest";

// jsdom has no layout engine. Return a Promise like current Chromium does, so an effect that returns
// scrollIntoView()'s result fails here instead of blanking the page in production.
Element.prototype.scrollIntoView = (() => Promise.resolve()) as unknown as Element["scrollIntoView"];
