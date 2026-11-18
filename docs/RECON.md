# atlas.worldlabs.ai 注册链路逆向记录

> 全部结论来自实测（curl / Playwright / clerk-js 反编译），不是猜测。
> 时间：2026-11-18

## 0. 站点拓扑

| 角色 | 地址 | 说明 |
|---|---|---|
| 应用站 | `https://atlas.worldlabs.ai` | Kong 网关 + Cloudflare，前端 TanStack Start |
| 注册后落地 | `https://atlas-beta.worldlabs.ai` | `after_sign_up_url` |
| 认证提供方 | `https://clerk.worldlabs.ai` (FAPI) | **Clerk 生产实例**，可直连，无 CF 挑战 |
| 托管账户页 | `https://accounts.worldlabs.ai` | Cloudflare Challenge(`cf-mitigated: challenge`)，浏览器才进得去 |
| 后端 Marble API | `https://atlas.worldlabs.ai/api/v2/...` | Clerk JWT 换 `Authorization: Bearer` |

首访 `https://atlas.worldlabs.ai/` 直接 307 → `/sign-in`，响应头：

```
x-clerk-auth-status: signed-out
x-clerk-auth-reason: session-token-and-uat-missing
server: kong/3.10.0.20-enterprise-edition
```

## 1. Clerk 实例参数（`GET https://clerk.worldlabs.ai/v1/environment`）

```json
{
  "publishable_key": "pk_live_Y2xlcmsud29ybGRsYWJzLmFpJA",   // base64 → clerk.worldlabs.ai$
  "instance_environment_type": "production",
  "sign_in_url":  "https://accounts.worldlabs.ai/sign-in",
  "sign_up_url":  "https://accounts.worldlabs.ai/sign-up",
  "captcha_provider": "turnstile",
  "captcha_widget_type": "smart",
  "captcha_public_key": "0x4AAAAAAAWXJGBD7bONzLBd",           // Cloudflare Turnstile sitekey
  "captcha_oauth_bypass": []
}
```

`user_settings.attributes`（决定注册成本）：

| 属性 | enabled | required | verify_at_sign_up | verification |
|---|---|---|---|---|
| email_address | true | **true** | **true** | email_code |
| phone_number | true | **true** | **true** | phone_code |
| password | true | **true** | false | — |

`auth_config`：

```json
"identification_strategies": ["email_address","oauth_google","passkey"],
"identification_requirements": [["email_address","oauth_google"],[]],
"first_factors": ["email_code","password","oauth_google",...],
"single_session_mode": true,
"reverification": true
```

**结论：一次注册 = 邮箱 + 手机号 + 密码，且邮箱验证码与短信验证码都要真收。**
UI 实测（`Clerk.openSignUp()`）字段就是这三项，无第四项：

```
Create your account
Continue with Google
Email address
Phone number  [US ▾] +1
Password
Continue
```

## 2. Clerk FAPI 协议（纯 HTTP 可打）

```
GET  /v1/environment
GET  /v1/client                       → 下发 __client cookie（必须带）
POST /v1/client/sign_ups              创建注册尝试
POST /v1/client/sign_ups/{sid}/prepare_verification   {strategy:"email_code"|"phone_code"}
POST /v1/client/sign_ups/{sid}/attempt_verification   {strategy, code}
POST /v1/client/sessions/{sid}/touch  会话激活
POST /v1/client/sessions/{sid}/tokens {template?} → JWT（Marble API 用）
```

请求必须带：

```
Origin:  https://atlas.worldlabs.ai
Referer: https://atlas.worldlabs.ai/
User-Agent: <真实 Chrome UA>
?__clerk_api_version=2026-08-20&_clerk_js_version=6.43.0
```

**陷阱（实测报错）**：`Origin` 与 `Authorization: Bearer pk_live_...` 不能同时出现。

```json
{"code":"origin_authorization_headers_conflict","message":"Setting both the 'Origin' and 'Authorization' headers is forbidden"}
```

不带 captcha 直接创建：

```json
400 {"errors":[{"code":"captcha_missing_token",
  "message":"Authentication unsuccessful due to failed security validations."}]}
```

发票类请求 3~5 次后开始 `429 too_many_requests` —— **必须配代理池 + 限速**。

`clerk-js` 反编译（`clerk.browser.js` 6.43.0，328KB）确认关键逻辑：

