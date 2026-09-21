# ecnu-wemeet

华东师大云视频会议系统（vmr.ecnu.edu.cn）纯 HTTP 自动登录与预约工具。

无需浏览器/Playwright：登录链路完整复刻前端实现（OAuth2 → CAS 统一认证 → 换票），
密码加密、token 混淆算法均与前端一致；会议预约/查询/删除直接调 vmr API。

## 功能

| 命令 | 说明 |
|---|---|
| `booking.py list`   | 查看自己的会议（`--days` 指定天数，默认 7，接口支持任意范围） |
| `booking.py book`   | 预约会议（默认无密码、时长 120 分钟、办公用途） |
| `booking.py delete` | 删除会议（未开始的会议，id 见 list 输出，支持逗号批量） |
| `login.py`          | 登录调试入口（仅打印登录结果，不生成任何文件） |

## 安装

需要 Python ≥ 3.12 与 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
```

## 配置

```bash
cp .env.example .env   # 然后编辑填入学号密码
```

```ini
VMR_USERNAME=学号
VMR_PASSWORD=密码
```

`.env` 含账号密码，已被 `.gitignore` 排除，切勿提交或分享。

## 使用

```bash
# 查看会议(今天起 30 天)
uv run python booking.py list --days 30

# 预约会议:周会 @ 2026-09-23 14:00,时长 120 分钟
uv run python booking.py book --topic 周会 --date 2026-09-23 --time 14:00

# 预约并设置密码
uv run python booking.py book --topic 周会 --date 2026-09-23 --time 14:00 --password 123456

# 自定义时长
uv run python booking.py book --topic 周会 --date 2026-09-23 --time 14:00 --duration 60

# 删除会议(多个用逗号分隔)
uv run python booking.py delete 65464,65473

# 登录调试(可选)
uv run python login.py
```

会议预约后需管理员审批（系统行为）。

## 工作原理

**登录链路**（`login.py`，纯 requests + cryptography）：

```
api.ecnu.edu.cn/oauth2/authorize        OAuth2 授权,建立会话
  → portal1 / cas/login → sso 登录页    统一认证(CAS)
  → 提取 login-croypto(AES 密钥) 与 login-page-flowkey(执行流)
  → 提交表单:密码 AES-128-ECB/PKCS7 加密,验证码 payload 传 "{}"
  → CAS ticket → OAuth code → vmr /sign-in
  → POST /api/v1/user/sso               换取用户信息,token 只存内存
```

- 验证码策略：不破解/绕过；若服务端要求验证码（风控触发，正常使用极少）则报错退出
- 登录态不落盘，`booking.py` 每次运行自动登录

**会议操作**（`booking.py`）：
- 复用前端同款 `user_token` 混淆算法（`encrypt_token`）签名请求
- 预约 `POST /api/v1/meeting/edit`、查询 `POST /api/v1/user/calendar/my`、
  删除 `POST /api/v1/meeting/delete`

## 开发

```bash
uv run ruff check .          # lint
uv run ruff format --check . # 格式
```

规范配置见 `pyproject.toml`（`[tool.ruff]`），规则集：E/F/I/UP/B/DTZ/RUF/EXE。

## 安全提示

- `.env`、用户登录态等敏感信息严禁提交/分享
- 脚本仅适用于低频率个人使用；高频自动化可能触发统一认证风控