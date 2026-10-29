import React, { useEffect, useState } from "react";
import {
  ActivityIndicator, FlatList, SafeAreaView, StyleSheet, Text, TouchableOpacity, View,
} from "react-native";
import Constants from "expo-constants";
import * as AuthSession from "expo-auth-session";
import * as DocumentPicker from "expo-document-picker";
import * as FileSystem from "expo-file-system";
import * as Sharing from "expo-sharing";
import { listOperations, runOperation, Operation } from "./src/api";
import { useSignIn, signOut, isSignedIn } from "./src/auth";

const baseUrl: string =
  (Constants.expoConfig?.extra?.apiBaseUrl as string) || "http://localhost:5000";
const issuer: string =
  (Constants.expoConfig?.extra?.oidcIssuer as string) || "";
const clientId: string =
  (Constants.expoConfig?.extra?.oidcClientId as string) || "office-toolkit-mobile";

export default function App() {
  const [signedIn, setSignedIn] = useState<boolean | null>(null);
  const [ops, setOps] = useState<Operation[] | null>(null);
  const [active, setActive] = useState<Operation | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const redirectUri = AuthSession.makeRedirectUri({ scheme: "officetoolkit" });
  const { ready, signIn } = useSignIn({ issuer, clientId, redirectUri });

  useEffect(() => {
    isSignedIn().then(setSignedIn);
  }, []);

  useEffect(() => {
    if (signedIn) {
      listOperations(baseUrl).then(setOps).catch((e) => setError(e.message));
    }
  }, [signedIn]);

  const runActive = async () => {
    if (!active) return;
    setError(null);
    setResult(null);
    const picked = await DocumentPicker.getDocumentAsync({ copyToCacheDirectory: true });
    if (picked.canceled) return;
    const asset = picked.assets[0];
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", {
        uri: asset.uri,
        name: asset.name || "input",
        type: asset.mimeType || "application/octet-stream",
      } as unknown as Blob);
      const res = await runOperation(baseUrl, active.slug, form);
      const disposition = res.headers.get("content-disposition") || "";
      const match = /filename="?([^";]+)"?/.exec(disposition);
      const filename = match?.[1] || `${active.slug}-result`;
      const blob = await res.blob();
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as string);
        reader.onerror = () => reject(new Error("Could not read the result."));
        reader.readAsDataURL(blob);
      });
      const base64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
      const out = `${FileSystem.documentDirectory}${filename}`;
      await FileSystem.writeAsStringAsync(out, base64, {
        encoding: FileSystem.EncodingType.Base64,
      });
      if (await Sharing.isAvailableAsync()) {
        await Sharing.shareAsync(out);
      }
      setResult(`Done: ${filename}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <SafeAreaView style={styles.root}>
      <View style={styles.header}>
        <Text style={styles.title}>Office Toolkit</Text>
        <Text style={styles.sub}>Files are processed on your server, not here.</Text>
      </View>
      {signedIn === null && <ActivityIndicator style={{ marginTop: 40 }} />}
      {signedIn === false && (
        <View style={styles.center}>
          <Text style={styles.note}>Sign in with your toolkit account.</Text>
          <TouchableOpacity
            style={[styles.button, !ready && { opacity: 0.5 }]}
            disabled={!ready}
            onPress={() => signIn().then(() => setSignedIn(true)).catch((e) => setError(e.message))}
          >
            <Text style={styles.buttonText}>Sign in</Text>
          </TouchableOpacity>
          {error && <Text style={styles.error}>{error}</Text>}
        </View>
      )}
      {signedIn && !active && (
        <>
          {error && <Text style={styles.error}>{error}</Text>}
          {!ops && !error && <ActivityIndicator style={{ marginTop: 40 }} />}
          {ops && (
            <FlatList
              data={ops}
              keyExtractor={(o) => o.slug}
              renderItem={({ item }) => (
                <TouchableOpacity style={styles.card} onPress={() => { setActive(item); setResult(null); setError(null); }}>
                  <Text style={styles.cardText}>{item.label}</Text>
                </TouchableOpacity>
              )}
            />
          )}
          <TouchableOpacity onPress={() => signOut().then(() => setSignedIn(false))}>
            <Text style={styles.signOut}>Sign out</Text>
          </TouchableOpacity>
        </>
      )}
      {signedIn && active && (
        <View style={styles.center}>
          <Text style={styles.note}>{active.label}</Text>
          {busy && <ActivityIndicator style={{ marginBottom: 16 }} />}
          {result && <Text style={styles.resultText}>{result}</Text>}
          {error && <Text style={styles.error}>{error}</Text>}
          <TouchableOpacity style={styles.button} disabled={busy} onPress={runActive}>
            <Text style={styles.buttonText}>Choose file and run</Text>
          </TouchableOpacity>
          <TouchableOpacity onPress={() => setActive(null)}>
            <Text style={styles.signOut}>Back</Text>
          </TouchableOpacity>
        </View>
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: "#f5f6f8" },
  header: { backgroundColor: "#1f2937", padding: 18 },
  title: { color: "#fff", fontSize: 20, fontWeight: "600" },
  sub: { color: "#cbd5e1", fontSize: 13, marginTop: 4 },
  center: { alignItems: "center", marginTop: 60, paddingHorizontal: 32 },
  note: { fontSize: 15, color: "#374151", marginBottom: 16 },
  button: {
    backgroundColor: "#2563eb", paddingVertical: 12, paddingHorizontal: 40,
    borderRadius: 8,
  },
  buttonText: { color: "#fff", fontSize: 16, fontWeight: "600" },
  error: { color: "#991b1b", margin: 16 },
  resultText: { color: "#166534", fontSize: 15, marginBottom: 16 },
  card: {
    backgroundColor: "#fff", marginHorizontal: 16, marginTop: 10, padding: 16,
    borderRadius: 10, borderWidth: 1, borderColor: "#e2e5ea",
  },
  cardText: { fontSize: 16, color: "#1a1d21" },
  signOut: { color: "#2563eb", textAlign: "center", margin: 20, fontSize: 15 },
});
