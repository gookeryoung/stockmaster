/** Session state.

Kept deliberately small: `config` decides whether the gate is on, `user` is the
authenticated account (null when anonymous *or* when the gate is off). The token
itself lives in the API client, which also notifies us when the server rejects it.
 */

import { create } from "zustand";
import { api, authEvents, getAuthToken, setAuthToken } from "./api";
import type { AuthConfig, AuthUser } from "./types";

interface AuthState {
  ready: boolean;
  config: AuthConfig | null;
  user: AuthUser | null;
  init: () => Promise<void>;
  refreshUser: () => Promise<void>;
  applyToken: (token: string, user: AuthUser) => void;
  login: (username: string, password: string) => Promise<void>;
  register: (payload: { username: string; password: string; email?: string; nickname?: string }) => Promise<void>;
  logout: () => Promise<void>;
  setUser: (user: AuthUser) => void;
}

let initPromise: Promise<void> | null = null;

export const useAuth = create<AuthState>((set) => ({
  ready: false,
  config: null,
  user: null,

  init: async () => {
    // React StrictMode mounts effects twice; share one bootstrap request.
    if (initPromise) return initPromise;
    initPromise = (async () => {
      let config: AuthConfig | null = null;
      try {
        config = await api.authConfig();
      } catch {
        config = null;
      }

      let user: AuthUser | null = null;
      if (getAuthToken()) {
        try {
          user = await api.me();
        } catch {
          setAuthToken(null);
        }
      }
      set({ config, user, ready: true });
    })();
    return initPromise;
  },

  refreshUser: async () => {
    if (!getAuthToken()) {
      set({ user: null });
      return;
    }
    try {
      set({ user: await api.me() });
    } catch {
      set({ user: null });
    }
  },

  applyToken: (token, user) => {
    setAuthToken(token);
    set({ user });
  },

  login: async (username, password) => {
    const result = await api.login(username, password);
    setAuthToken(result.token);
    set({ user: result.user });
  },

  register: async (payload) => {
    const result = await api.register(payload);
    setAuthToken(result.token);
    set({ user: result.user });
  },

  logout: async () => {
    try {
      await api.logout();
    } catch {
      // Token may already be invalid; dropping it locally is what matters.
    }
    setAuthToken(null);
    set({ user: null });
  },

  setUser: (user) => set({ user }),
}));

// A rejected token (expired, revoked, account disabled) must not leave the UI in
// a half-logged-in state where every request fails silently.
authEvents.addEventListener("unauthorized", () => {
  useAuth.setState({ user: null });
});

/** Whether account management is available: real admin, or the gate is off. */
export function canManageUsers(config: AuthConfig | null, user: AuthUser | null): boolean {
  if (user) return user.role === "admin";
  return Boolean(config && !config.auth_enabled);
}

/** Whether the SPA must force a login before rendering the workbench. */
export function requiresLogin(config: AuthConfig | null, user: AuthUser | null): boolean {
  return Boolean(config?.auth_enabled && !user);
}
