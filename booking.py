#!/usr/bin/env python3
"""
华东师大云视频会议系统(vmr.ecnu.edu.cn)预约工具

用法(自动使用 .env 的 VMR_USERNAME / VMR_PASSWORD 登录,无需 cookie 文件):
    # 查看我的会议(--days 指定天数,默认7;接口支持任意范围)
    python3 booking.py list --days 30

    # 预约会议(无密码):2026-09-23 14:00 时长60分钟;   --password 123456 设置密码
    python3 booking.py book --topic "周会" --date 2026-09-23 --time 14:00 --duration 120

    # 删除会议(list 里查到的 id,多个用逗号分隔)
    python3 booking.py delete 65464,65473

说明:
    - 登录复用 login.login(),登录态只存内存,不写文件
    - 会议默认提交后需管理员审批(页面提示"会议申请保存后将提交给管理员审批")
    - 不传 --password 则为无密码会议
    - 依赖: pip install requests
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.parse
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

from login import LoginError, login

load_dotenv()  # 读取 .env(VMR_USERNAME / VMR_PASSWORD)

BASE_URL = "https://vmr.ecnu.edu.cn/api/v1"
COOKIE_FILE = "cookie.txt"  # 登录态缓存文件(已 gitignore)


def extract_token_from_cookie(cookie: str) -> str:
    """从 login() 返回的 user_info JSON 中提取认证 token"""
    obj = json.loads(cookie)
    token = obj.get("token") if isinstance(obj, dict) else None
    if not token:
        raise ValueError("user_info 中未找到 token 字段")
    return token


def encrypt_token(token: str, ts: int | None = None) -> str:
    """
    与前端一致的 user_token 加密算法:
      r = reverse( base64( base64( reverse( quote(token) + "_" + ts ) ) + "_" + ts ) )
    """
    a = str(ts if ts is not None else int(time.time() * 1000))
    b = urllib.parse.quote(token, safe="") + "_" + a
    e = b[::-1]
    n = base64.b64encode(e.encode()).decode()
    i = base64.b64encode((n + "_" + a).encode()).decode()
    return i[::-1]


def build_session(cookie: str):
    """创建带 cookie 的 requests.Session 并生成新加密 token"""
    token = extract_token_from_cookie(cookie)
    ut = encrypt_token(token)

    # 浏览器发送 cookie 时会对值做百分号编码(尤其含中文/引号时),
    # requests 不会自动编码,这里手动编码避免 latin-1 报错
    session = requests.Session()
    session.headers["Cookie"] = "user_info=" + urllib.parse.quote(cookie, safe="")

    session.headers.update(
        {
            "user-token": ut,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://vmr.ecnu.edu.cn",
            "Referer": "https://vmr.ecnu.edu.cn/new",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
    )
    return session, ut


def book_meeting(
    cookie: str, topic: str, date: str, time_slot: str, duration: int = 120, password: str = ""
) -> dict:
    """
    预约会议
      date:      'YYYY-MM-DD'
      time_slot: 'HH:MM'
      duration:  分钟(默认120)
      password:  空字符串=无密码
    """
    session, ut = build_session(cookie)
    end_date = date  # 单次会议日期即结束日期

    payload = {
        "duration": str(duration),
        "meeting_date": date,
        "meeting_time": time_slot,
        "option_mute": "1",
        "option_jbh": "1",
        "option_h323": "0",
        "group_id": "2",
        "usage": "办公",
        "is_recurrent": "0",
        "recurrent_id": "0",
        "end_meeting_date": end_date,
        "end_meeting_times": "2",
        "repeat_type": "2",
        "size": "100",
        "auto_record": "0",
        "assistant": "",
        "option_enroll": "0",
        "enroll_attendees": "",
        "option_water_mark": "0",
        "option_sso_only": "0",
        "meeting_guests": "",
        "waiting_room": "0",
        "option_interpreter": "0",
        "id": "0",
        "quite": "true",
        "password": password,
        "option_live": "0",
        "topic": topic,
        "attendees": "[]",
        "ask": "1",
        "user_token": ut,
    }

    resp = session.post(f"{BASE_URL}/meeting/edit", data=payload, timeout=30)
    try:
        return resp.json()
    except ValueError:
        return {"status": resp.status_code, "raw": resp.text[:1000]}


def query_calendar(cookie: str, start: str | None = None, end: str | None = None) -> dict:
    """查询自己某时间段内的会议(只读接口,用于确认预约结果)"""
    session, ut = build_session(cookie)
    payload = {
        "user_token": ut,
        "start": start,
        "end": end,
    }
    resp = session.post(f"{BASE_URL}/user/calendar/my", data=payload, timeout=30)
    return resp.json()


def delete_meeting(cookie: str, meeting_id: str) -> dict:
    """删除自己的会议, meeting_id 为 list 输出中的会议 id"""
    session, ut = build_session(cookie)
    payload = {
        "user_token": ut,
        "id": str(meeting_id),
    }
    resp = session.post(f"{BASE_URL}/meeting/delete", data=payload, timeout=30)
    try:
        return resp.json()
    except ValueError:
        return {"status": resp.status_code, "raw": resp.text[:1000]}


def _load_cookie_file() -> str:
    """读取本地 cookie 文件,不存在/为空返回空串"""
    try:
        with open(COOKIE_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_cookie_file(cookie: str) -> None:
    """将登录态写入 cookie 文件"""
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write(cookie)


def _cookie_valid(cookie: str) -> bool:
    """轻量校验 cookie 是否仍有效(调一次只读接口,远快于重新登录)"""
    today = time.strftime("%Y-%m-%d")
    try:
        data = query_calendar(cookie, today, today)
        return bool(data.get("success"))
    except (ValueError, requests.RequestException):
        return False


def login_with_env() -> str:
    """
    获取登录态 cookie:
      1. 优先读当前目录 cookie 文件,有效则直接使用
      2. 缺失/失效则用 .env 账号重新登录并写回文件
    """
    cached = _load_cookie_file()
    if cached:
        try:
            extract_token_from_cookie(cached)  # 格式检查
            if _cookie_valid(cached):  # 服务端有效性检查
                print(f"[✓] 使用本地 cookie({COOKIE_FILE})")
                return cached
        except ValueError:
            pass
        print("[*] 本地 cookie 失效,重新登录...")
    else:
        print("[*] 未找到本地 cookie,开始登录...")

    username = os.environ.get("VMR_USERNAME", "")
    password = os.environ.get("VMR_PASSWORD", "")
    if not username or not password:
        raise SystemExit("[错误] .env 缺少 VMR_USERNAME / VMR_PASSWORD(参考 .env.example)")
    try:
        cookie = login(username, password)
    except LoginError as e:
        raise SystemExit(f"[✗] 自动登录失败: {e}") from e
    _save_cookie_file(cookie)
    print(f"[✓] 登录成功,已缓存到 {COOKIE_FILE}")
    return cookie


def main():
    parser = argparse.ArgumentParser(description="华东师大云视频会议系统预约工具")
    sub = parser.add_subparsers(dest="cmd", metavar="操作", required=True)

    p_list = sub.add_parser("list", help="查看我的会议")
    p_list.add_argument("--date", help="起始日期 YYYY-MM-DD(默认今天)")
    p_list.add_argument(
        "--days", type=int, default=7, help="查询天数(默认7;接口支持任意范围,0=仅查起始日当天)"
    )

    p_book = sub.add_parser("book", help="预约会议")
    p_book.add_argument("--topic", required=True, help="会议主题")
    p_book.add_argument("--date", required=True, help="会议日期 YYYY-MM-DD")
    p_book.add_argument("--time", required=True, help="开始时间 HH:MM")
    p_book.add_argument("--duration", type=int, default=120, help="时长(分钟,默认120)")
    p_book.add_argument("--password", default="", help="会议密码(不传=无密码)")

    p_del = sub.add_parser("delete", help="删除会议")
    p_del.add_argument("ids", help="会议 id(见 list 输出),多个用逗号分隔")
    args = parser.parse_args()

    cookie = login_with_env()
    try:
        token = extract_token_from_cookie(cookie)
        print(f"[✓] 已解析出 token: {token[:12]}...")
    except ValueError as e:
        print(f"[✗] cookie 解析失败: {e}")
        sys.exit(1)

    if args.cmd == "list":
        start = args.date or time.strftime("%Y-%m-%d")
        d = date.fromisoformat(start)
        end = start if args.days <= 0 else (d + timedelta(days=args.days)).isoformat()
        data = query_calendar(cookie, start, end)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        if data.get("success") and data.get("data"):
            print(f"\n共 {len(data['data'])} 场会议:")
            for m in data["data"]:
                mid = m.get("id")
                print(f"  - {m.get('start')} ~ {m.get('end')}  {m.get('title')}  (id={mid})")
        return

    if args.cmd == "delete":
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]
        print(f"[*] 正在删除 {len(ids)} 个会议: {ids}")
        ok = 0
        for mid in ids:
            result = delete_meeting(cookie, mid)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result.get("success"):
                print(f"[✓] 会议 {mid} 已删除")
                ok += 1
            else:
                print(f"[✗] 会议 {mid} 删除失败")
        if ok != len(ids):
            sys.exit(1)
        return

    # book
    print(
        f"[*] 正在预约: {args.topic} @ {args.date} {args.time} 时长{args.duration}分钟"
        f" 密码={'无' if not args.password else args.password}"
    )
    result = book_meeting(
        cookie, args.topic, args.date, args.time, duration=args.duration, password=args.password
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if result.get("success"):
        print("\n[✓] 预约申请已提交(需管理员审批)。可用 list 确认。")
    else:
        print("\n[✗] 预约失败,请检查参数或cookie是否过期")
        sys.exit(1)


if __name__ == "__main__":
    main()
