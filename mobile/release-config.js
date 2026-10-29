// Release builds must talk to the real deployment. These checks run while
// the Expo config is evaluated, so a release build that would embed a
// loopback, emulator or plain http endpoint fails instead of shipping.
const LOOPBACK = ["localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "10.0.2.2"];

function endpointProblem(name, value) {
  let url;
  try {
    url = new URL(value);
  } catch (e) {
    return `${name} is not a valid URL`;
  }
  const host = url.hostname.toLowerCase();
  if (url.protocol !== "https:") {
    return `${name} must use https in a release build`;
  }
  if (LOOPBACK.includes(host) || host.startsWith("127.") || host.endsWith(".localhost")) {
    return `${name} points at a local or emulator host`;
  }
  return null;
}

function isReleaseBuild(env) {
  return env.TOOLKIT_RELEASE_BUILD === "1" || env.EAS_BUILD_PROFILE === "production";
}

function assertReleaseEndpoints(extra, scheme) {
  const problems = [
    endpointProblem("apiBaseUrl", extra.apiBaseUrl),
    endpointProblem("oidcIssuer", extra.oidcIssuer),
  ].filter(Boolean);
  // The app signs in with the scheme redirect officetoolkit://auth; a
  // release build must carry that scheme, never a dev server redirect.
  if (scheme !== "officetoolkit") {
    problems.push("the app scheme must be officetoolkit");
  }
  if (problems.length) {
    throw new Error(
      "Release build refused: " + problems.join("; ")
        + ". Set TOOLKIT_API_BASE_URL and TOOLKIT_OIDC_ISSUER to the production"
        + " https endpoints in the build environment."
    );
  }
}

module.exports = { assertReleaseEndpoints, endpointProblem, isReleaseBuild };
