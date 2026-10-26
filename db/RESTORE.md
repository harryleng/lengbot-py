# 本地库恢复指南

dump 文件：`db/lengbot_seed.dump`（远程 `lengbot` 库快照，custom 格式，7.9 MB，含 pgvector 数据）

> 为什么不用 Docker：这台机器 PATH 里没有 docker CLI，本地 `C:\PostgreSQL\15` 是现成的 PG 15.18 + pgvector 0.8.5，
> 直接用它更省事，且和远程 0.7.3 向下兼容。

---

## 第 1 步：启动本地 PostgreSQL（**必须你自己执行**）

原因：我在沙箱里启动的进程活不过当前会话，`pg_ctl start` 起来后会被回收。
你执行一次，之后就一直有。

**方式 A —— 注册成 Windows 服务（推荐，开机自启）**

管理员身份打开 CMD 或 PowerShell：

```bat
"C:\PostgreSQL\15\pgsql\bin\pg_ctl.exe" register -N postgresql-15 -D "C:\PostgreSQL\15\data" -S auto
net start postgresql-15
```

以后就是标准服务了，`net start/stop postgresql-15` 控制。

**方式 B —— 临时启动（重启后失效）**

```bat
"C:\PostgreSQL\15\pgsql\bin\pg_ctl.exe" -D "C:\PostgreSQL\15\data" -l "C:\PostgreSQL\15\data\startup.log" start
```

验证：

```bat
"C:\PostgreSQL\15\pgsql\bin\pg_isready.exe" -h 127.0.0.1 -p 5432
```

应该输出 `127.0.0.1:5432 - 接受连接`。

> 注意：`C:\PostgreSQL\15\data` 里已有你自己的库，本指南**只新建 `lengbot_py`**，不动任何现有数据库。

---

## 第 2 步：建空库并恢复

```bat
set PGPASSWORD=<你的 postgres 密码>
"C:\PostgreSQL\15\pgsql\bin\createdb.exe" -h 127.0.0.1 -p 5432 -U postgres -E UTF8 -T template0 lengbot_py
"C:\PostgreSQL\15\pgsql\bin\pg_restore.exe" -h 127.0.0.1 -p 5432 -U postgres -d lengbot_py --no-owner --no-privileges --verbose "C:\home\lengbot\lengbot-py\db\lengbot_seed.dump"
```

`-T template0` 很重要：template1 如果被改过（比如装了扩展）会导致建库失败。

`--no-owner --no-privileges` 是为了跳过「角色不存在」的报错——远程库的属主角色本地很可能没有。

### 可能出现的报错（都不影响使用）

| 报错 | 原因 | 处理 |
|---|---|---|
| `role "xxx" does not exist` | 远程库有专属角色 | 已用 `--no-owner` 规避 |
| `extension "vector" already exists` | 本地已装 | 忽略 |
| `schema "public" already exists` | 新库自带 | 忽略 |

判断是否真成功，看最后一行和下面第 3 步的校验。

---

## 第 3 步：校验

```bat
"C:\PostgreSQL\15\pgsql\bin\psql.exe" -h 127.0.0.1 -p 5432 -U postgres -d lengbot_py -c "SELECT count(*) AS sessions FROM chat_session;" -c "SELECT count(*) AS messages FROM message;" -c "SELECT extversion FROM pg_extension WHERE extname='vector';"
```

对照远程真实值（2026-10-23 快照）：

- `chat_session` ≈ 十几条
- `message` ≈ 182 条
- `vector` 扩展 = 0.8.5

数字对得上就说明恢复成功。

---

## 第 4 步：改 .env

```bash
cp .env.example .env
```

把 `DATABASE_URL` 里的密码换成你本地 postgres 的真实密码。

---

## 恢复之后的数据边界

- 这份库是**快照**，之后本地改动不会同步回远程，反之亦然。
- 本地可以随便写（练手嘛），**永远不要**把 `.env` 指回 `localhost:15432` 做写操作。
- 想重新来一遍：`dropdb lengbot_py` 然后重跑第 2 步。
- 想更新快照：重新 dump 一次即可（命令见 `README.md`）。

---

## 附：连不上远程库时怎么判断

典型报错：

```
asyncpg.exceptions.InvalidAuthorizationSpecificationError:
no pg_hba.conf entry for host "27.38.230.157", user "postgres",
database "lengbot_py", no encryption
```

**这不是密码错、也不是代码问题**，是 PostgreSQL 的 `pg_hba.conf` 里没有匹配这个
客户端 IP 的规则。判断方法：用 psql 连同一个库——

```bat
"C:\PostgreSQL\15\pgsql\bin\psql.exe" -h localhost -p 15432 -U postgres -d lengbot_py -c "SELECT 1"
```

- **psql 也报同样错误** → IP 白名单问题（大概率）
- **psql 能连、asyncpg 不能** → SSL 问题（asyncpg 默认不加密，若 pg_hba 写的是
  `hostssl` 就匹配不上，需要给连接加 `ssl=require`）

### 本机出口 IP 是动态的

实测同一个沙箱在不同时刻的出口 IP：`163.125.124.144` → `27.38.230.157` →
`27.38.148.139` → `36.36.248.183`。运营商的动态地址池，按单个 IP 加白名单会时通时不通。

三种解法，按推荐度排：

1. **按网段放行**（改 `pg_hba.conf`，服务端操作）
   ```
   host all all 27.38.0.0/16 scram-sha-256
   host all all 163.125.0.0/16 scram-sha-256
   host all all 36.36.0.0/16 scram-sha-256
   ```
   比 `0.0.0.0/0` 安全，且覆盖动态变化。改完 `SELECT pg_reload_conf();`

2. **SSH 隧道固定本地端口**（最安全，不用改服务端）
   ```bat
   ssh -N -L 15432:127.0.0.1:5432 user@localhost
   ```
   然后 `.env` 指向 `127.0.0.1:15432`，出口对本机恒定。

3. **改用本地副本库**（`RESTORE.md` 前半部分）—— 完全不受网络影响，适合长期练手。
