const appJson = require("./app.json");

// Static config lives in app.json; the endpoints below can be overridden
// per build (CI runtime tests point the app at the runner's ephemeral
// stack through the emulator's host alias).
module.exports = () => ({
  ...appJson.expo,
  extra: {
    ...appJson.expo.extra,
    apiBaseUrl: process.env.TOOLKIT_API_BASE_URL
      || appJson.expo.extra.apiBaseUrl,
    oidcIssuer: process.env.TOOLKIT_OIDC_ISSUER
      || appJson.expo.extra.oidcIssuer,
    oidcClientId: process.env.TOOLKIT_MOBILE_CLIENT_ID
      || appJson.expo.extra.oidcClientId,
  },
});
