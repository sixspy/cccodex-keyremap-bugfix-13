# 实体键桥

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 实体键桥 |
| 目录 | `07-keyremap` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10938 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

一对多/多对一、生效窗、旧键只读、重映重放

## 避开

别做成 Feed 冲突归并；正式区不是 staging 换皮

## 互斥切口候选

- 冲突拒收整批 vs 进冲突区后重放
- 废弃旧键只读保留 vs 重写正式键
- 生效窗原子切换 vs 双窗按事件时裁

## 退役口径（已锁定 → 见 `ops/退役修复卡_2026-11-20.md`）

退役后重映走**甲路径：拒新映**。`POST /api/remap` 命中已退役映射即整笔拒绝（409），正式区零增，写拒映审计。退役、重映均占 `map_row` 锁，后到只记争用。

五处同态：退役台账 / 正式实体 / 拒映审计 / 互证页 / 映射册。只改徽章判失败。

| 接口 | 说明 |
|------|------|
| `POST /api/mappings/{id}/retire` | 退役映射（写台账，不另开退役台） |
| `POST /api/remap` | 入站重映（命中退役则拒映 + 审计） |
| `GET /api/retirement-ledger` | 退役台账 |
| `GET /api/remap-rejections` | 拒映审计 |
| `GET /api/lock-contention` | 抢写争用 |
| `GET /api/attestation` | 互证页，`consistent=true` 为同态 |

## 启动（一键）

`powershell
cd projects/07-keyremap
docker compose up -d --build
`

浏览器：http://127.0.0.1:10938/  
健康检查：GET http://127.0.0.1:10938/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10604
python -m pytest -q
`
