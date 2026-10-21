import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import {
  Button,
  ConfirmDialog,
  DataTable,
  IconButton,
  LinkTabs,
  StatusPill,
  TextField,
  ToastProvider,
  VisibilityBadge,
  VisibilityControl,
  statusLabel,
  useToast,
  visualState,
} from "./ui";
import GalleryPage from "../features/dev/GalleryPage";
import { ADMIN, MEMBER, mockApi, renderAt, signedInAs } from "../test-utils";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("Button", () => {
  it("defaults to type=button and shows pending as busy and disabled", () => {
    const onClick = vi.fn();
    render(
      <form onSubmit={() => onClick("submitted")}>
        <Button onClick={() => onClick("clicked")}>Plain</Button>
        <Button pending>Saving</Button>
      </form>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Plain" }));
    expect(onClick).toHaveBeenCalledWith("clicked");
    expect(onClick).not.toHaveBeenCalledWith("submitted");
    const busy = screen.getByRole("button", { name: "Saving" });
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute("aria-busy", "true");
  });

  it("gives icon buttons an accessible name", () => {
    render(<IconButton label="More actions" icon={<span />} />);
    expect(screen.getByRole("button", { name: "More actions" })).toBeInTheDocument();
  });
});

describe("TextField", () => {
  it("links the label, hint and error", () => {
    render(<TextField label="Branch" hint="Defaults to main" error="Not a branch" />);
    const input = screen.getByLabelText("Branch");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription("Defaults to main Not a branch");
  });

  it("keeps a caller's aria-describedby alongside the hint", () => {
    render(
      <>
        <p id="extra">Extra help</p>
        <TextField label="Path" hint="Folders are created" aria-describedby="extra" />
      </>,
    );
    expect(screen.getByLabelText("Path")).toHaveAccessibleDescription("Folders are created Extra help");
  });
});

describe("StatusPill", () => {
  it("maps every status to one of six states and always shows words", () => {
    expect(visualState("queued")).toBe("waiting");
    expect(visualState("running")).toBe("working");
    expect(visualState("needs_decision")).toBe("needs");
    expect(visualState("ready")).toBe("done");
    expect(visualState("failed")).toBe("problem");
    expect(visualState("cancelled")).toBe("off");
    expect(visualState("something_new")).toBe("waiting");
    expect(statusLabel("budget_exceeded")).toBe("Budget reached");
    expect(statusLabel("some_new_state")).toBe("Some new state");
    render(<StatusPill status="failed" />);
    const pill = screen.getByText("Failed").closest("[data-status]");
    expect(pill).toHaveAttribute("data-state", "problem");
    expect(pill).toHaveAttribute("data-status", "failed");
  });
});

describe("visibility", () => {
  it("names the state in words and flips it", () => {
    function Harness() {
      const [v, setV] = useState<"private" | "public">("private");
      return (
        <>
          <VisibilityBadge visibility={v} />
          <VisibilityControl visibility={v} onChange={setV} shareLabel="Share code index" />
        </>
      );
    }
    render(<Harness />);
    expect(screen.getByText("Private")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Share code index" }));
    expect(screen.getByText("Team")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Make private" })).toBeInTheDocument();
  });
});

describe("DataTable", () => {
  const rows = [
    { id: 1, name: "b", n: 2 },
    { id: 2, name: "a", n: 10 },
  ];
  const columns = [
    { key: "name", header: "Name", cell: (r: (typeof rows)[0]) => r.name, sortValue: (r: (typeof rows)[0]) => r.name },
    { key: "n", header: "Count", numeric: true, cell: (r: (typeof rows)[0]) => r.n, sortValue: (r: (typeof rows)[0]) => r.n },
  ];

  it("sorts by a column and marks the direction", () => {
    render(<DataTable label="Things" rows={rows} columns={columns} rowKey={(r) => r.id} rowLabel={(r) => r.name} />);
    const table = screen.getByRole("table", { name: "Things" });
    const names = () => within(table).getAllByRole("row").slice(1).map((r) => r.getAttribute("aria-label"));
    expect(names()).toEqual(["b", "a"]);
    fireEvent.click(screen.getByRole("button", { name: "Name" }));
    expect(names()).toEqual(["a", "b"]);
    expect(screen.getByRole("columnheader", { name: "Name" })).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(screen.getByRole("button", { name: "Name" }));
    expect(names()).toEqual(["b", "a"]);
    fireEvent.click(screen.getByRole("button", { name: "Count" }));
    expect(names()).toEqual(["b", "a"]); // numbers, not strings
  });

  it("shows the empty state and Load more", () => {
    const more = vi.fn();
    const { rerender } = render(
      <DataTable label="Things" rows={[]} columns={columns} rowKey={(r) => r.id} empty={<p>Nothing yet</p>} />,
    );
    expect(screen.getByText("Nothing yet")).toBeInTheDocument();
    rerender(<DataTable label="Things" rows={rows} columns={columns} rowKey={(r) => r.id} onLoadMore={more} />);
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(more).toHaveBeenCalled();
  });
});

describe("ConfirmDialog", () => {
  it("names the action and confirms only when allowed", () => {
    const onConfirm = vi.fn();
    const onOpenChange = vi.fn();
    const { rerender } = render(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="Remove etl?"
        body="Deletes its code index."
        confirmLabel="Remove repository"
        danger
        disabled
        onConfirm={onConfirm}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: "Remove etl?" });
    expect(dialog).toHaveAccessibleDescription("Deletes its code index.");
    expect(within(dialog).getByRole("button", { name: "Remove repository" })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    rerender(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="Remove etl?"
        body="Deletes its code index."
        confirmLabel="Remove repository"
        onConfirm={onConfirm}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Remove repository" }));
    expect(onConfirm).toHaveBeenCalled();
  });
});

describe("Toast", () => {
  it("shows a message with an optional action", async () => {
    const undo = vi.fn();
    function Harness() {
      const toast = useToast();
      return <Button onClick={() => toast({ title: "Deleted", action: { label: "Undo", onClick: undo } })}>Go</Button>;
    }
    render(
      <ToastProvider>
        <Harness />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Go" }));
    expect(await screen.findByText("Deleted")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    expect(undo).toHaveBeenCalled();
  });
});

describe("LinkTabs", () => {
  it("marks the current route", () => {
    render(
      <MemoryRouter initialEntries={["/sources/documents"]}>
        <LinkTabs
          label="Sources"
          tabs={[
            { to: "/sources/repos", label: "Repositories" },
            { to: "/sources/documents", label: "Documents" },
          ]}
        />
      </MemoryRouter>,
    );
    const nav = screen.getByRole("navigation", { name: "Sources" });
    expect(within(nav).getByRole("link", { name: "Documents" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Repositories" })).not.toHaveAttribute("aria-current");
  });
});

describe("component gallery", () => {
  it("renders every component for admins", async () => {
    mockApi(signedInAs(ADMIN));
    renderAt("/dev/components", <GalleryPage />);
    expect(await screen.findByRole("heading", { name: "Components", level: 1 })).toBeInTheDocument();
    const states = [...document.querySelectorAll(".pill[data-state]")].map((e) => e.getAttribute("data-state"));
    expect(new Set(states)).toEqual(new Set(["waiting", "working", "needs", "done", "problem", "off"]));
    fireEvent.click(screen.getByRole("button", { name: "Open confirmation" }));
    expect(await screen.findByRole("dialog", { name: "Delete payments-api?" })).toBeInTheDocument();
  });

  it("is hidden from members in production builds", async () => {
    vi.stubEnv("DEV", false);
    mockApi(signedInAs(MEMBER));
    renderAt("/dev/components", <GalleryPage />);
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Components" })).not.toBeInTheDocument());
  });
});
