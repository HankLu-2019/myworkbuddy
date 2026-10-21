import { Navigate, useParams } from "react-router-dom";
import { usePageTitle } from "../../app/pageTitle";
import { LinkTabs, PageHeader } from "../../components/ui";
import DocumentsTab from "./DocumentsTab";
import ImportsTab from "./ImportsTab";
import ReposTab from "./ReposTab";

const TABS = {
  repos: { label: "Repositories", element: <ReposTab /> },
  documents: { label: "Documents", element: <DocumentsTab /> },
  imports: { label: "Claude Code sessions", element: <ImportsTab /> },
} as const;

/** One page for everything that feeds the knowledge base (07 §4.6). */
export default function SourcesPage() {
  const { tab = "repos" } = useParams();
  const current = TABS[tab as keyof typeof TABS];
  usePageTitle({ label: "Sources", to: "/sources" }, current?.label);
  if (!current) return <Navigate to="/sources/repos" replace />;
  return (
    <div className="page">
      <PageHeader title="Sources" />
      <LinkTabs
        label="Sources"
        tabs={Object.entries(TABS).map(([key, t]) => ({ to: `/sources/${key}`, label: t.label }))}
      />
      {current.element}
    </div>
  );
}
