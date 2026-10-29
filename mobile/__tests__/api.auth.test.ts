import { listOperations, runOperation, AuthError } from "../src/api";

jest.mock("expo-secure-store", () => ({
  getItemAsync: jest.fn(async () => "tok-123"),
  setItemAsync: jest.fn(async () => undefined),
  deleteItemAsync: jest.fn(async () => undefined),
}));

const mockResponse = (status: number, body: unknown = {}) =>
  ({
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  } as Response);

describe("api auth failure mapping", () => {
  afterEach(() => jest.restoreAllMocks());

  test("listOperations maps 401 to AuthError", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue(mockResponse(401));
    await expect(listOperations("http://srv")).rejects.toBeInstanceOf(AuthError);
  });

  test("runOperation maps 401 to AuthError", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue(mockResponse(401));
    await expect(
      runOperation("http://srv", "to-jpeg", new FormData())
    ).rejects.toBeInstanceOf(AuthError);
  });

  test("listOperations keeps other failures generic", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue(mockResponse(500));
    const err = await listOperations("http://srv").catch((e) => e);
    expect(err).toBeInstanceOf(Error);
    expect(err).not.toBeInstanceOf(AuthError);
  });

  test("listOperations returns operations on success", async () => {
    jest.spyOn(global, "fetch").mockResolvedValue(
      mockResponse(200, { operations: [{ slug: "to-jpeg", label: "PDF to JPEG" }] })
    );
    await expect(listOperations("http://srv")).resolves.toEqual([
      { slug: "to-jpeg", label: "PDF to JPEG" },
    ]);
  });
});
