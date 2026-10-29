import React from "react";
import renderer from "react-test-renderer";
import { Text, TouchableOpacity } from "react-native";
import App from "../App";
import { AuthError } from "../src/api";

const mockSignOut = jest.fn(async () => undefined);
const mockListOperations = jest.fn();
const mockRunOperation = jest.fn();

jest.mock("expo-auth-session", () => ({
  __esModule: true,
  makeRedirectUri: () => "officetoolkit://auth",
  useAuthRequest: () => [null, null, () => Promise.resolve()],
  exchangeCodeAsync: jest.fn(),
  refreshAsync: jest.fn(),
  revokeAsync: jest.fn(),
  DiscoveryDocument: {},
  ResponseType: { Code: "code" },
}));
jest.mock("expo-constants", () => ({
  __esModule: true,
  default: { expoConfig: { scheme: "officetoolkit", extra: {} } },
}));
jest.mock("expo-secure-store", () => ({
  getItemAsync: jest.fn(async () => "tok-123"),
  setItemAsync: jest.fn(async () => undefined),
  deleteItemAsync: jest.fn(async () => undefined),
}));
jest.mock("expo-document-picker", () => ({
  getDocumentAsync: jest.fn(async () => ({
    canceled: false,
    assets: [{ uri: "file:///tmp/in.pdf", name: "in.pdf", mimeType: "application/pdf" }],
  })),
}));
jest.mock("expo-file-system", () => ({
  documentDirectory: "file:///tmp/",
  EncodingType: { Base64: "base64" },
  writeAsStringAsync: jest.fn(async () => undefined),
}));
jest.mock("expo-sharing", () => ({
  isAvailableAsync: jest.fn(async () => false),
  shareAsync: jest.fn(async () => undefined),
}));
jest.mock("../src/auth", () => ({
  useSignIn: () => ({ ready: true, signIn: jest.fn(async () => undefined) }),
  signOut: (...args: unknown[]) => mockSignOut(...args),
  isSignedIn: jest.fn(async () => true),
}));
jest.mock("../src/api", () => {
  const actual = jest.requireActual("../src/api");
  return {
    ...actual,
    listOperations: (...args: unknown[]) => mockListOperations(...args),
    runOperation: (...args: unknown[]) => mockRunOperation(...args),
  };
});

function pressByText(root: renderer.ReactTestInstance, label: string) {
  const node = root
    .findAllByType(TouchableOpacity)
    .find((t) => t.findAllByType(Text).some((x) => x.props.children === label));
  if (!node) throw new Error(`no touchable for ${label}`);
  return renderer.act(async () => node.props.onPress());
}

beforeEach(() => {
  mockSignOut.mockClear();
  mockListOperations.mockReset();
  mockRunOperation.mockReset();
});

test("list 401 signs out and returns to the sign-in screen", async () => {
  mockListOperations.mockRejectedValue(new AuthError("Session expired"));
  let tree: renderer.ReactTestRenderer | undefined;
  await renderer.act(async () => {
    tree = renderer.create(<App />);
  });
  // effects settle across a second act
  await renderer.act(async () => {});
  const json = JSON.stringify(tree!.toJSON());
  expect(mockSignOut).toHaveBeenCalledTimes(1);
  expect(json).toContain("Sign in");
  expect(json).toContain("Your session expired. Sign in again.");
});

test("run 401 signs out and returns to the sign-in screen", async () => {
  mockListOperations.mockResolvedValue([{ slug: "to-jpeg", label: "PDF to JPEG" }]);
  mockRunOperation.mockRejectedValue(new AuthError("Session expired"));
  let tree: renderer.ReactTestRenderer | undefined;
  await renderer.act(async () => {
    tree = renderer.create(<App />);
  });
  await renderer.act(async () => {});
  await pressByText(tree!.root, "PDF to JPEG");
  await pressByText(tree!.root, "Choose file and run");
  await renderer.act(async () => {});
  const json = JSON.stringify(tree!.toJSON());
  expect(mockSignOut).toHaveBeenCalledTimes(1);
  expect(json).toContain("Sign in");
  expect(json).toContain("Your session expired. Sign in again.");
});
