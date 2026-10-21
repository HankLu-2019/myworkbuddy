import { LinkTabs } from "../../components/ui";

/** Admin has two pages (07 §4 Admin); the tabs are links so each has its own URL. */
export function AdminTabs() {
  return (
    <LinkTabs
      label="Admin"
      tabs={[
        { to: "/admin", label: "Users" },
        { to: "/admin/system", label: "System" },
      ]}
    />
  );
}