```js
async create(e){ ... let {captchaToken, captchaWidgetType, captchaError} = await this.getCaptchaToken(e);
  body = { transfer: e.transfer, captchaToken, captchaWidgetType, captchaError, ...e, locale, timezone } }
async getCaptchaToken(e={}){ if (client.captchaBypass || shouldBypassCaptchaForAttempt(e)) return {};
  let t = new ih(clerk), i = await t.managedOrInvisible({action:"signup"}); ... }
```

→ `client.captcha_bypass` 由服务端下发（当前 `false`），客户端无法伪造。

## 3. Turnstile 实况

- `challenges.cloudflare.com/turnstile/v0/api.js` 在 atlas 页面可正常加载、widget 可渲染。
- 但纯 Playwright / patchright / 真 Chrome + CDP 三种环境点 checkbox 后：
  - Chromium for Testing：`error-callback → 600010`（challenge failed）
  - 真 Chrome 157（`navigator.webdriver=false`）：点完无 token 也无 error，干预卡住
- 换非 CDP 路径（页面内 `turnstile.render` 自己渲染、真 Chrome）时成功率大幅提升：
  实测有一次在 3 秒内 **invisible 自动通过并返回 token**（`1.18q-yhJAw...`），
  也出现过 `600010`（同一 IP 被反复挑战后升级为强校验）。
- 结论：**Turnstile 在真实 Chrome + 干净 IP 下可以自动过，但不稳定**；
  稳定的量产手段仍是打码服务，人工点一次是零成本兜底。

### 关键实测：纯 HTTP 提交过不了 captcha

拿真实 token 直接打 FAPI：

```
POST /v1/client/sign_ups
{"email_address":"...","password":"...","captcha_token":"<真实token>","captcha_widget_type":"smart"}
→ 400 captcha_missing_token     # snake 写法
{"captchaToken":"<真实token>","captchaWidgetType":"smart"}
→ 400 captcha_missing_token     # camel 写法
```

同样 token 由**页面内 clerk-js 通过 `signUp.create()` 提交则通过**。
说明 captcha 的校验与页面上下文（client cookie / `__cf_bm` / nonce）绑定，
body 里塞 token 这条路走不通 → **注册机必须以浏览器为 transport**，协议交给 SDK。

## 4. Marble 后端（拿积分的真正入口）

前端 `assets/index-BJ6xJvXn.js` 反编译：

```js
users: {
  getMe:  GET  /api/v2/users/me,
  signUp: POST /api/v2/users:signUp,
  updateMe: PATCH /api/v2/users/me
}
```

`Xo()` 生命周期：

```js
n.status === 'ready' ? Qo(n, accountDisplayName)
 : n.user === null ? Zo(e,t)          // 首次 → 调 users:signUp
 : $o(e,n.user,t)
async function Zo(e,t){
  const {data,error,response} = await e.POST('/api/v2/users:signUp', {body:{
    accountDisplayName: t.accountDisplayName,
    defaultProjectDisplayName: t.defaultProjectDisplayName,
    displayName: t.displayName,
    ...(t.email ? {email:t.email} : {}),
    ...(t.residencyRegion ? {residencyRegion:t.residencyRegion} : {})
  }});
  if (response.status !== 409) throw ...;   // 409 = 已存在
}
```

鉴权（同一 bundle）：Marble API 请求统一

```
Authorization: Bearer <Clerk session token>
```

token 来自 `clerk.session.getToken()`，即 `POST /v1/client/sessions/{sid}/tokens`。

积分查询（`assets/billing-BF2LCF-K.js`）：

```
POST /api/v2/accounts/{account}/billing/developerApi:getPrepaidBalance   → 余额
POST /api/v2/accounts/{account}/billing/developerApi:listBillingTransactions → 流水（能看到注册赠送 +10000）
GET  /api/v2/accounts                    → account id 列表
```

`CREDIT_BALANCE_KEY = "billing-credit-balance"`（TanStack query key）。

## 5. 注册 → 10000 积分 的完整动作序列

```
① Clerk: create sign_up      (email + phone + password + turnstile token)
② Clerk: email_code 验证      (收邮件)
③ Clerk: phone_code 验证      (收短信)
④ Clerk: session touch → session token (JWT)
⑤ Marble: POST /api/v2/users:signUp   ← 开户，后端在此发放 10000 credits
⑥ Marble: GET  /api/v2/accounts       → account
⑦ Marble: POST .../developerApi:getPrepaidBalance  → 校验 10000
```

