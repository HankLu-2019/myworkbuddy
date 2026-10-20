import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, ApiError, setCsrfToken, type User } from "./api";

type Me = { user: User; csrf_token: string };

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  clear: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const accept = (me: Me) => {
    setCsrfToken(me.csrf_token);
    setUser(me.user);
  };

  useEffect(() => {
    api<Me>("/api/auth/me")
      .then(accept)
      .catch((e) => {
        if (!(e instanceof ApiError && e.status === 401)) console.error(e);
      })
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    accept(await api<Me>("/api/auth/login", { method: "POST", json: { username, password } }));
  }, []);

  const clear = useCallback(() => {
    setCsrfToken("");
    setUser(null);
  }, []);

  const logout = useCallback(async () => {
    await api("/api/auth/logout", { method: "POST" }).catch(() => undefined);
    clear();
  }, [clear]);

  return <AuthContext.Provider value={{ user, loading, login, logout, clear }}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
