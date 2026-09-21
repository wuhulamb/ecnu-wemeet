#!/usr/bin/env python3
"""
华东师大云视频会议系统 纯 HTTP 登录模块 (vmr.ecnu.edu.cn)

不依赖 Playwright/浏览器,直接复刻前端登录链路:

    1. api.ecnu.edu.cn/oauth2/authorize   (OAuth2,建立 api 域会话)
       → portal1 / cas/login → sso.ecnu.edu.cn/login (统一认证登录页)
    2. 解析页面中的 login-croypto(AES 密钥)与 login-page-flowkey(执行流)
    3. 提交登录表单:
        密码/验证码 payload 用 AES-128-ECB 加密(crypto-js 同款)
        验证码 payload 传 "{}" —— 未触发风控时服务端接受(实测通过)
    4. ticket → OAuth code → POST /api/v1/user/sso
    5. 前端把响应数据 JSON.stringify 后写入 user_info cookie
       (本模块直接照做,返回与浏览器一致的值)

验证码策略:
    - 不尝试破解/绕过验证码
    - 若服务端要求验证码(风控触发),抛 LoginError(code=2)

核心接口:
    login(username, password) -> str
        返回 user_info cookie 值(JSON 字符串),供 booking.py 直接使用,
        不写任何文件。失败抛 LoginError。

命令行(仅调试/手动验证,不生成文件):
    uv run python login.py
"""

import base64
import json
import os
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from dotenv import load_dotenv