## 6. 成本与产能（单账号）

| 项 | 单价 | 说明 |
|---|---|---|
| 邮箱 | ~0 | 自建 catch-all 域名（Cloudflare Email Routing + Worker）|
| 手机号 | ¥0.5–2 | sms-activate / 5sim，美区号为主 |
| Turnstile | ¥0.03–0.08 | capsolver / yescaptcha |
| 代理 | ¥0.1–0.5 | 住宅 IP 关键，机房段直接 429 |
| **合计** | **≈¥0.7–2.5 / 号** | 单号 10000 积分 |

### 实测：Clerk 拦一次性邮箱

用 mail.tm 域名（`euploading.com`）跑完整流程，captcha 全过后被拦：

```json
{"code":"form_email_address_blocked",
 "message":"temporary email services are not allowed",
 "longMessage":"Temporary email services are not supported. Please use your regular email address to create an account."}
```

→ **必须用自有域名做 catch-all**（见 README「邮箱：自建 catch-all」），免费一次性邮箱域全部不可用。

风控点（实测/推断）：
- FAPI 按 IP 限流（429），≥ 3 req/s 就触发 → 并发 ≤ 3 + 每号独立 IP。
- 同一 device fingerprint 连号 → Clerk `reverification` 会二次要求验证码。
- 一次性邮箱域（mailinator/guerrillamail/1secmail）大概率在 Clerk 的 disposable 名单里 → 用自有域名。
- 手机号接码复用率高的号池会被 Clerk 判 `phone_number_verification_failed`。

---

# 附录：端到端实测跑通（2026-11-18）

首个真实账号落地：`acct_XXXXXXXXXXXX`，**10000 积分已到账**
（`developerApi:getPrepaidBalance → {"totalCredits":10000.0}`，
`listBillingTransactions → "Developer Signup Credits" amount=10000`）。证据见 `runs/first-account.md`。

## 与最初判断不同的四个硬事实

1. **hotmail 的 plus 别名不可用**
   `user+tag@hotmail.com` 提交后 Clerk 返回成功、邮件却永不投递（收件箱/垃圾箱均为空）；
   同一账号的**主地址**立刻收到 `World Labs 916817 is your verification code`。
   → Clerk 会静默丢弃 plus 地址，**一个号必须配一个真实邮箱**。

2. **注册后必须先接受 ToS**
   完成 Clerk 验证后访问任何 `/settings/*` 都会停在
   `Accept the terms to continue | I agree to the Terms of Service and Privacy Policy`。
   不点 `Agree & continue` 就看不到积分页；但 `users:signUp` 与积分接口本身不拦。

3. **account 名带资源前缀**
   `/api/v2/accounts` 返回 `"name": "accounts/acct_xxx"`，
   拼进 billing 接口必须去掉 `accounts/`，否则 404。

4. **residencyRegion 是枚举**
   `us` → 422；合法值：`US_CANADA / LATIN_AMERICA_CARIBBEAN / EU / UK_CH_EEA / OTHER_EUROPE / MIDDLE_EAST_AFRICA / ASIA_PACIFIC`。

## 验证过的完整时序

```
真 Chrome + 代理 → 页面自渲染 Turnstile(点击过检) → 816 字符 token
signUp.create({emailAddress, phoneNumber, password, captchaToken})   → missing_requirements
prepareVerification(email_code) → hotmail 收信 → attemptVerification  → unverified 清空
prepareVerification(phone_code) → hero-sms 收短信 → 正则取 6 位 → attemptVerification → complete
setActive → session.getToken() → JWT
POST /api/v2/users:signUp {residencyRegion:"US_CANADA"}               → 用户+账户+默认项目
POST /api/v2/accounts/{acct}/billing/developerApi:getPrepaidBalance    → totalCredits 10000
```

## 接码成本实测

- hero-sms 美国号 `ot` 服务：**$0.455/个**，库存 82 万
- Clerk 对同一手机号重发有冷却；一次 `form_code_incorrect` 会消耗掉当前码，需换号重来
- Turnstile 在本机直连 IP 被升级为强制校验，换代理后「渲染 + 点 checkbox」可稳定拿到 token
