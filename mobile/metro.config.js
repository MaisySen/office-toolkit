// Bare-workflow Metro config: use Expo's defaults so the dev server
// understands the .expo/.virtual-metro-entry bundle URL that the
// prebuilt native apps request in Debug builds.
const { getDefaultConfig } = require("@expo/metro-config");

module.exports = getDefaultConfig(__dirname);