VMR_ORIGIN = "https://vmr.ecnu.edu.cn"
OAUTH_AUTHORIZE = (
    "https://api.ecnu.edu.cn/oauth2/authorize"
    "?scope=ECNU-Basic&response_type=code"
    "&client_id=8fb442345db6d45c&state=ecnu_mms"
    "&redirect_uri=" + "https%3A%2F%2Fvmr.ecnu.edu.cn%2Fsign-in"
)
VM_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class LoginError(Exception):
    """登录失败。code: 1=常规失败, 2=需要验证码(不处理)"""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def aes_ecb_encrypt_b64(key_b64: str, plaintext: str) -> str:
    """与前端 crypto-js 一致的 AES-128-ECB/PKCS7,输出 base64"""
    key = base64.b64decode(key_b64)
    padder = padding.PKCS7(128).padder()
    data = padder.update(plaintext.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def parse_login_page(text: str):
    """从登录页提取 croypto(AES 密钥) 与 flowkey(execution)"""
    croypto = re.search(r'<p id="login-croypto">([^<]+)</p>', text)
    flowkey = re.search(r'<p id="login-page-flowkey">([^<]+)</p>', text)
    if not croypto or not flowkey:
        return None
    return croypto.group(1), flowkey.group(1)


def read_login_error(text: str) -> str:
    """读取登录失败信息(登录页重载时)"""
    code = re.search(r'id="login-error-code"[^>]*>([^<]*)<', text)
    params = re.search(r'id="login-error-params"[^>]*>([^<]*)<', text)
    msg = re.search(r'class="[^"]*login-error-msg[^"]*"[^>]*>([^<]*)<', text)
    parts = []
    if code and code.group(1).strip():
        parts.append(f"错误码={code.group(1).strip()}")
    if params and params.group(1).strip():
        parts.append(f"参数={params.group(1).strip()}")
    if msg and msg.group(1).strip():
        parts.append(f"提示={msg.group(1).strip()}")
    return " ".join(parts) if parts else ""


def _require_captcha(text: str) -> bool:
    """检测登录页是否要求图形/易盾验证码(风控触发)"""
    return ("验证码" in text or "captcha" in text.lower()) and "verification" in text


def login(username: str, password: str) -> str:
    """
    完成 SSO 登录并返回 user_info cookie 值(JSON 字符串)。

    返回值与浏览器前端 setUserInfo 写入的 cookie 一致:
        {"name":..., "token":..., "user_id":..., "time": <当前毫秒>, ...}
    不写任何文件。失败抛 LoginError。
    """
    session = requests.Session()
    session.headers["User-Agent"] = VM_UA

    # ── 1. OAuth 授权 → 跟随重定向到统一认证登录页(建立各域会话 cookie) ──
    try:
        r = session.get(OAUTH_AUTHORIZE, allow_redirects=True, timeout=45)
    except requests.RequestException as e:
        raise LoginError(f"访问 OAuth 失败: {e}") from e
    if not r.url.startswith("https://sso.ecnu.edu.cn/login"):
        raise LoginError(f"未跳转到统一认证登录页,当前URL: {r.url}")

    cred = parse_login_page(r.text)
    if not cred:
        raise LoginError("登录页缺少 croypto/flowkey,页面结构可能已变更")
    croypto, flowkey = cred

    # ── 2. 提交登录表单(AES 加密密码;未触发风控时服务端接受空验证码 payload) ──
    payload = {
        "type": "UsernamePassword",
        "_eventId": "submit",
        "geolocation": "",
        "execution": flowkey,
        "croypto": croypto,
        "username": username,
        "password": aes_ecb_encrypt_b64(croypto, password),
        "captcha_payload": aes_ecb_encrypt_b64(croypto, "{}"),
    }
    try:
        r = session.post(r.url, data=payload, allow_redirects=False, timeout=30)
    except requests.RequestException as e:
        raise LoginError(f"提交登录失败: {e}") from e

    if r.status_code not in (301, 302, 303):
        if _require_captcha(r.text):
            raise LoginError("服务端要求图形/易盾验证码,本模块不处理验证码,退出", code=2)
        err = read_login_error(r.text)
        raise LoginError(f"登录未跳转。{err or '未知错误'}")
    ticket_url = r.headers.get("Location", "")

    # ── 3. ticket → OAuth code → vmr sign-in ──
    try:
        r = session.get(ticket_url, allow_redirects=False, timeout=30)
        if r.status_code not in (301, 302, 303):
            raise LoginError("ticket 交换失败(未跳转)")
        r = session.get(r.headers["Location"], allow_redirects=False, timeout=30)
        if r.status_code not in (301, 302, 303):
            raise LoginError("未获得 OAuth code")
    except (requests.RequestException, KeyError) as e:
        raise LoginError(f"ticket/code 交换失败: {e}") from e

    signin_url = r.headers["Location"]
    if not signin_url.startswith(VMR_ORIGIN):
        raise LoginError(f"未重定向回 vmr,Location: {signin_url}")
    q = parse_qs(urlparse(signin_url).query)
    code = q.get("code", [""])[0]
    state = q.get("state", [""])[0]
    if not code:
        raise LoginError("回调缺少 code 参数")

    # ── 4. 与前端一致:POST /api/v1/user/sso 换用户信息 ──
    api = requests.Session()
    api.headers.update(
        {
            "User-Agent": VM_UA,
            "Origin": VMR_ORIGIN,
            "Referer": f"{VMR_ORIGIN}/sign-in?code={code}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
    )
    try:
        r = api.post(
            f"{VMR_ORIGIN}/api/v1/user/sso",
            data={"code": code, "state": state, "_t": ""},
            timeout=30,
        )
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        raise LoginError(f"user/sso 接口失败: {e}") from e

    if not body.get("success") or not body.get("data"):
        raise LoginError(f"user/sso 返回失败: {json.dumps(body, ensure_ascii=False)[:300]}")

    # ── 5. 前端把数据 JSON.stringify + time 后写入 user_info cookie ──
    user = body["data"]
    user["time"] = int(time.time() * 1000)
    return json.dumps(user, ensure_ascii=False, separators=(",", ":"))


def main():
    load_dotenv()
    username = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("VMR_USERNAME", "")
    password = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("VMR_PASSWORD", "")
    if not username or not password:
        print(
            "[错误] 缺少账号/密码:请在 .env 配置 VMR_USERNAME / VMR_PASSWORD,"
            "或传参数: python login.py <学号> <密码>"
        )
        sys.exit(1)
    try:
        cookie = login(username, password)
    except LoginError as e:
        print(f"[错误] 登录失败: {e}")
        sys.exit(e.code)
    user = json.loads(cookie)
    print("[✓] 登录成功!")
    print(f"      用户: {user.get('name')} ({user.get('user_id')})  {user.get('department', '')}")
    print("      user_info cookie 已就绪(未写文件,由 booking.py 内部使用)")


if __name__ == "__main__":
    main()
