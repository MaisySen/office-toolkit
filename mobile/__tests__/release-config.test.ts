const { assertReleaseEndpoints, endpointProblem, isReleaseBuild } = require("../release-config");

const PROD = {
  apiBaseUrl: "https://doctool.multies.si",
  oidcIssuer: "https://doctool.multies.si/auth/realms/office-toolkit",
};

function loadConfig(env: Record<string, string | undefined>) {
  const saved = { ...process.env };
  Object.assign(process.env, env);
  try {
    jest.resetModules();
    return require("../app.config.js")();
  } finally {
    for (const key of Object.keys(env)) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
  }
}

describe("release endpoint assertions", () => {
  it("accepts https production endpoints", () => {
    expect(() => assertReleaseEndpoints(PROD, "officetoolkit")).not.toThrow();
  });

  it.each([
    ["http://localhost:5000"],
    ["https://localhost:5000"],
    ["http://10.0.2.2:5000"],
    ["https://127.0.0.1"],
    ["http://doctool.multies.si"],
    ["not a url"],
  ])("rejects %s as the API base", (bad) => {
    expect(endpointProblem("apiBaseUrl", bad)).not.toBeNull();
    expect(() => assertReleaseEndpoints({ ...PROD, apiBaseUrl: bad }, "officetoolkit"))
      .toThrow(/Release build refused/);
  });

  it("rejects a local issuer and a foreign scheme", () => {
    expect(() => assertReleaseEndpoints(
      { ...PROD, oidcIssuer: "http://localhost:8080/realms/office-toolkit" }, "officetoolkit"))
      .toThrow(/oidcIssuer/);
    expect(() => assertReleaseEndpoints(PROD, "exp")).toThrow(/scheme/);
  });

  it("only applies to release builds", () => {
    expect(isReleaseBuild({})).toBe(false);
    expect(isReleaseBuild({ TOOLKIT_RELEASE_BUILD: "1" })).toBe(true);
    expect(isReleaseBuild({ EAS_BUILD_PROFILE: "production" })).toBe(true);
    expect(isReleaseBuild({ EAS_BUILD_PROFILE: "preview" })).toBe(false);
  });
});

describe("app.config.js", () => {
  it("keeps the local defaults for development builds", () => {
    const config = loadConfig({ TOOLKIT_RELEASE_BUILD: undefined });
    expect(config.extra.apiBaseUrl).toBe("http://localhost:5000");
  });

  it("refuses a release build that would embed the local defaults", () => {
    expect(() => loadConfig({
      TOOLKIT_RELEASE_BUILD: "1",
      TOOLKIT_API_BASE_URL: undefined,
      TOOLKIT_OIDC_ISSUER: undefined,
    })).toThrow(/Release build refused/);
  });

  it("builds a release config from production endpoints", () => {
    const config = loadConfig({
      TOOLKIT_RELEASE_BUILD: "1",
      TOOLKIT_API_BASE_URL: PROD.apiBaseUrl,
      TOOLKIT_OIDC_ISSUER: PROD.oidcIssuer,
    });
    expect(config.extra).toMatchObject(PROD);
  });
});
