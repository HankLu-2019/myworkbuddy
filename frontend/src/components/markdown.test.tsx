import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { render } from "@testing-library/react";
import { Markdown, safeUrl } from "./markdown";

function html(md: string): HTMLElement {
  return render(<Markdown>{md}</Markdown>).container;
}

describe("safe Markdown (design 01 §H4)", () => {
  it("never renders a remote image; shows a link with the host instead", () => {
    const c = html("Look ![chart](https://evil.example/a.png?d=secret)");
    expect(c.querySelector("img")).toBeNull();
    const link = c.querySelector("a.md-remote-image") as HTMLAnchorElement;
    expect(link.textContent).toContain("[image: chart]");
    expect(link.textContent).toContain("evil.example");
    expect(link.rel).toBe("noopener noreferrer nofollow");
  });

  it("blocks protocol-relative and other-scheme images", () => {
    for (const src of ["//evil.example/x.png", "http://evil.example/x.png", "ftp://evil.example/x.png"]) {
      expect(html(`![x](${src})`).querySelector("img")).toBeNull();
    }
  });

  it("renders relative and inline data images", () => {
    const c = html("![a](/assets/logo.png) ![b](data:image/png;base64,iVBORw0KGgo=)");
    const srcs = Array.from(c.querySelectorAll("img")).map((i) => i.getAttribute("src"));
    expect(srcs).toEqual(["/assets/logo.png", "data:image/png;base64,iVBORw0KGgo="]);
  });

  it("rejects non-image data URLs and script data in images", () => {
    expect(html("![x](data:text/html;base64,PHNjcmlwdD4=)").querySelector("img")).toBeNull();
    expect(html("![x](data:image/svg+xml;base64,PHN2Zz4=)").querySelector("img")).toBeNull();
  });

  it("neutralises javascript: and other unsafe link schemes", () => {
    const c = html("[x](javascript:alert(1)) [y](vbscript:msgbox) [z](data:text/html,hi)");
    for (const a of Array.from(c.querySelectorAll("a"))) expect(a.getAttribute("href")).toBe("#");
  });

  it("opens external links safely and shows their host", () => {
    const a = html("[docs](https://docs.example.com/page)").querySelector("a") as HTMLAnchorElement;
    expect(a.target).toBe("_blank");
    expect(a.rel).toBe("noopener noreferrer nofollow");
    expect(a.textContent).toBe("docs (docs.example.com)");
  });

  it("keeps in-app links in the same tab", () => {
    const a = html("[page](/wiki/12)").querySelector("a") as HTMLAnchorElement;
    expect(a.getAttribute("href")).toBe("/wiki/12");
    expect(a.target).toBe("");
  });

  it("does not render raw HTML", () => {
    const c = html('<img src="https://evil.example/x.png"><script>alert(1)</script>');
    expect(c.querySelector("img")).toBeNull();
    expect(c.querySelector("script")).toBeNull();
  });

  it("transforms URLs per attribute", () => {
    expect(safeUrl("data:image/png;base64,AAAA", "src", {} as never)).toBe("data:image/png;base64,AAAA");
    expect(safeUrl("data:image/png;base64,AAAA", "href", {} as never)).toBe("#");
    expect(safeUrl("mailto:a@b.c", "href", {} as never)).toBe("mailto:a@b.c");
  });
});

describe("source rules", () => {
  function files(dir: string): string[] {
    return readdirSync(dir).flatMap((n) => {
      const p = join(dir, n);
      return statSync(p).isDirectory() ? files(p) : /\.(ts|tsx)$/.test(n) && !/\.test\./.test(n) ? [p] : [];
    });
  }
  const sources = files(join(__dirname, ".."));

  it("never uses raw HTML rendering", () => {
    for (const f of sources) {
      const text = readFileSync(f, "utf8");
      expect(text, f).not.toMatch(/dangerouslySetInnerHTML|rehype-raw/);
    }
  });

  it("renders Markdown only through components/markdown.tsx", () => {
    const users = sources.filter((f) => readFileSync(f, "utf8").includes("react-markdown"));
    expect(users.map((f) => f.replace(/.*src[\\/]/, ""))).toEqual(["components/markdown.tsx"]);
  });
});
