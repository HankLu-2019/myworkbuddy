import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

export type Crumb = { label: string; to?: string };

const TitleContext = createContext<{ crumbs: Crumb[]; setCrumbs: (c: Crumb[]) => void }>({
  crumbs: [],
  setCrumbs: () => undefined,
});

export function TitleProvider({ children }: { children: ReactNode }) {
  const [crumbs, setCrumbs] = useState<Crumb[]>([]);
  return <TitleContext.Provider value={{ crumbs, setCrumbs }}>{children}</TitleContext.Provider>;
}

export function useCrumbs(): Crumb[] {
  return useContext(TitleContext).crumbs;
}

/** Sets the top bar breadcrumb and the document title, e.g. usePageTitle({label: "Wiki", to: "/wiki"}, {label: title}). */
export function usePageTitle(...crumbs: (Crumb | string | undefined | null | false)[]) {
  const { setCrumbs } = useContext(TitleContext);
  const list = crumbs.filter(Boolean).map((c) => (typeof c === "string" ? { label: c } : c)) as Crumb[];
  const key = JSON.stringify(list);
  useEffect(() => {
    setCrumbs(list);
    document.title = list.length ? `${[...list].reverse().map((c) => c.label).join(" · ")} · MyWorkBuddy` : "MyWorkBuddy";
  }, [key]);
}
