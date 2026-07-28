import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { api, getToken, setToken, Me } from "./api";

interface AuthState {
  user: Me | null;
  loading: boolean;
  login: (u: string, p: string) => Promise<void>;
  logout: () => void;
  can: (perm: string) => boolean;
}

const AuthContext = createContext<AuthState>(null as any);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!getToken()) {
      setLoading(false);
      return;
    }
    api
      .me()
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setLoading(false));
  }, []);

  async function login(username: string, password: string) {
    await api.login(username, password);
    setUser(await api.me());
  }
  function logout() {
    setToken(null);
    setUser(null);
  }
  function can(perm: string) {
    return !!user?.permissions.includes(perm);
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, can }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
