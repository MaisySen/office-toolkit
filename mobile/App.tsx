import React, { useEffect, useState } from "react";
import {
  ActivityIndicator, FlatList, SafeAreaView, StyleSheet, Text, TouchableOpacity, View,
} from "react-native";
import Constants from "expo-constants";
import * as AuthSession from "expo-auth-session";
import { listOperations, Operation } from "./src/api";
import { useSignIn, signOut, isSignedIn } from "./src/auth";

const baseUrl: string =
  (Constants.expoConfig?.extra?.apiBaseUrl as string) || "http://localhost:5000";
const clientId = "office-toolkit-mobile";

export default function App() {
  const [signedIn, setSignedIn] = useState<boolean | null>(null);
  const [ops, setOps] = useState<Operation[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const redirectUri = AuthSession.makeRedirectUri({ scheme: "officetoolkit" });
  const { ready, signIn } = useSignIn({ baseUrl, clientId, redirectUri });

  useEffect(() => {
    isSignedIn().then(setSignedIn);
  }, []);

  useEffect(() => {
    if (signedIn) {
      listOperations(baseUrl).then(setOps).catch((e) => setError(e.message));
    }
  }, [signedIn]);

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
      {signedIn && (
        <>
          {error && <Text style={styles.error}>{error}</Text>}
          {!ops && !error && <ActivityIndicator style={{ marginTop: 40 }} />}
          {ops && (
            <FlatList
              data={ops}
              keyExtractor={(o) => o.slug}
              renderItem={({ item }) => (
                <TouchableOpacity style={styles.card}>
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
  card: {
    backgroundColor: "#fff", marginHorizontal: 16, marginTop: 10, padding: 16,
    borderRadius: 10, borderWidth: 1, borderColor: "#e2e5ea",
  },
  cardText: { fontSize: 16, color: "#1a1d21" },
  signOut: { color: "#2563eb", textAlign: "center", margin: 20, fontSize: 15 },
});
