# 市场部企微 H5（market_h5）

华福/smj 场务市场部「企微内 H5 工作台」：零依赖 Python 标准库实现（`http.server` + `pymysql`），
部署为一台轻量测试机上的 systemd 服务，经 nginx 由 `https://huafu.china-ktbm.com/statics/api/mh5/` 对外开放，
在企微 OA 移动端 iframe / 独立入口中使用。

## 功能

- **首页/待办**：周计划、跟进录入、指标看板
- **9 项绩效指标**实时聚合 + 当月得分与加分流水（trace 算法 v6.2，周计划匹配度口径）
- **跟进录入弹窗**（v2，按原「外勤打卡」表单复现）：
  - 关联对象三选一：客户 / 项目 / 商机（选择面板搜索，项目带出关联客户、商机带出业主单位）
  - 跟进信息：拜访目的、本次达成节点（项目阶段变更）、后续安排、下次跟进时间、有效跟进、意向、跟进反馈
  - 阶段进度快照 5 组多选（前期沟通/初步勘察/方案编制/内部立项/招投标，37 选项）
  - 业务洞察 6 字段 + 项目状态变更（是/否 + 目标状态 0-7）
  - 写路径：`follow_up_record` 全字段落列；外勤打卡同步写 `chcks`；
    选项目时回写 `project_info.follow_up_state / follow_up_arrangement / project_phase`，
    阶段变化时补 `project_phase_logs`（best-effort，失败不影响保存）
  - 计分口径：外勤打卡 +拜访量分；本月**首次**跟进某项目才 +10/目标（去重口径）
- **免登链路**：URL uid > 企微 OAuth（CorpID+Secret，服务端 code→userid→users.wx_id 映射）> OA iframe `$userInfo`
- **OA 深链跳转**（oa_deep_url v8）：顶层 openId + hash 路由直参，可用 `MH5_OA_JUMP_STYLE=goto` 回退旧制式

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/me?uid=` | 用户信息 + 指标 + 计分规则 |
| GET | `/api/todo?uid=` | 周计划 + 跟进列表 |
| GET | `/api/score?uid=` | 当月得分/9 项指标 |
| GET | `/api/follow-ups?uid=` | 跟进记录 |
| POST | `/api/follow-up` | **唯一写接口**：新增跟进（+外勤双写 chcks） |
| GET | `/api/pickers?uid=&q=` | 弹窗三图标候选（客户/项目/商机聚合） |
| GET | `/api/projects` `/api/flows` `/api/flow-detail` `...` | 详情/流程用只读接口 |
| GET | `/api/wx-login` → `/api/wx-callback` | 企微 OAuth（免登） |

全部生产/测试库访问均为只读，**唯一写接口即上表 POST**。

## 配置（环境变量，一律不硬编码凭证）

`MH5_DB_HOST/PORT/USER/PASSWORD/NAME` · `MH5_WX_CORP/AGENT/SECRET` ·
`MH5_LANDING` · `MH5_API_KEY`（X-MH5-Key 防跨用户）· `MH5_WHITELIST[_WXIDS]`（调试选账号白名单）·
`MH5_OA_JUMP_STYLE`

systemd 单元示例（/etc/systemd/system/market-h5.service）以 `Environment=` 注入上述变量。
nginx 将 `location /statics/api/mh5/` 反代至 `127.0.0.1:8766`（见 `nginx.conf`）。

## 部署

`deploy2.py`：上传 `server.py + index.html` 至 `/opt/market_h5`，`sudo systemctl restart market-h5`。
SSH 凭证运行时从本机外部文件读取，不写入仓库。

## 文档

- `项目交接文档.md` —— 架构 / 表字段对照 / 已知坑（往下 21 条）
- `项目状态报告_2026-10-22.md` —— 接口实测与验收记录
- `trace_score_v6.2_周计划匹配度口径修订.md` —— 计分口径溯源

> ⚠️ 数据库为祥泰 OA 生产副本与生产库本身，请严格遵守：测试库可写实验，生产库只读。
