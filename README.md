# ecnu-wemeet

华东师大云视频会议系统（vmr.ecnu.edu.cn）纯 HTTP 自动登录与预约工具。

无需浏览器/Playwright，登录链路与密码加密、token 算法均复刻前端实现。

## 安装与配置

需要 Python ≥ 3.12 与 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync
cp .env.example .env   # 填入 VMR_USERNAME(学号) / VMR_PASSWORD(密码)
```

## 使用

```bash
# 查看会议(今天起 7 天,--days 可改,支持任意范围)
uv run python booking.py list

# 预约会议(主题/时间取 .env 配置;--topic/--time/--password/--duration 可覆盖)
uv run python booking.py book --date 2026-09-24

# 删除会议(多个用逗号分隔;已完成会议不可删)
uv run python booking.py delete 65464,65473
```

登录态缓存于当前目录 `cookie.txt`(已 gitignore):首次运行后直接复用,失效自动重新登录。

## 开发

```bash
uv run ruff check .         # lint
uv run ruff format --check . # 格式
```