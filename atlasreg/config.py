"""配置加载：config.yaml + 环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: Any = None) -> Any:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass
class CaptchaCfg:
    provider: str = "capsolver"          # capsolver | yescaptcha | none
    api_key: str = ""
    api_url: str = ""                    # 留空用默认
    sitekey: str = "0x4AAAAAAAWXJGBD7bONzLBd"
    page_url: str = "https://atlas.worldlabs.ai/sign-in"
    action: str = "signup"
    timeout: int = 120
    field_style: str = "both"
    manual_token: str = ""               # provider=manual 时用人工过检的 token            # snake | camel | both


@dataclass
class MailCfg:
    provider: str = "imap"               # imap | mailtm | http
    domain: str = ""                     # 自有 catch-all 域名，如 r.example.com
    local_len: int = 10
    # imap
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_pass: str = ""
    imap_folder: str = "INBOX"
    # 通用 HTTP 收信箱（自建 worker）
    http_url: str = ""                   # 形如 https://mail.example.com/api/code?to={addr}
    http_token: str = ""
    # 微软账号池（provider=ms）
    ms_accounts_file: str = "ms_accounts.json"
    ms_email: str = ""                 # provider=msowa 时用哪个 hotmail 收信
    owa_profile: str = "ms_profile"    # ms-link 生成的浏览器登录态目录
    wait: int = 180


@dataclass
class SmsCfg:
    provider: str = "sms-activate"       # sms-activate | 5sim | manual
    api_key: str = ""
    country: str = "0"                   # sms-activate: 0=俄罗斯 187=美国 ...
    service: str = ""                    # sms-activate service code，留空用默认
    operator: str = "any"
    max_price: float = 0.5
    wait: int = 240
    # 5sim
    fivesim_country: str = "usa"
    fivesim_product: str = "other"


@dataclass
class AtlasCfg:
    app_url: str = "https://atlas.worldlabs.ai"
    clerk_fapi: str = "https://clerk.worldlabs.ai"
    publishable_key: str = "pk_live_Y2xlcmsud29ybGRsYWJzLmFpJA"
    clerk_api_version: str = "2026-08-20"
    clerk_js_version: str = "6.43.0"
    # Marble 开户默认值
    pool_url: str = "http://127.0.0.1:8790"   # 注册成功后自动入池的网关地址
    residency_region: str = "US_CANADA"   # 合法值: US_CANADA / LATIN_AMERICA_CARIBBEAN / EU / UK_CH_EEA / OTHER_EUROPE / MIDDLE_EAST_AFRICA / ASIA_PACIFIC
    default_project_name: str = "My First World"
    display_name_pool: str = "Atlas User"


@dataclass
class RunCfg:
    concurrency: int = 2
    per_ip_delay: float = 4.0
    proxies: list[str] = field(default_factory=list)
    db_path: str = "accounts.db"
    password: str = ""                   # 留空自动生成
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )


@dataclass
class Config:
    captcha: CaptchaCfg
    mail: MailCfg
    sms: SmsCfg
    atlas: AtlasCfg
    run: RunCfg


def load(path: str | Path | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.yaml"
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    c = CaptchaCfg(**{**raw.get("captcha", {})})
    m = MailCfg(**{**raw.get("mail", {})})
    s = SmsCfg(**{**raw.get("sms", {})})
    a = AtlasCfg(**{**raw.get("atlas", {})})
    r = RunCfg(**{**raw.get("run", {})})

    # 环境变量覆盖（CI / 临时改参）
    c.api_key = _env("CAPTCHA_KEY", c.api_key)
    c.provider = _env("CAPTCHA_PROVIDER", c.provider)
    c.manual_token = _env("CAPTCHA_TOKEN", c.manual_token)
    m.domain = _env("MAIL_DOMAIN", m.domain)
    m.imap_host = _env("IMAP_HOST", m.imap_host)
    m.imap_user = _env("IMAP_USER", m.imap_user)
    m.imap_pass = _env("IMAP_PASS", m.imap_pass)
    m.http_url = _env("MAIL_HTTP_URL", m.http_url)
    m.ms_email = _env("MS_EMAIL", m.ms_email)
    m.owa_profile = _env("MS_PROFILE", m.owa_profile)
    m.http_token = _env("MAIL_HTTP_TOKEN", m.http_token)
    s.api_key = _env("SMS_KEY", s.api_key)
    s.provider = _env("SMS_PROVIDER", s.provider)
    if _env("PROXIES"):
        r.proxies = [x.strip() for x in str(_env("PROXIES")).split(",") if x.strip()]
    return Config(c, m, s, a, r)
