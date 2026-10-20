import "@testing-library/jest-dom/vitest";

// jsdom has no layout engine.
Element.prototype.scrollIntoView = () => {};
