import type { ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";
import * as RadixTabs from "@radix-ui/react-tabs";

/** In-page tabs (arrow keys move between them). */
export function Tabs({
  value,
  onValueChange,
  label,
  tabs,
}: {
  value: string;
  onValueChange: (v: string) => void;
  label: string;
  tabs: { value: string; label: ReactNode; content: ReactNode }[];
}) {
  return (
    <RadixTabs.Root value={value} onValueChange={onValueChange}>
      <RadixTabs.List className="tabs-list" aria-label={label}>
        {tabs.map((t) => (
          <RadixTabs.Trigger key={t.value} value={t.value} className="tab">
            {t.label}
          </RadixTabs.Trigger>
        ))}
      </RadixTabs.List>
      {tabs.map((t) => (
        <RadixTabs.Content key={t.value} value={t.value}>
          {t.content}
        </RadixTabs.Content>
      ))}
    </RadixTabs.Root>
  );
}

/** Tabs that are routes (each tab has its own URL, 07 §3.2): a nav of links, the active one marked current. */
export function LinkTabs({
  label,
  tabs,
}: {
  label: string;
  /** `active` overrides route matching, for tabs that differ only in the query string (filters). */
  tabs: { to: string; label: ReactNode; active?: boolean }[];
}) {
  return (
    <nav className="tabs-list" aria-label={label}>
      {tabs.map((t) =>
        t.active === undefined ? (
          <NavLink key={t.to} to={t.to} className="tab" end>
            {t.label}
          </NavLink>
        ) : (
          <Link key={t.to} to={t.to} className={`tab${t.active ? " active" : ""}`} aria-current={t.active ? "page" : undefined}>
            {t.label}
          </Link>
        ),
      )}
    </nav>
  );
}
