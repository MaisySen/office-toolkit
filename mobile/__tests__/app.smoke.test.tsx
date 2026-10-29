import React from "react";
import renderer from "react-test-renderer";
import App from "../App";

jest.mock("expo-auth-session", () => ({
  __esModule: true,
  makeRedirectUri: () => "officetoolkit://redirect",
  useAuthRequest: () => [null, null, () => Promise.resolve()],
  exchangeCodeAsync: jest.fn(),
  refreshAsync: jest.fn(),
  revokeAsync: jest.fn(),
  DiscoveryDocument: {},
  ResponseType: { Code: "code" },
}));
jest.mock("expo-constants", () => ({
  __esModule: true,
  default: { expoConfig: { scheme: "officetoolkit", name: "Office Toolkit", slug: "office-toolkit" }, manifest: { scheme: "officetoolkit" } },
}));
jest.mock("expo-secure-store", () => ({
  getItemAsync: jest.fn(async () => null),
  setItemAsync: jest.fn(async () => undefined),
  deleteItemAsync: jest.fn(async () => undefined),
}));

test("App renders the sign-in gate without crashing", async () => {
  let tree: renderer.ReactTestRenderer | undefined;
  await renderer.act(async () => {
    tree = renderer.create(<App />);
  });
  const json = JSON.stringify(tree!.toJSON());
  expect(json).toContain("Sign in");
});
