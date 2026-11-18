#!/usr/bin/env python3
"""atlas.worldlabs.ai 注册机 CLI。

用法：
  python reg.py probe                      # 环境自检（不打码也能跑）
  python reg.py run --n 10                 # 纯 HTTP 批量注册（需打码 key + 接码 key）
  python reg.py browser --n 1 --mode ui     # 浏览器模式（无打码 key 时）
  python reg.py browser --n 1 --mode token  # 浏览器 + 打码服务注入 token
  python reg.py stats                       # 看已注册多少、总分
  python reg.py export --out accounts.csv   # 导出账号
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atlasreg import config as cfgmod
from atlasreg.atlas import Marble
from atlasreg.clerk import Clerk
from atlasreg.http import Http
from atlasreg.pipeline import run_batch
from atlasreg.store import Store


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = cfgmod.load(args.config)
    print("== 配置 ==")
    print(f"  FAPI           : {cfg.atlas.clerk_fapi}")
    print(f"  publishable key: {cfg.atlas.publishable_key}")
    print(f"  captcha        : {cfg.captcha.provider} key={'SET' if cfg.captcha.api_key else 'MISSING'} "
          f"sitekey={cfg.captcha.sitekey}")
    print(f"  mail           : {cfg.mail.provider} domain={cfg.mail.domain or 'MISSING'}")
    print(f"  sms            : {cfg.sms.provider} key={'SET' if cfg.sms.api_key else 'MISSING'}")
    print(f"  proxies        : {len(cfg.run.proxies)} 个")

    async def go() -> None:
        async with Http(cfg.run.user_agent, proxy=None, origin=cfg.atlas.app_url) as http:
            clerk = Clerk(cfg.atlas, http)
            try:
                env = await clerk.environment()
            except Exception as e:  # noqa: BLE001
                print(f"  !! FAPI 不可达: {e}")
                return
            d = env.get("display_config", {})
            us = env.get("user_settings", {}).get("attributes", {})
            print("\n== Clerk 实例 ==")
            print(f"  应用        : {d.get('application_name')} ({d.get('instance_environment_type')})")
            print(f"  captcha     : {d.get('captcha_provider')} / {d.get('captcha_widget_type')} "
                  f"sitekey={d.get('captcha_public_key')}")
            print(f"  注册标识符  : {env.get('auth_config', {}).get('identification_strategies')}")
            for k in ("email_address", "phone_number", "password"):
                a = us.get(k, {})
                print(f"  {k:14s}: required={a.get('required')} verify_at_sign_up={a.get('verify_at_sign_up')}")
            client = await clerk.bootstrap()
            print(f"\n== client ==\n  id={client.get('id')} captcha_bypass={client.get('captcha_bypass')}")
            print(f"  cookies: {list(http.cookies)}")
        print("\n== 就绪检查 ==")
        miss = []
        if not cfg.captcha.api_key and cfg.captcha.provider != "none":
            miss.append("CAPTCHA_KEY（capsolver/yescaptcha）")
        if not cfg.mail.domain and cfg.mail.provider != "mailtm":
            miss.append("MAIL_DOMAIN（catch-all 域名）")
        if not cfg.sms.api_key and cfg.sms.provider != "manual":
            miss.append("SMS_KEY（sms-activate/5sim）")
        if miss:
            print("  缺：" + "，".join(miss))
            print("  → 补齐后即可 run；没有打码 key 就用 `python reg.py browser`（页面内 SDK，Turnstile 能自动过）")
        else:
            print("  全部就绪，可以 run")

    asyncio.run(go())
    return 0


def _validate(cfg) -> list[str]:
    """开跑前把缺的资源一次性列清，别让用户跑到一半才炸。"""
    miss: list[str] = []
    if cfg.captcha.provider != "none":
        if cfg.captcha.provider == "manual" and not cfg.captcha.manual_token:
            miss.append("manual 模式需要 --captcha-token")
        elif cfg.captcha.provider != "manual" and not cfg.captcha.api_key:
            miss.append(f"CAPTCHA_KEY（provider={cfg.captcha.provider}）")
    if cfg.mail.provider == "imap" and not (cfg.mail.domain and cfg.mail.imap_host):
        miss.append("MAIL_DOMAIN + IMAP_* （或改用 provider: http / mailtm）")
    if cfg.mail.provider == "http" and not (cfg.mail.domain and cfg.mail.http_url):
        miss.append("MAIL_DOMAIN + MAIL_HTTP_URL")
    if cfg.mail.provider == "mailtm":
        print("  ! mailtm 域名可能被 Clerk 判一次性邮箱，仅建议验证流程")
    if cfg.sms.provider != "manual" and not cfg.sms.api_key:
        miss.append(f"SMS_KEY（provider={cfg.sms.provider}）")
    return miss


def cmd_run(args: argparse.Namespace) -> int:
    cfg = cfgmod.load(args.config)
    cfg.run.concurrency = args.concurrency or cfg.run.concurrency
    if args.captcha_token:
        cfg.captcha.provider = "manual"
        cfg.captcha.manual_token = args.captcha_token
    miss = _validate(cfg)
    if miss:
        print("缺资源，先补齐：")
        for m in miss:
            print(f"  - {m}")
        print("\n提示：没有打码 key 时，先在浏览器里人工过一次 Turnstile，再：")
        print("  python reg.py run --n 1 --captcha-token <TOKEN>")
        return 2
    store = Store(cfg.run.db_path)
    results = asyncio.run(run_batch(cfg, args.n, store, verbose=not args.quiet))
    ok = [r for r in results if r.get("ok")]
    print(f"\n== 结果 == 成功 {len(ok)}/{len(results)}，累计账号 {store.ok_count()}，"
          f"累计积分 {store.credits_sum():.0f}")
    for r in results:
        if not r.get("ok"):
            print(f"  x {r.get('email')} @ {r.get('stage')}: {r.get('error')}")
    return 0 if len(ok) == len(results) else 1


def cmd_browser(args: argparse.Namespace) -> int:
    from atlasreg.browser import run_sdk_signup

    cfg = cfgmod.load(args.config)
    miss = _validate(cfg)
    store = Store(cfg.run.db_path)
    ok = 0
    for i in range(args.n):
        proxy = cfg.run.proxies[i % len(cfg.run.proxies)] if cfg.run.proxies else None
        print(f"\n=== 浏览器 SDK 注册 {i + 1}/{args.n} ===")
        if miss:
            print("  (缺少：" + "，".join(miss) + ")")
        try:
            r = run_sdk_signup(
                cfg, store, proxy=proxy, headless=False,
                profile_dir=args.profile,
                on_need_human=lambda p: print("  ⚠ Turnstile 需要人工点一下（最多等 180s）..."),
            )
            print(json.dumps({k: v for k, v in r.items() if k != "jwt"}, ensure_ascii=False, indent=1))
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  !! {type(e).__name__}: {e}")
    print(f"\n== 成功 {ok}/{args.n}，累计 {store.ok_count()} 号 / {store.credits_sum():.0f} 积分")
    return 0 if ok else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """用已有 JWT 直接查 Marble 侧账号与积分。"""
    cfg = cfgmod.load(args.config)

    proxy = args.proxy or (cfg.run.proxies[0] if cfg.run.proxies else None)

    async def go() -> None:
        async with Http(cfg.run.user_agent, origin=cfg.atlas.app_url, proxy=proxy) as http:
            m = Marble(cfg.atlas, http, args.jwt)
            try:
                me = await m.me()
                print("me:", json.dumps(me, ensure_ascii=False)[:400])
                accs = await m.accounts()
                print("accounts:", json.dumps(accs, ensure_ascii=False)[:400])
                acc = await m.first_account()
                if acc:
                    print(f"account id: {acc}")
                    print("credits:", await m.credits_total(acc))
                    print("transactions:", json.dumps(await m.transactions(acc), ensure_ascii=False)[:600])
            except Exception as e:  # noqa: BLE001
                print(f"!! {type(e).__name__}: {e}")

    asyncio.run(go())
    return 0


def cmd_ms_login(args: argparse.Namespace) -> int:
    """设备码授权一个微软账号，拿 Graph refresh_token 存进池子。"""
    from atlasreg.mail_ms import MSAccount, Pool, device_login

    cfg = cfgmod.load(args.config)
    path = cfg.mail.ms_accounts_file or "ms_accounts.json"
    pool = Pool(path)

    async def go() -> None:
        def on_code(code: str, url: str) -> None:
            print("\n" + "=" * 58)
            print(f"  1) 打开   {url}")
            print(f"  2) 输入   {code}")
            print(f"  3) 用 {args.email or '目标 hotmail'} 登录并同意授权")
            print("=" * 58 + "\n（正在等待授权，15 分钟内有效）", flush=True)

        r = await device_login(args.email, on_code)
        acc = MSAccount(email=r["email"], refresh_token=r["refresh_token"], note="device-code")
        pool.add(acc)
        print(f"✔ 已入库：{acc.email}")
        print(f"  别名示例：{acc.alias('atlas01')}")
        print(f"  文件：{path}")

    asyncio.run(go())
    return 0


def cmd_ms_import(args: argparse.Namespace) -> int:
    """导入微软账号：一行一个 `email----password----uuid----refresh_token`。"""
    from atlasreg.mail_ms import Pool, parse_line

    cfg = cfgmod.load(args.config)
    path = cfg.mail.ms_accounts_file or "ms_accounts.json"
    pool = Pool(path)
    src = Path(args.file).read_text(encoding="utf-8").splitlines()
    ok = 0
    for line in src:
        if not line.strip():
            continue
        try:
            pool.add(parse_line(line))
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  跳过: {e}")
    print(f"✔ 导入 {ok} 个账号 → {path}（共 {len(pool.accounts)} 个）")
    return 0


def cmd_ms_link(args: argparse.Namespace) -> int:
    """打开可见浏览器，人工登录一次 outlook，之后靠 profile 自动读信。"""
    from atlasreg.owa import Owa

    cfg = cfgmod.load(args.config)
    profile = args.profile or "ms_profile"
    o = Owa(profile)
    if args.email and args.password:
        r = o.auto_login(args.email, args.password, timeout=args.timeout)
    else:
        r = o.login(timeout=args.timeout)
    print(f"✔ profile 已保存：{profile}（邮箱 {r['email'] or '未知'}）")
    print("  之后读信：python reg.py ms-test --email <地址> --profile " + profile)
    return 0


def cmd_ms_test(args: argparse.Namespace) -> int:
    """测一个微软账号能不能通过 Graph 读到信。"""
    from atlasreg.mail_ms import Pool

    cfg = cfgmod.load(args.config)
    if args.profile:
        # 走浏览器登录态（OWA），不需要 OAuth
        from atlasreg.owa import Owa

        o = Owa(args.profile)
        alias = args.email
        if args.alias_tag:
            base = args.email.split("@")[0].split("+")[0]
            alias = f"{base}+{args.alias_tag}@{args.email.split('@')[-1]}"
        print(f"监听收件人：{alias}")
        code = o.wait_code(alias, timeout=args.wait or 120, latest=args.latest)
        print(f"✔ 取到验证码：{code}")
        return 0

    pool = Pool(cfg.mail.ms_accounts_file or "ms_accounts.json")
    acc = next((a for a in pool.accounts if a.email.lower() == args.email.lower()), None)
    if not acc:
        print(f"池子里没有 {args.email}")
        return 1

    async def go() -> None:
        tok = await pool.token(acc)
        print(f"✔ token ok（{len(tok)} chars）")
        msgs = await pool._messages(acc, tok, top=args.top)
        print(f"收件箱最近 {len(msgs)} 封：")
        for m in msgs:
            to = ", ".join((r.get("emailAddress") or {}).get("address", "") for r in (m.get("toRecipients") or []))
            print(f"  [{m.get('receivedDateTime','')[:19]}] {(m.get('subject') or '')[:52]}")
            print(f"        to: {to[:90]}")
        if args.wait:
            tag = random_tag()
            print(f"\n等 {args.email.split('@')[0]}+{tag}@{args.email.split('@')[-1]} 的码 ...")
            code = await pool.wait_code(acc.alias(tag), timeout=args.wait)
            print(f"✔ 取到验证码：{code}")

    asyncio.run(go())
    return 0


def random_tag(n: int = 8) -> str:
    import string

    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


HELPER_JS = """
async () => {
  if (!window.turnstile) {
    await new Promise((res) => {
      const s = document.createElement('script');
      s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
      s.onload = res; document.head.appendChild(s); setTimeout(res, 8000);
    });
  }
  const el = document.createElement('div');
  el.id = 'tshelper';
  el.style.cssText = 'position:fixed;bottom:24px;left:24px;z-index:2147483647;background:#fff;padding:12px;border-radius:8px;box-shadow:0 4px 24px #0006';
  el.innerHTML = '<div style="font:12px sans-serif;margin-bottom:8px">点一下验证框，token 会自动打印</div>';
  document.body.appendChild(el);
  window.__TOKEN = null;
  window.turnstile.render('#tshelper', {
    sitekey: '0x4AAAAAAAWXJGBD7bONzLBd',
    callback: (t) => { window.__TOKEN = t; console.log('TOKEN=' + t); },
    'error-callback': (e) => { window.__TERR = String(e); },
  });
  return 'rendered';
}
"""


def cmd_token(args: argparse.Namespace) -> int:
    """辅助：打开页面渲染 Turnstile widget，人工点过后把 token 打出来。"""
    from playwright.sync_api import sync_playwright

    from atlasreg.browser import chrome_path as _chrome_path

    cfg = cfgmod.load(args.config)
    exe = _chrome_path()
    with sync_playwright() as pw:
        kw = {"headless": False, "args": ["--disable-blink-features=AutomationControlled"]}
        if exe:
            kw["executable_path"] = exe
        b = pw.chromium.launch(**kw)
        ctx = b.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        page = ctx.new_page()
        page.goto(f"{cfg.atlas.app_url}/sign-in", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_function("window.Clerk && window.Clerk.loaded", timeout=40000)
        page.evaluate(HELPER_JS)
        print("页面已打开，左下角有验证框。请在浏览器里点一下 Verify you are human ...")
        for i in range(120):
            page.wait_for_timeout(1000)
            tok = page.evaluate("window.__TOKEN")
            err = page.evaluate("window.__TERR || null")
            if tok:
                print(f"\nTOKEN={tok}\n")
                print("马上用（有效期约 300s）：")
                print(f"  python reg.py run --n 1 --captcha-token '{tok}'")
                break
            if err:
                print(f"验证失败 error={err}（换 IP / 换浏览器再试）")
                break
        else:
            print("等待超时，没拿到 token")
        ctx.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = cfgmod.load(args.config)
    store = Store(cfg.run.db_path)
    print(f"账号 {store.ok_count()} 个，累计积分 {store.credits_sum():.0f}")
    rows = store.conn.execute(
        "SELECT stage, COUNT(*) c FROM accounts GROUP BY stage ORDER BY c DESC"
    ).fetchall()
    for r in rows:
        print(f"  {r['stage']:20s} {r['c']}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cfg = cfgmod.load(args.config)
    store = Store(cfg.run.db_path)
    n = store.export(args.out)
    print(f"已导出 {n} 个账号 -> {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="atlas.worldlabs.ai 注册机")
    ap.add_argument("--config", default=None, help="config.yaml 路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="环境自检")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("run", help="纯 HTTP 批量注册")
    p.add_argument("--n", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--captcha-token", default="", help="人工过检拿到的 Turnstile token（单号验证全流程用）")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("browser", help="浏览器内 SDK 注册（实测最稳）")
    p.add_argument("--n", type=int, default=1)
    p.add_argument("--profile", default=None, help="持久化 profile 目录")
    p.set_defaults(func=cmd_browser)

    p = sub.add_parser("verify", help="用已有 JWT 查账号与积分")
    p.add_argument("--jwt", required=True)
    p.add_argument("--proxy", default="", help="例如 http://127.0.0.1:7890")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("token", help="辅助：人工过 Turnstile 拿 token")
    p.set_defaults(func=cmd_token)

    p = sub.add_parser("ms-login", help="设备码授权微软账号（Graph 接码）")
    p.add_argument("--email", default="", help="目标 hotmail（仅用于提示/兜底命名）")
    p.set_defaults(func=cmd_ms_login)

    p = sub.add_parser("ms-import", help="导入微软账号（email----pwd----uuid----token）")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_ms_import)

    p = sub.add_parser("ms-link", help="浏览器登录一次 outlook（推荐，能过 MFA）")
    p.add_argument("--profile", default="ms_profile")
    p.add_argument("--email", default="")
    p.add_argument("--password", default="")
    p.add_argument("--timeout", type=int, default=300)
    p.set_defaults(func=cmd_ms_link)

    p = sub.add_parser("ms-test", help="测微软账号读信")
    p.add_argument("--email", required=True)
    p.add_argument("--profile", default="", help="用浏览器登录态读信（ms-link 生成的 profile）")
    p.add_argument("--alias-tag", default="", help="用 plus 别名监听，例如 atlas01")
    p.add_argument("--latest", action="store_true", help="直接取最新一封的码（不做新旧快照）")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--wait", type=int, default=0, help="非 0 则等待一封新邮件验证码（秒）")
    p.set_defaults(func=cmd_ms_test)

    p = sub.add_parser("stats", help="统计")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("export", help="导出账号")
    p.add_argument("--out", default="accounts.csv")
    p.set_defaults(func=cmd_export)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
