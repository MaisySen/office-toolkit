// OAuth2 authorization-code + PKCE sign-in against the toolkit backend.
// Tokens live in expo-secure-store (Keychain / Keystore), never in AsyncStorage.

import * as AuthSession from "expo-auth-session";
import { getToken, setToken } from "./api";

export type AuthConfig = {
  baseUrl: string;
  clientId: string;
  redirectUri: string;
};

export function endpoints(baseUrl: string) {
  return {
    authorizationEndpoint: `${baseUrl}/oauth/authorize`,
    tokenEndpoint: `${baseUrl}/oauth/token`,
  };
}

export function useSignIn(config: AuthConfig) {
  const [request, response, promptAsync] = AuthSession.useAuthRequest(
    {
      clientId: config.clientId,
      redirectUri: config.redirectUri,
      usePKCE: true,
      scopes: [],
    },
    endpoints(config.baseUrl)
  );

  const signIn = async (): Promise<void> => {
    const result = await promptAsync();
    if (result.type !== "success" || !result.params.code) {
      throw new Error(result.type === "error"
        ? (result.params.error_description || "Sign in failed")
        : "Sign in cancelled");
    }
    const token = await AuthSession.exchangeCodeAsync(
      {
        clientId: config.clientId,
        code: result.params.code,
        redirectUri: config.redirectUri,
        extraParams: { code_verifier: request?.codeVerifier || "" },
      },
      endpoints(config.baseUrl)
    );
    await setToken(token.accessToken);
  };

  return { ready: !!request, signIn, response };
}

export async function signOut(): Promise<void> {
  await setToken(null);
}

export async function isSignedIn(): Promise<boolean> {
  return (await getToken()) !== null;
}
