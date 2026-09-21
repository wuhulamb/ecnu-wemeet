#!/usr/bin/env python3
"""
华东师大云视频会议系统(vmr.ecnu.edu.cn)预约工具

用法(自动使用 .env 的 VMR_USERNAME / VMR_PASSWORD 登录):
    # 查看我的会议(--days 指定天数,默认7)
    uv run python booking.py list --days 30

    # 预约会议(无密码):2026-09-23 14:00 时长60分钟;--password 123456 设置密码
    uv run python booking.py book --topic "周会" --date 2026-09-23 --time 14:00 --duration 120

    # 删除会议(list 里查到的 id,多个用逗号分隔)
    uv run python booking.py delete 65464,65473

说明:
    - 登录态缓存于本地 cookie.txt(已 gitignore),失效时自动用 .env 账号重登
    - 预约提交后系统自动批准,book 命令直接打印新会议号
    - 不传 --password 则为无密码会议
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv

from login import VM_UA, LoginError, login

load_dotenv()  # 读取 .env(VMR_USERNAME / VMR_PASSWORD)

BASE_URL = "https://vmr.ecnu.edu.cn/api/v1"
COOKIE_FILE = "cookie.txt"  # 登录态缓存文件(已 gitignore)


class AuthExpired(Exception):
    """服务端返回 401,登录态已失效"""


def _check_auth(resp: requests.Response) -> None:
    """token 失效时服务端返回 HTTP 401,抛 AuthExpired 交由上层重登"""
    if resp.status_code == 401:
        raise AuthExpired


def extract_token_from_cookie(cookie: str) -> str:
    """从 login() 返回的 user_info JSON 中提取认证 token"""
    obj = json.loads(cookie)
    token = obj.get("token") if isinstance(obj, dict) else None
    if not token:
        raise ValueError("user_info 中未找到 token 字段")
    return token


def encrypt_token(token: str) -> str:
    """
    与前端一致的 user_token 加密算法:
      r = reverse( base64( base64( reverse( quote(token) + "_" + ts ) ) + "_" + ts ) )
      ts = 当前毫秒时间戳
    """
    a = str(int(time.time() * 1000))
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
            "User-Agent": VM_UA,
        }
    )
    return session, ut


def _post(session: requests.Session, path: str, payload: dict) -> dict:
    """向接口 POST 表单,统一做 401 检查与非 JSON 响应兜底"""
    resp = session.post(f"{BASE_URL}{path}", data=payload, timeout=30)
    _check_auth(resp)
    try:
        return resp.json()
    except ValueError:
        return {"status": resp.status_code, "raw": resp.text[:1000]}


def book_meeting(
    cookie: str,
    topic: str,
    meeting_date: str,
    time_slot: str,
    duration: int = 120,
    password: str = "",
) -> dict:
    """
    预约会议
      meeting_date: 'YYYY-MM-DD'
      time_slot:    'HH:MM'
      duration:     分钟(默认120)
      password:     空字符串=无密码
    """
    session, ut = build_session(cookie)

    payload = {
        "duration": str(duration),
        "meeting_date": meeting_date,
        "meeting_time": time_slot,
        "option_mute": "1",
        "option_jbh": "1",
        "option_h323": "0",
        "group_id": "2",
        "usage": "办公",
        "is_recurrent": "0",
        "recurrent_id": "0",
        "end_meeting_date": meeting_date,
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

    return _post(session, "/meeting/edit", payload)


def query_calendar(cookie: str, start: str | None = None, end: str | None = None) -> dict:
    """查询自己某时间段内的会议(只读接口,用于确认预约结果)"""
    session, ut = build_session(cookie)
    return _post(session, "/user/calendar/my", {"user_token": ut, "start": start, "end": end})


def format_meeting_number(number: str) -> str:
    """会议号按 3 位一组加连字符展示:175581502 → 175-581-502(从右往左分组,非数字则原样返回)"""
    digits = re.sub(r"\D", "", number)
    if not digits:
        return number
    return re.sub(r"(?<=\d)(?=(\d{3})+$)", "-", digits)


def parse_zoom_info(zoom_info: str | None) -> tuple[str, str]:
    """
    从列表接口返回的 zoom_info(如 '会议号:261610460<br>密码:738671')解析出 (会议号, 密码)。
    会议号统一按 3 位一组加连字符展示(如 261-610-460);无会议号/无密码时对应位置返回空串。
    """
    if not zoom_info:
        return "", ""
    zoom_info = zoom_info.replace("：", ":")  # 兼容全角冒号  # noqa: RUF001
    m = re.search(r"会议号:\s*(\d+)", zoom_info)
    p = re.search(r"密码:\s*(\d*)", zoom_info)
    return (format_meeting_number(m.group(1)) if m else ""), (p.group(1) if p else "")


def _disp_width(s: str) -> int:
    """计算字符串的终端显示宽度(中文/全角字符按 2 列计)"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _meeting_period(start_time: str, duration_hour: str) -> tuple[datetime, datetime] | None:
    """解析开始时间与时长('2小时'/'30分钟'),返回 (开始, 结束) datetime;无法解析返回 None"""
    m = re.match(r"(\d+)\s*(小时|分钟)?", duration_hour or "")
    if not m:
        return None
    try:
        start = datetime.strptime(start_time, "%Y-%m-%d %H:%M")  # noqa: DTZ007 服务端时间为 naive,仅用于展示
    except ValueError:
        return None
    if m.group(2) == "小时":
        delta = timedelta(hours=int(m.group(1)))
    else:
        delta = timedelta(minutes=int(m.group(1)))
    return start, start + delta


