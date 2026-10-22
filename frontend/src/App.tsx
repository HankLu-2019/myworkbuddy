import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import Shell from "./app/Shell";
import { SkeletonRows } from "./components/ui";
import LoginPage from "./features/auth/LoginPage";

// Route-level code splitting (07 §8): each feature is its own chunk, loaded on first visit.
const HomePage = lazy(() => import("./features/home/HomePage"));
const RepoDetailPage = lazy(() => import("./features/sources/RepoDetailPage"));
const ChatPage = lazy(() => import("./features/chat/ChatPage"));
const SearchPage = lazy(() => import("./features/search/SearchPage"));
const WikiPage = lazy(() => import("./features/wiki/WikiPage"));
const SourcesPage = lazy(() => import("./features/sources/SourcesPage"));
const SettingsPage = lazy(() => import("./features/settings/SettingsPage"));
const UsersPage = lazy(() => import("./features/admin/UsersPage"));
const IntegrationsAdminPage = lazy(() => import("./features/admin/IntegrationsAdminPage"));
const SystemPage = lazy(() => import("./features/admin/SystemPage"));
const GalleryPage = lazy(() => import("./features/dev/GalleryPage"));
const InboxPage = lazy(() => import("./features/inbox/InboxPage"));
const SchedulesPage = lazy(() => import("./features/schedules/SchedulesPage"));
const ScheduleEditPage = lazy(() => import("./features/schedules/ScheduleEditPage"));
const ScheduleDetailPage = lazy(() => import("./features/schedules/ScheduleDetailPage"));
const RunPage = lazy(() => import("./features/schedules/RunPage"));
const BoardPage = lazy(() => import("./features/work/BoardPage"));
const WorkItemPage = lazy(() => import("./features/work/WorkItemPage"));

function Page({ children }: { children: ReactNode }) {
  return (
    <Suspense
      fallback={
        <div className="page">
          <SkeletonRows />
        </div>
      }
    >
      {children}
    </Suspense>
  );
}

export default function App() {
  const { loading } = useAuth();
  if (loading) return <div className="boot">Loading…</div>;
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<Shell />}>
        <Route path="/" element={<Page><HomePage /></Page>} />
        <Route path="/inbox" element={<Page><InboxPage /></Page>} />
        <Route path="/schedules" element={<Page><SchedulesPage /></Page>} />
        <Route path="/schedules/new" element={<Page><ScheduleEditPage /></Page>} />
        <Route path="/schedules/:scheduleId" element={<Page><ScheduleDetailPage /></Page>} />
        <Route path="/schedules/:scheduleId/edit" element={<Page><ScheduleEditPage /></Page>} />
        <Route path="/runs/:runId" element={<Page><RunPage /></Page>} />
        <Route path="/work" element={<Page><BoardPage /></Page>} />
        <Route path="/work/:workId" element={<Page><WorkItemPage /></Page>} />
        <Route path="/work/:workId/plan" element={<Page><WorkItemPage tab="plan" /></Page>} />
        <Route path="/plans/:planId" element={<Page><WorkItemPage tab="plan" /></Page>} />
        <Route path="/chat" element={<Page><ChatPage /></Page>} />
        <Route path="/chat/:sessionId" element={<Page><ChatPage /></Page>} />
        <Route path="/search" element={<Page><SearchPage /></Page>} />
        <Route path="/wiki" element={<Page><WikiPage /></Page>} />
        <Route path="/wiki/changes" element={<Page><WikiPage /></Page>} />
        <Route path="/wiki/:pageId" element={<Page><WikiPage /></Page>} />
        <Route path="/sources" element={<Navigate to="/sources/repos" replace />} />
        <Route path="/sources/repos/:repoId" element={<Page><RepoDetailPage /></Page>} />
        <Route path="/sources/:tab" element={<Page><SourcesPage /></Page>} />
        {/* Old routes (07 §3.1): Projects, Documents and Imports are tabs of Sources now. */}
        <Route path="/projects" element={<Navigate to="/sources/repos" replace />} />
        <Route path="/documents" element={<Navigate to="/sources/documents" replace />} />
        <Route path="/imports" element={<Navigate to="/sources/imports" replace />} />
        <Route path="/settings" element={<Navigate to="/settings/profile" replace />} />
        <Route path="/settings/:tab" element={<Page><SettingsPage /></Page>} />
        <Route path="/admin" element={<Page><UsersPage /></Page>} />
        <Route path="/admin/integrations" element={<Page><IntegrationsAdminPage /></Page>} />
        <Route path="/admin/system" element={<Page><SystemPage /></Page>} />
        {/* Component gallery: in dev, and for admins in production so the visual baselines run on the real build. */}
        <Route path="/dev/components" element={<Page><GalleryPage /></Page>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
