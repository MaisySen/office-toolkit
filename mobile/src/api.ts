// Client for the office toolkit backend JSON API.
// The base URL comes from app.json extra.apiBaseUrl (overridable per build via app.config.js env); auth token support is
// wired but empty until the auth stack decision lands (OAuth2 + PKCE).

import * as SecureStore from "expo-secure-store";

export type Operation = { slug: string; label: string };

const TOKEN_KEY = "access_token";

export async function getToken(): Promise<string | null> {
  try {
    return await SecureStore.getItemAsync(TOKEN_KEY);
  } catch {
    return null;
  }
}

export async function setToken(token: string | null): Promise<void> {
  if (token) await SecureStore.setItemAsync(TOKEN_KEY, token);
  else await SecureStore.deleteItemAsync(TOKEN_KEY);
}

async function headers(): Promise<Record<string, string>> {
  const token = await getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// Thrown when the stored token is rejected (expired or revoked), so the
// app can drop back to the sign-in screen instead of failing generically.
export class AuthError extends Error {}

export async function listOperations(baseUrl: string): Promise<Operation[]> {
  const res = await fetch(`${baseUrl}/api/operations`, { headers: await headers() });
  if (res.status === 401) throw new AuthError("Session expired");
  if (!res.ok) throw new Error(`Server answered ${res.status}`);
  const data = await res.json();
  return data.operations as Operation[];
}

export type RunResult =
  | { ok: true; filename: string; bytes: string }
  | { ok: false; error: string };

export async function runOperation(
  baseUrl: string,
  slug: string,
  form: FormData
): Promise<Response> {
  const res = await fetch(`${baseUrl}/api/run/${slug}`, {
    method: "POST",
    headers: await headers(),
    body: form,
  });
  if (res.status === 401) throw new AuthError("Session expired");
  if (!res.ok) {
    let message = `Server answered ${res.status}`;
    try {
      message = (await res.json()).error || message;
    } catch {}
    throw new Error(message);
  }
  return res;
}