def _format_time_range(start: datetime, end: datetime) -> str:
    """展示会议时间:同天为 '2026-09-24(周四) 16:00 ~ 18:00',跨天则两端各带日期"""
    wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][start.weekday()]
    day = f"{start:%Y-%m-%d}({wd})"
    if start.date() == end.date():
        return f"{day} {start:%H:%M} ~ {end:%H:%M}"
    return f"{day} {start:%H:%M} ~ {end:%Y-%m-%d} {end:%H:%M}"


def format_meeting_card(meeting: dict, topic: str, number: str, pwd: str) -> str:
    """
    预约成功信息块:分隔线下排列 主题/时间/密码,末尾附 #腾讯会议 号。
    topic 用预订时填写的完整主题(list 接口会截断到 20 字符)。
    """
    start_raw = meeting.get("start_time") or ""
    period = _meeting_period(start_raw, meeting.get("duration_hour") or "")
    rows = [
        ("主题", topic),
        ("时间", _format_time_range(*period) if period else start_raw),
    ]
    if pwd:
        rows.append(("密码", pwd))
    body = "\n".join(f"{label}：{value}" for label, value in rows)  # noqa: RUF001
    tail = f"#腾讯会议：{number}" if number else "#腾讯会议: 待生成"  # noqa: RUF001
    width = max(_disp_width(line) for line in [*body.splitlines(), tail])
    return "─" * width + "\n" + body + "\n" + tail


def list_meetings(cookie: str) -> dict[int, dict]:
    """拉取我的全部会议(/meeting/list),返回 {会议id: 会议详情} 映射。
    详情中含 start_time/topic/zoom_info 等字段(zoom_info 含会议号与密码)。"""
    session, ut = build_session(cookie)
    obj = _post(session, "/meeting/list", {"user_token": ut})
    if not obj.get("success"):
        return {}
    items = (obj.get("data") or {}).get("data") or []
    return {int(m["id"]): m for m in items if m.get("id") is not None}


def find_new_meeting(cookie: str, before_ids: set[int], topic: str) -> dict | None:
    """
    book 后在会议列表中定位新预约的会议:
      1. 差集:book 前记录的 id 集合中不存在的 id(最可靠)
      2. 兜底:按主题匹配(列表接口会截断主题到 20 字符,故用 startswith)
    返回会议详情,找不到返回 None。
    """
    after = list_meetings(cookie)
    candidates = [m for mid, m in after.items() if mid not in before_ids]
    if len(candidates) == 1:
        return candidates[0]
    for m in after.values():  # candidates 已包含在 after 中,直接全量遍历即可
        if m.get("topic", "").startswith(topic[:20]):
            return m
    return None


def delete_meeting(cookie: str, meeting_id: str) -> dict:
    """删除自己的会议, meeting_id 为 list 输出中的会议 id"""
    session, ut = build_session(cookie)
    return _post(session, "/meeting/delete", {"user_token": ut, "id": str(meeting_id)})


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


def login_with_env(force: bool = False) -> str:
    """
    获取登录态 cookie:
      1. 默认优先读 cookie 文件(仅格式检查,不做服务端往返)
      2. 缺失/格式损坏/force=True 时用 .env 账号重新登录并写回文件
    服务端是否真的失效由接口 401 时自动重登兜底(见 call_with_auth)。
    """
    cached = "" if force else _load_cookie_file()
    if cached:
        try:
            extract_token_from_cookie(cached)
        except ValueError:
            cached = ""  # 格式损坏 → 重新登录
    if cached:
        print(f"[✓] 使用本地 cookie({COOKIE_FILE})")
        return cached
    if force:
        print("[*] cookie 已失效,重新登录...")
    else:
        print("[*] 未找到有效本地 cookie,开始登录...")

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


def call_with_auth(cookie_box: list[str], fn) -> object:
    """执行 fn(cookie_box[0]);若接口返回 401(token 超过 1 小时有效期),自动重新登录后重试一次。

    登录态用可变容器 cookie_box 持有:重登后把新 cookie 写回,后续调用直接复用,
    避免每个接口都拿旧 cookie 再触发一轮 401→重登。
    """
    try:
        return fn(cookie_box[0])
    except AuthExpired:
        print("[*] 登录态已失效,自动重新登录后重试...")
        cookie_box[0] = login_with_env(force=True)
        return fn(cookie_box[0])


