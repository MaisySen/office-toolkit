// Cloudflare Worker in front of the app host: doctool.multies.si.
// TLS, the public hostname and edge rules live here; the host refuses
// anything without the shared header (deploy/render/wsgi.py).
// Secrets: PROXY_SECRET. Vars: ORIGIN (the host's own https URL).
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const target = new URL(url.pathname + url.search, env.ORIGIN);
    const headers = new Headers(request.headers);
    headers.delete("host");
    headers.delete("cookie2");
    headers.set("X-Doctool-Proxy", env.PROXY_SECRET);
    headers.set("X-Real-Client-IP", request.headers.get("CF-Connecting-IP") || "");
    const init = { method: request.method, headers, redirect: "manual" };
    if (!["GET", "HEAD"].includes(request.method)) init.body = request.body;
    const upstream = await fetch(target, init);
    const response = new Response(upstream.body, upstream);
    const location = response.headers.get("Location");
    if (location && location.startsWith(env.ORIGIN)) {
      response.headers.set("Location", "https://" + url.host + location.slice(env.ORIGIN.length));
    }
    response.headers.set("Strict-Transport-Security", "max-age=31536000");
    return response;
  },
  // Keep the free host from sleeping after 48 h without traffic.
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(fetch(new URL("/healthz", env.ORIGIN)));
  },
};
