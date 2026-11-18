/**
 * Cloudflare Email Worker：catch-all 域名收信 → 提取 6 位验证码 → KV
 * 部署：Workers → 绑定 KV (MAILBOX) → Email Routing 把 *@r.yourdomain.com 全部 route 到此 Worker
 * 读取：GET https://mail.yourdomain.com/code?to=<address>  （由 worker 同源的 fetch handler 提供）
 *
 * 注册机侧配置：
 *   mail.provider: http
 *   mail.http_url: https://mail.yourdomain.com/code?to={addr}
 *   mail.http_token: <TOKEN>
 */

const TOKEN = "REPLACE_WITH_RANDOM_TOKEN";   // 与服务端 MAIL_HTTP_TOKEN 一致
const CODE_RE = /\b(\d{6})\b/;

export default {
  // ---------------------------------------------------------------- 收信
  async email(message, env, ctx) {
    const to = (message.to || "").toLowerCase();
    const raw = await new Response(message.raw).text();

    // Clerk 的验证码邮件：6 位数字
    let code = null;
    const m = raw.match(CODE_RE);
    if (m) code = m[1];

    if (code) {
      // 每个收件地址一个 key，保留 1 小时
      await env.MAILBOX.put(`code:${to}`, code, { expirationTtl: 3600 });
      await env.MAILBOX.put(`raw:${to}`, raw.slice(0, 8000), { expirationTtl: 3600 });
    }
  },

  // ---------------------------------------------------------------- 取码 API
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/code") return new Response("not found", { status: 404 });

    const auth = request.headers.get("Authorization") || "";
    if (auth !== `Bearer ${TOKEN}`) return new Response("unauthorized", { status: 401 });

    const to = (url.searchParams.get("to") || "").toLowerCase();
    if (!to) return new Response("missing to", { status: 400 });

    const code = await env.MAILBOX.get(`code:${to}`);
    if (!code) return new Response("", { status: 200 });   // 还没到
    return new Response(code, { status: 200, headers: { "content-type": "text/plain" } });
  },
};