def main():
    parser = argparse.ArgumentParser(description="华东师大云视频会议系统预约工具")
    sub = parser.add_subparsers(dest="cmd", metavar="操作", required=True)

    p_list = sub.add_parser("list", help="查看我的会议")
    p_list.add_argument("--date", help="起始日期 YYYY-MM-DD(默认今天)")
    p_list.add_argument(
        "--days", type=int, default=7, help="查询天数(默认7;接口支持任意范围,0=仅查起始日当天)"
    )

    p_book = sub.add_parser("book", help="预约会议")
    p_book.add_argument("--date", required=True, help="会议日期 YYYY-MM-DD")
    p_book.add_argument(
        "--topic",
        help="会议主题,支持 {YYYY-MM-DD} 占位符(缺省取 .env VMR_MEETING_TOPIC)",
    )
    p_book.add_argument(
        "--time",
        help="开始时间 HH:MM(缺省取 .env VMR_MEETING_TIME)",
    )
    p_book.add_argument("--duration", type=int, default=120, help="时长(分钟,默认120)")
    p_book.add_argument("--password", default="", help="会议密码(不传=无密码)")

    p_del = sub.add_parser("delete", help="删除会议")
    p_del.add_argument("ids", help="会议 id(见 list 输出),多个用逗号分隔")
    args = parser.parse_args()

    cookie = [login_with_env()]  # 可变容器:重登后由 call_with_auth 写回新 cookie

    if args.cmd == "list":
        start = args.date or time.strftime("%Y-%m-%d")
        d = date.fromisoformat(start)
        end = start if args.days <= 0 else (d + timedelta(days=args.days)).isoformat()
        data = call_with_auth(cookie, lambda c: query_calendar(c, start, end))
        # cookie 已在上一步刷新,此时 list_meetings 直接复用新 cookie,不再重复重登
        details = call_with_auth(
            cookie, list_meetings
        )  # 会议号/密码在 /meeting/list 的 zoom_info 中
        if data.get("success") and data.get("data"):
            meetings = data["data"]
            print(f"共 {len(meetings)} 场会议:")
            for m in meetings:
                mid = m.get("id")
                info = details.get(int(mid), {}) if mid is not None else {}
                number, pwd = parse_zoom_info(info.get("zoom_info"))
                extra = f"会议号:{number}" if number else "会议号:未生成"
                if pwd:
                    extra += f" 密码:{pwd}"
                s, e, title = m.get("start"), m.get("end"), m.get("title")
                print(f"  - {s} ~ {e}  {title}  (id={mid}, {extra})")
        else:
            print("查询失败:" + json.dumps(data, ensure_ascii=False))
        return

    if args.cmd == "delete":
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]
        print(f"[*] 正在删除 {len(ids)} 个会议: {ids}")
        ok = 0
        for mid in ids:
            result = call_with_auth(cookie, lambda c, m=mid: delete_meeting(c, m))
            if result.get("success"):
                print(f"[✓] 会议 {mid} 已删除")
                ok += 1
            else:
                print(f"[✗] 会议 {mid} 删除失败")
        if ok != len(ids):
            sys.exit(1)
        return

    # book
    topic = args.topic or os.environ.get("VMR_MEETING_TOPIC", "")
    time_slot = args.time or os.environ.get("VMR_MEETING_TIME", "")
    if not topic or not time_slot:
        parser.error("需要 --topic/--time,或 .env 配置 VMR_MEETING_TOPIC / VMR_MEETING_TIME")
    topic = topic.replace("{YYYY-MM-DD}", args.date).replace("{date}", args.date)
    print(
        f"[*] 正在预约: {topic} @ {args.date} {time_slot} 时长{args.duration}分钟"
        f" 密码={'无' if not args.password else args.password}"
    )
    before_ids = set(call_with_auth(cookie, list_meetings))  # book 前已有会议 id,用于定位新会议
    result = call_with_auth(
        cookie,
        lambda c: book_meeting(
            c,
            topic,
            args.date,
            time_slot,
            duration=args.duration,
            password=args.password,
        ),
    )

    if result.get("success"):
        # 定位新会议并查询会议号(审批通过/自动批准时立即生成,最多等待 ~9 秒)
        meeting = None
        for _ in range(3):
            meeting = call_with_auth(cookie, lambda c: find_new_meeting(c, before_ids, topic))
            if meeting and parse_zoom_info(meeting.get("zoom_info"))[0]:
                break
            time.sleep(3)
        if meeting:
            number, pwd = parse_zoom_info(meeting.get("zoom_info"))
            if number:
                line = f"[✓] 新会议 id={meeting.get('id')}, 会议号:{number}"
                if pwd:
                    line += f", 密码:{pwd}"
                print(line)
                print(format_meeting_card(meeting, topic, number, pwd))
            else:
                print(f"[*] 新会议 id={meeting.get('id')} 已提交,会议号待审批生成,可稍后 list 查询")
    else:
        print("\n[✗] 预约失败,请检查参数或cookie是否过期")
        sys.exit(1)


if __name__ == "__main__":
    main()
