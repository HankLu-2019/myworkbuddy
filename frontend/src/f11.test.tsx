import { afterEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import LoginPage, { SSO_ERRORS } from "./features/auth/LoginPage";
import { mockApi, renderAt } from "./test-utils";

afterEach(() => vi.unstubAllGlobals());

const signedOut = { "GET /api/auth/me": new Response(JSON.stringify({ detail: "Not signed in", code: "not_signed_in" }), { status: 401 }) };

describe("Single sign-on (08 §F11)", () => {
  it("offers the provider first and keeps the password form", async () => {
    mockApi({ ...signedOut, "GET /api/auth/oidc/config": { enabled: true, name: "Company SSO", local_login: "admins" } });
    renderAt("/login?next=/work/1", <LoginPage />);
    const link = await screen.findByRole("link", { name: "Sign in with Company SSO" });
    expect(link).toHaveAttribute("href", "/api/auth/oidc/start?next=%2Fwork%2F1");
    expect(screen.getByText("Admins can also sign in with a password:")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
  });

  it("explains why the provider sent the user back", async () => {
    mockApi({ ...signedOut, "GET /api/auth/oidc/config": { enabled: true, name: "SSO", local_login: "all" } });
    renderAt("/login?sso_error=no_account", <LoginPage />);
    expect(await screen.findByText(SSO_ERRORS.no_account)).toBeInTheDocument();
  });

  it("shows no SSO button when it isn't set up", async () => {
    mockApi({ ...signedOut, "GET /api/auth/oidc/config": { enabled: false, name: "SSO", local_login: "all" } });
    renderAt("/login", <LoginPage />);
    expect(await screen.findByLabelText("Username")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Sign in with/ })).toBeNull();
  });
});
