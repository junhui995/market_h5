#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场部企微 H5 · 后端（零依赖 http.server + pymysql）

职责：
  - /api/me            解析当前登录人（OA iframe：前端读 localStorage.$userInfo 后带 uid 来）
  - /api/todo          待办：本月考核要求转成的任务清单 + 我的周计划 + 待跟进
  - /api/week-plan     周计划主表 + 明细（上午/下午客户框）
  - /api/follow-ups    我的跟进记录（最近）
  - /api/flow          流程页：商机 / 项目 两分栏列表，每行带「生命条」实时阶段
  - /api/flow-detail   单条商机/项目详情：信息面板 + 关联流程 + 生命条节点 + 下阶段操作
  - /api/performance   我的：当月 9 项绩效指标实时进度（只读聚合，不写 achievements）
  - /api/pickers       跟进弹窗「客户/项目/商机」图标选择器候选数据（只读，支持搜索）
  - POST /api/follow-up 新增跟进（写 follow_up_record；若带现场打卡则同步写 chcks 计绩效）

说明：
  - 生命条 linkage 沿用 project_lifecycle_app 已验证机制：
        business 表记录 (pk=id, fk→project_info) 提交时生成 flow_procs 实例
        flow_procs.data_id = business 记录 id，flow_procs.model_key = 业务表 model_key
        故阶段 = f(business 表 fk→project 的记录的 flow_procs.state)
  - 大节点口径（用户 2026-10-20 定义）：
        商机 → 立项(前期评估评审&初评-经济效益评估 都通过) → 项目
              → 投标(投标文件申请&标前-经济效益评估 都通过 & bidding_documents.whether_or_not_winning_id=1)
              → 项目销售合同(完成) → 完成
  - 写库仅 follow_up_record / chcks 两处，且按用户确认的口径（外勤打卡同步写 chcks 计拜访绩效）。
  - 防 IDOR：所有个人数据查询强制用服务端解析的 uid，不接受前端自由指定他人 id。
"""

import os
import sys
import json
import re
import traceback
import threading
from datetime import datetime, date, timedelta
from decimal import Decimal

import pymysql
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
import urllib.request
from urllib.parse import urlparse, parse_qs

# ============================================================
# 数据库配置：一律经环境变量注入（systemd Environment=），
# 不在本文件硬编码任何凭证；未配置时启动报错提示。
# ============================================================
DB_HOST = os.getenv("MH5_DB_HOST", "")
DB_PORT = int(os.getenv("MH5_DB_PORT", "3306"))
DB_USER = os.getenv("MH5_DB_USER", "")
DB_PASSWORD = os.getenv("MH5_DB_PASSWORD", "")
DB_NAME = os.getenv("MH5_DB_NAME", "")

# 接口鉴权 Key（防 IDOR / 跨用户读取的纵深防御）：生产环境务必设置。
# 前端需在请求头 X-MH5-Key 携带；为空则关闭校验（仅限开发）。
MH5_API_KEY = os.getenv("MH5_API_KEY", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 市场部筛选口径（已连库查实 2026-10-20：if_marketing_department='是' 共 408 人）
MARKET_FILTER = "if_marketing_department = '是'"

# 老 OA 移动端 SPA 深链底座（制式与 flow_engine/flow_ui 的 jump_url 同源，
# 2026-10 已在生产实证：待办点击即落到对应流程处理页，openId 免登）
OA_MOBILE_BASE = os.getenv(
    "MH5_OA_MOBILE_BASE", "https://huafu.china-ktbm.com/oa-mobile-test/").rstrip("/") + "/"

# ============================================================
# 企微免登（OAuth，与 flow_ui 同链路：/api/wx-login → 企微静默授权
# snsapi_base → /api/wx-callback → code 换 userid → users.wx_id 查 uid）
# corpid/agentid/secret 经 unit 环境变量注入（不落代码）。密钥为空则免登关闭。
# ============================================================
WX_CORP = os.getenv("MH5_WX_CORP", "").strip()
WX_AGENT = os.getenv("MH5_WX_AGENT", "1000015").strip()
WX_SECRET = os.getenv("MH5_WX_SECRET", "").strip()
LANDING = os.getenv("MH5_LANDING", "https://huafu.china-ktbm.com").rstrip("/")
MH5_HREF = LANDING + "/statics/api/mh5/"

# ============================================================
# 写入审计（2026-10-23 生产部署启用）：
#   每一笔写（INSERT/UPDATE）落 JSONL，含行 id + 写入值 + 更新前值，
#   供 rollback.py 依据"审计行"反推 SQL 撤销脏数据。
#   文件：{MH5_AUDIT_DIR}/write_audit_YYYYMMDD.jsonl
# ============================================================
AUDIT_DIR = os.getenv("MH5_AUDIT_DIR", "/opt/market_h5/audit")
_AUDIT_LOCK = threading.Lock()


def _audit(req, uid, name, acts):
    """acts: [{"action":"insert","table":t,"id":rowid,"row":{...}},
              {"action":"update","table":t,"id":rowid,"before":{...},"after":{...}}]"""
    if not acts:
        return
    entry = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "req": req, "uid": uid, "name": name, "acts": acts}
    try:
        os.makedirs(AUDIT_DIR, exist_ok=True)
        path = os.path.join(AUDIT_DIR, "write_audit_%s.jsonl" %
                            date.today().strftime("%Y%m%d"))
        with _AUDIT_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        sys.stderr.write("mh5 audit: req=%s uid=%s acts=%s\n"
                         % (req, uid, ",".join(a["action"] + ":" + a["table"] for a in acts)))
    except Exception as e:
        sys.stderr.write("mh5 audit WRITE-FAILED(写库已提交，审计留痕失败): %s\n" % str(e)[:200])

# 调试账号切换（devbar）白名单：仅这些人可见选账号栏（用户 2026-10-20 指定）。
# 姓名按包含匹配（库里显示名常带部门前缀），wx_id 精确兜底（三人事先连库锁定）。
WHITELIST_NAMES = [s.strip() for s in os.getenv(
    "MH5_WHITELIST", "陈俊惠,朱衡新,王亚").split(",") if s.strip()]
WHITELIST_WXIDS = [s.strip() for s in os.getenv(
    "MH5_WHITELIST_WXIDS", "SZHYJZXCJH,SZHZHX,XZRSBWANGYA").split(",") if s.strip()]


def _in_whitelist(name, wxid):
    return any(n in (name or "") for n in WHITELIST_NAMES) or (wxid or "") in WHITELIST_WXIDS

_WX_TOKEN = {"tk": "", "exp": 0.0}
_NONCES = set()
_NONCE_LOCK = threading.Lock()


def _new_nonce():
    n = os.urandom(8).hex()
    with _NONCE_LOCK:
        _NONCES.add(n)
    return n


def _pop_nonce(n):
    with _NONCE_LOCK:
        if n in _NONCES:
            _NONCES.discard(n)
            return True
    return False


def _wx_token():
    """gettoken，内存缓存至过期前 5min（同 flow_ui）。失败抛 RuntimeError。"""
    import time
    now = time.time()
    if _WX_TOKEN["tk"] and now < _WX_TOKEN["exp"]:
        return _WX_TOKEN["tk"]
    if not (WX_CORP and WX_SECRET):
        raise RuntimeError("企微配置缺失（MH5_WX_CORP/MH5_WX_SECRET 未设置）")
    url = ("https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=%s&corpsecret=%s"
           % (urllib.parse.quote(WX_CORP, safe=""), urllib.parse.quote(WX_SECRET, safe="")))
    try:
        r = json.loads(urllib.request.urlopen(url, timeout=8).read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError("gettoken 失败: %s" % str(e)[:150])
    if r.get("errcode") not in (0, None):
        raise RuntimeError("gettoken errcode=%s errmsg=%s" % (r.get("errcode"), r.get("errmsg")))
    _WX_TOKEN["tk"] = r["access_token"]
    _WX_TOKEN["exp"] = now + int(r.get("expires_in", 7200)) - 300
    return _WX_TOKEN["tk"]


def h_wx_login(self):
    """GET /api/wx-login：302 到企微授权（微信内置/企微）或 PC 扫码页。"""
    if not WX_SECRET:
        self._send_json(404, {"success": False, "message": "企微免登未启用（缺 MH5_WX_SECRET）"})
        return
    redirect_uri = MH5_HREF + "api/wx-callback"
    state = _new_nonce()
    ua = (self.headers.get("User-Agent") or "")
    if "wxwork" in ua or "MicroMessenger" in ua:
        # 企微内置：应用内静默授权（snsapi_base），登录人无感
        url = ("https://open.weixin.qq.com/connect/oauth2/authorize?appid=%s&redirect_uri=%s"
               "&response_type=code&scope=snsapi_base&state=%s&agentid=%s#wechat_redirect"
               % (urllib.parse.quote(WX_CORP, safe=""), urllib.parse.quote(redirect_uri, safe=""),
                  state, urllib.parse.quote(WX_AGENT, safe="")))
    else:
        # PC/其他浏览器：官方全页扫码
        url = ("https://open.work.weixin.qq.com/wwopen/sso/qrConnect?appid=%s&agentid=%s"
               "&redirect_uri=%s&state=%s"
               % (urllib.parse.quote(WX_CORP, safe=""), urllib.parse.quote(WX_AGENT, safe=""),
                  urllib.parse.quote(redirect_uri, safe=""), state))
    self.send_response(302)
    self.send_header("Location", url)
    self.send_header("Cache-Control", "no-store")
    self.end_headers()


def h_wx_callback(self, q):
    """GET /api/wx-callback?code&state：换 userid → users.wx_id 查 uid → 回 H5。"""
    code = (q.get("code") or [None])[0]
    state = (q.get("state") or [""])[0]
    if not code:
        self._send_json(400, {"success": False, "message": "回调缺 code（未授权/取消授权）"})
        return
    if not _pop_nonce(state):
        self._send_json(400, {"success": False, "message": "登录会话过期，请重新进入应用"})
        return
    try:
        tk = _wx_token()
    except RuntimeError as e:
        self._send_json(502, {"success": False, "message": str(e)})
        return
    try:
        url = ("https://qyapi.weixin.qq.com/cgi-bin/user/getuserinfo?access_token=%s&code=%s"
               % (tk, urllib.parse.quote(code, safe="")))
        r = json.loads(urllib.request.urlopen(url, timeout=8).read().decode("utf-8"))
    except Exception as e:
        self._send_json(502, {"success": False,
                              "message": "企微换 userid 失败: %s" % str(e)[:150]})
        return
    if r.get("errcode") not in (0, None):
        self._send_json(400, {"success": False,
                              "message": "企微 code 校验失败: %s" % json.dumps(r, ensure_ascii=False)[:200]})
        return
    # 新版 auth/getuserinfo 返回 userid；旧版 user/getuserinfo 返回 UserId（flow_ui 实测口径）
    userid = r.get("userid") or r.get("UserId")
    if not userid:
        self._send_json(401, {"success": False,
                              "message": "未取到企微 userid(响应:%s)" % json.dumps(r, ensure_ascii=False)[:200]})
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM users WHERE wx_id=%s AND deleted_at IS NULL "
                        "ORDER BY id LIMIT 1", (userid,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        self._send_json(401, {"success": False,
                              "message": "企微账号 %s 未关联到 OA 用户（请管理员在后台维护 wx_id）" % userid})
        return
    uid = str(int(row["id"]))
    # 登录解析留痕（问题1 类排查：企微 userid → OA uid）
    try:
        sys.stderr.write("wx-login-resolve: wx_userid=%s -> uid=%s (%s)\n"
                         % (userid, uid, row.get("name", "")))
    except Exception:
        pass
    self.send_response(302)
    self.send_header("Location", "%s?uid=%s" % (MH5_HREF, uid))
    self.send_header("Cache-Control", "no-store")
    self.end_headers()

# 下阶段操作按钮的深链构造。移动端 model-page 路由读 $route.query：
#   pageMode=add/detail/handle、modelKey、flowKey、processId、dataId、clientType
def oa_deep_url(open_id, page_mode, model_key=None, flow_key=None,
                proc_id=None, data_id=None):
    """
    深链 v8（2026-10-22 重制，实测修正）：

    实测事实（.tmp_deep_diag*.py，oa-mobile bundle + 后端行为）：
      1. oa-mobile 后端拦截一切入口请求，必须带 openId（或 miniOpenId），
         服务端按 openId 注入 localStorage.$userInfo 后才放行 SPA——openId
         即免登钥匙，并决定 OA 侧登录身份（须传数据属主的 wx_id）。
      2. SPA 根组件对顶层 query 带 goto=x 的处理是：把 gotoparams 解出的
         参数 + 硬编码 pageMode=handle/dataMode=handle push 进路由——
         即便 gotoparams 里写了 pageMode=add 也会被硬编码覆盖，
         而其余裸放外层的 modelKey/flowKey/processId 全部丢弃 → 进
         model-page 时零参数 → 页面报错（用户 2026-10-22 实机命中）。

    故 v8 用「顶层 openId 免登 + hash 路由直带全部参数」，pageMode 得以保真：
        base?openId=#ID#/model-page?clientType=mobile&pageMode=add&modelKey=..&flowKey=..
    旧制式（仅 handle 场景、OA 待办同款）可经 MH5_OA_JUMP_STYLE=goto 回滚。
    """
    if os.getenv("MH5_OA_JUMP_STYLE", "hash") == "goto":
        core = ["clientType=mobile", "pageMode=" + page_mode]
        q = {"openId": str(open_id or "")} if open_id else {}
        q["goto"] = "model-page"
        q["gotoparams"] = "undefined"
        if model_key:
            core.append("modelKey=" + urllib.parse.quote(str(model_key), safe=""))
            q["modelKey"] = model_key
        if flow_key:
            core.append("flowKey=" + urllib.parse.quote(str(flow_key), safe=""))
            q["flowKey"] = flow_key
        if proc_id:
            core.append("processId=%d" % proc_id)
            q["processId"] = proc_id
        if data_id:
            core.append("dataId=%d" % data_id)
            q["dataId"] = data_id
        parts = ["%s=%s" % (k, urllib.parse.quote(str(v), safe="")) for k, v in q.items()]
        return "%s?%s#/%s?%s" % (OA_MOBILE_BASE, "&".join(parts),
                                 "model-page", "&".join(core))
    # hash 制式（默认）
    q = {}
    if open_id:
        q["openId"] = str(open_id)
    core = ["clientType=mobile", "pageMode=" + page_mode]
    if model_key:
        core.append("modelKey=" + str(model_key))
    if flow_key:
        core.append("flowKey=" + str(flow_key))
    if proc_id:
        core.append("processId=%d" % proc_id)
    if data_id:
        core.append("dataId=%d" % data_id)
    head = ("openId=" + urllib.parse.quote(str(open_id), safe="")) if open_id else ""
    return "%s?%s#/%s?%s" % (OA_MOBILE_BASE, head, "model-page",
                             urllib.parse.quote("&".join(core), safe="=&"))

# 总监硬编码（绩效全景 §10.1 / §6.1）
DIRECTORS = [1826233891, 3151, 1826234046]
# 特殊拜访目标人员（绩效全景 §5.3）：目标 20 次、系数 0.75
SPECIAL_VISIT = [1826233891, 3151, 1826234046, 1826234138, 1826234209, 1826234023, 3192,
                 1826234222, 1826234257, 1826234230, 1826234243, 1826234234, 1826234255,
                 1826234218, 1826234240, 3553]

# 流程表 → 项目外键列 / model_key（已连库查实）
FLOW_DEFS = {
    "assessment_review":          {"fk": "name_id",           "mk": "assessment_review"},
    "economic_benefit_evaluation":{"fk": "project_name_id",   "mk": "economic_benefit_evaluation"},
    "bid_economic_evaluation":    {"fk": "project_name_id",   "mk": "bid_economic_evaluation"},
    "bidding_documents":          {"fk": "project_name_id",   "mk": "bidding_documents"},
    "contract_order":             {"fk": "related_projects_id", "mk": "contract_order"},
}

WRITE_LOCK = threading.Lock()


# ============================================================
# 字段中文标签（前端详情面板：英文 key 在上、中文名在下）
# 数据来源：OA 表单设计器（bidding_documents / assessment_review /
#           contract_order 三张表单截图）+ project_info / sales_opportunities 真实列
# ============================================================
PROJECT_LABELS = {
    "project_name": "项目名称", "item_no": "项目编号",
    "tender_number_purchase_number": "招标编号/采购编号",
    "correlation_information": "关联信息（客户）",
    "customer_contact": "客户联系人", "customer_contact_phone_number": "客户联系电话",
    "recommended_unit_service": "推荐单位（服务商）",
    "recommended_unit_contacts": "推荐单位联系人",
    "recommend_unit_contact_phone_n": "推荐单位联系电话",
    "project_overview": "项目概况", "content": "内容",
    "operation_mode": "运营模式", "way": "技术方式",
    "project_type": "项目类型", "belongs_to_regional": "所属大区",
    "operating_regions": "操作大区", "project_address": "项目地址",
    "project_time": "立项时间", "members_of_project": "项目成员",
    "project_size": "项目规模", "layer_number_of_the_project": "项目层数",
    "marketing_director": "市场负责人",
    "operations_follow_up_people": "运营部跟进人",
    "bid_bond_state": "投标保证金状态", "bid_bond": "投标保证金",
    "tender_production_status": "标书制作状态",
    "service_period": "服务期限", "deadline_for_service": "服务截止期限",
    "winning_amount": "中标金额", "sub_contract_sum": "分包金额",
    "project_flow_state": "项目结转状态", "service_providers": "服务商",
    "cumulative_received": "累计已付",
    "accumulation_of_make_out_an_in": "累计开票",
    "cumulative_make_out_an_invoice": "累计收票",
    "cumulative_bill_to": "累计回款",
    "not_receivable_amount": "未回款金额",
    "is_expected_to_yield": "预计收益率", "real_time_yields": "实际收益率",
    "contract_states": "合同状态", "owner_of_fixed_telephone": "业主固定电话",
    "follow_up_state": "跟进状态",
    "did_not_follow_up_creation_tim": "未跟进创建时间",
    "associated_opportunities": "关联商机",
    "whether_to_automatically_gener": "是否自动生成机会",
    "estimated_amount": "预计金额",
    "project_sales_contract_number": "项目销售合同编号",
    "contract_type": "签约类型", "whether_to_renew": "能否续签",
    "number_of_tasks": "任务数量", "response_time": "响应时长",
    "transfer_instructions": "流转说明", "source_of_renewal": "续签来源",
    "is_expected_in_bid_opening": "预计开标时间",
    "new_expected_in_bid_opening": "预计开标时间（新）",
    "belonging_to_company": "归属公司",
    "payment_request_amount": "付款申请金额", "parent_project_source": "父项目来源",
    "related_reporting_project": "关联报备项目",
    "reporting_customer": "关联外联客户", "reporting_manger": "报备负责人",
    "project_process_status": "项目阶段", "external_clients": "是否有外联客户",
    "necessary_establish_file": "是否需要建档", "synchronized_review": "同步审核",
    "area": "面积", "energy_consumption": "能耗（年）",
    "quality": "性质", "property_rights": "产权性质", "remark": "附件",
    "decision_making_chain": "决策链", "related_sales": "关联商机",
    "scheme_state": "方案状态", "evaluation": "经济测算表",
    "follow_up_arrangement": "跟进结果", "whether_abandon": "是否放弃",
    "corresponding_server_address": "对应服务器地址", "server_code": "对应服务器code",
    "expected_bid_date": "预计中标时间", "project_phase": "项目阶段状态",
    "tender_intention_publicity_time": "招标意向公示时间",
    "tender_hanging_time": "招标挂网时间",
    "resource_liaison_job_level": "资源对接人职级",
    "expected_contract_duration": "预计合同年限",
    "expected_signing_date": "预计签约日期",
    "latitude_and_longitude": "经纬度",
    "creator": "创建人", "created_at": "创建时间", "updated_at": "更新时间",
}

OPP_LABELS = {
    "theme": "商机名称", "corresponding_to_customer": "业主单位",
    "corresponding_project": "对应项目", "opportunity_number": "商机编号",
    "needs_of_customers": "客户需求", "cc_object": "抄送对象",
    "attachment": "附件", "note": "备注", "follow_up_records": "跟进记录",
    "amount": "预期金额(万/年)", "source": "来源",
    "related_projects": "关联项目", "size_of_the": "规模",
    "service_providers": "服务商", "provide_people": "提供人",
    "find_time": "发现时间", "chances_of_success": "成功几率",
    "profile": "概况", "all_people": "相关人员", "content": "内容",
    "type": "类型", "contact": "联系人",
    "contact_phone_number": "联系电话", "belongs_to_regional": "所属大区",
    "operation_mode": "运营模式", "whether_to_project": "是否转项目",
    "expected_amount": "预期金额",
    "related_reporting_project": "关联报备项目",
    "reporting_customer": "关联外联客户",
    "business_opportunity_process_status": "商机阶段",
    "external_clients": "是否有外联客户",
    "opportunity_status": "商机状态",
    "customer_contact_phone_number": "客户联系电话",
    "customer_contact_person": "客户联系人",
    "whether_abandon": "是否放弃", "quality": "性质",
    "creator": "创建人", "created_at": "创建时间", "updated_at": "更新时间",
}

# 投标文件申请表（OA 表单设计器 modelKey: bidding_documents）
BIDDING_LABELS = {
    "project_name": "项目名称", "belongs_to_regional": "所属大区",
    "marketing_director": "市场负责人", "bid_opening_way": "开标方式",
    "bid_opening_date": "开标日期", "project_price": "项目限价(万)",
    "limit_that": "限价说明", "budget_level": "预算等级",
    "project_type": "项目类型", "belonging_to_unit": "归属单位",
    "expect_time": "期望封标时间", "license_s_name": "被授权人姓名",
    "bid_opening_type": "开标类型", "results_through": "业绩补录",
    "interested_suppliers": "意向供应商",
    "qualification_certificate": "资质证书",
    "qualification_certificate_deduction": "资质证书扣分",
    "authorize": "空调厂家授权书",
    "authorize_deduction": "空调厂家授权书扣分",
    "scoring": "评分项（人员、业绩）",
    "scoring_deduction": "评分项（人员、业绩）扣分",
    "special": "其他特殊要求",
    "whether_tender_or_not": "是否需要做标书",
    "service_life": "合同年限", "owner_unit": "客户名称",
    "project_budget": "预估合同总额(万元)",
    "special_deduction": "其他特殊要求扣分",
    "special_introduction": "其他特殊要求说明",
    "bidding_instructions": "投标说明", "scoring_criteria": "评分标准",
    "bid_bond": "投标保证金", "performance_bond": "履约保证金",
    "winning_bid_service_fee": "中标服务费",
    "twenty_notes": "小于20天特殊说明", "budget_yield": "预算收益率",
    "whether_or_not_winning": "是否中标", "bidding_date": "中标日期",
    "winning_amount": "中标金额(万)", "annual_trust_amount": "年托管金额",
    "gross_profit_margin": "毛利率", "cause_analysis": "原因分析",
    "bid_winning_notice": "中标证明文件",
    "bid_manager": "招投标经理审批通过",
    "whether_energy_trusteeship_project": "是否为能源托管项目",
    "final_review_result": "最终评审结果",
    "is_project_assistant_host": "是否有项目协办人",
    "project_master_host": "项目主办人",
    "project_master_host_rate": "主办人分成比例",
    "project_assistant_host1": "项目协办人1",
    "project_assistant_host1_rate": "项目协办人1分成比例",
    "project_assistant_host2": "项目协办人2",
    "project_assistant_host2_rate": "项目协办人2分成比例",
    "creator": "创建人", "created_at": "创建时间",
}

# 前期评估评审表（modelKey: assessment_review）
ASSESS_LABELS = {
    "name": "商机名称", "belonging_to_company": "归属公司",
    "corresponding_to_customer": "业主单位", "content": "概况",
    "expected_amount": "预期金额(万/年)", "belongs_to_regional": "所属大区",
    "customer_contact_phone_number": "客户联系电话", "type": "类型",
    "cost": "三年能耗", "lists": "重点用能设备台账",
    "operation_mode": "运营模式", "decision_making_chain": "决策链",
    "area": "面积", "energy_consumption": "能耗（年）",
    "quality": "性质", "property_rights": "产权性质",
    "enterprise_nature": "企业性质", "shareholding_structure": "股权结构",
    "external_clients": "是否有外联客户",
    "customer_contact_person": "客户联系负责人",
    "reporting_customer": "关联外联客户",
    "marketing_direct_person": "市场责任人",
    "address": "地址", "contract_type": "签约类型",
    "project_size": "项目规模", "is_expected_in_bid_opening": "预计开标时间",
    "remark": "附件", "project_importance": "项目重要性",
    "technical_manager": "技术经理", "survey": "勘察", "executor": "执行人",
    "judging_results": "评审结果", "comments": "意见",
    "participant_selection": "参与人筛选", "rate": "评分",
    "resource_liaison_job_level": "资源对接人职级",
    "expected_signing_date": "预计签约日期",
    "project_classification": "项目分类", "business_scenario": "业务场景",
    "expected_contract_duration": "预计合同年限",
    "tender_intention_publicity_time": "招标意向公示时间",
    "tender_hanging_time": "招标挂网时间",
    "expected_bid_date": "预计中标时间",
    "is_project_relativer": "是否有协办人",
    "project_sponsor": "项目主办",
    "project_sponsor_rate": "项目主办人分成比例",
    "project_cohost_1": "项目协办人1",
    "project_cohost_1_rate": "项目协办人1分成比例",
    "project_cohost_2": "项目协办人2",
    "project_cohost_2_rate": "项目协办人2分成比例",
    "creator": "创建人", "created_at": "创建时间",
}

# 项目销售合同表（modelKey: contract_order）
CONTRACT_LABELS = {
    "name_of_contract": "合同名称", "related_projects": "关联项目",
    "project_than_choose": "项目比选", "economic_statement": "经济测算表",
    "belonging_to_company": "归属公司", "contract_type": "签约类型",
    "contract_no": "合同编号", "contact": "联系人",
    "contact_phone_number": "联系电话", "budget_level": "预算等级",
    "owner": "业主单位",
    "date": "中标日期", "winning_amount": "中标金额含税(元)",
    "winning_amount_without_tax": "中标金额不含税(元)",
    "service_period": "服务期限(年)", "deadline_for_service": "服务开始",
    "method_of_payment": "结算方式", "on_delivery_date": "交付期限",
    "belongs_to_regional": "所属大区", "head": "负责人",
    "budget_yields": "预算收益率", "file_number": "文件编号",
    "note": "备注", "contract_in_attachment": "合同附件(未盖章版本)",
    "using_back": "是否用背签", "c_type": "合同类型",
    "is_there_quality_retention_mon": "是否有质保金",
    "quality_retention_money_propor": "质保金比例",
    "quality_retention_money_amount": "质保金金额",
    "quality_assurance_period": "质保期限",
    "is_expected_to_return_time": "预计退回时间",
    "quality_retention_money_back_t": "质保金退回时间",
    "quality_retention_money_refund": "质保金退回金额",
    "project_type": "关联项目类型", "xi_ng_m_z_li_o": "项目资料",
    "creator": "创建人", "created_at": "创建时间",
}

LABELS = {
    "project_info": PROJECT_LABELS,
    "sales_opportunities": OPP_LABELS,
    "bidding_documents": BIDDING_LABELS,
    "assessment_review": ASSESS_LABELS,
    "contract_order": CONTRACT_LABELS,
}

# 详情面板展示顺序（重要字段优先；未列出的字段按表定义顺序追加）
PROJECT_VIEW_ORDER = [
    "project_name", "item_no", "tender_number_purchase_number",
    "correlation_information", "customer_contact", "customer_contact_phone_number",
    "project_type", "belongs_to_regional", "belonging_to_company",
    "marketing_director", "operations_follow_up_people", "project_process_status",
    "project_phase", "tender_production_status", "contract_states",
    "follow_up_state", "scheme_state", "bid_bond_state",
    "winning_amount", "estimated_amount", "service_period", "deadline_for_service",
    "contract_type", "project_sales_contract_number",
    "cumulative_received", "cumulative_bill_to", "not_receivable_amount",
    "is_expected_to_yield", "real_time_yields",
    "project_size", "project_address", "project_time", "project_overview",
    "committee_notice", "creator", "created_at",
]
OPP_VIEW_ORDER = [
    "theme", "corresponding_to_customer", "corresponding_project",
    "opportunity_number", "amount", "expected_amount",
    "opportunity_status", "business_opportunity_process_status",
    "whether_to_project", "belongs_to_regional", "type", "source",
    "customer_contact_person", "customer_contact_phone_number",
    "quality", "external_clients", "find_time", "profile",
    "creator", "created_at",
]

# 不应出现在详情面板的技术字段
HIDDEN_KEYS = {"id", "deleted_at", "description", "created_at_id", "updated_at_id"}


def _fmt_value(v):
    """值格式化：datetime / Decimal / bool 统一转可读字符串。"""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, Decimal):
        f = float(v)
        return ("%d" % f) if f == int(f) else ("%.2f" % f)
    s = str(v).strip()
    return s


def build_fields(table, row, order=None, limit=28):
    """
    把一行记录转成 [{key,label,value,required_hint}] 供前端渲染
    （英文 key 在上、中文名在下）。自动跳过空值、*_id 外键、技术字段。
    """
    labels = LABELS.get(table, {})
    if not row:
        return []
    keys = []
    seen = set()
    if order:
        for k in order:
            if k in row and k not in seen:
                keys.append(k); seen.add(k)
    for k in row.keys():
        if k in seen:
            continue
        keys.append(k); seen.add(k)
    out = []
    for k in keys:
        if k in HIDDEN_KEYS or k.endswith("_id"):
            continue
        val = _fmt_value(row.get(k))
        if val == "":
            continue
        out.append({"key": k, "label": labels.get(k, k), "value": val})
        if len(out) >= limit:
            break
    return out


# ============================================================
# 连接 / 工具
# ============================================================
def get_connection():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=False,
    )


def json_default(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def month_range(yyyymm=None):
    """返回 (date_start, date_end) ，date_end 为下月 1 号。"""
    if not yyyymm:
        today = date.today()
        yyyymm = "%04d-%02d" % (today.year, today.month)
    y, m = yyyymm.split("-")
    y, m = int(y), int(m)
    ds = "%s-01" % yyyymm
    if m == 12:
        de = "%04d-01-01" % (y + 1)
    else:
        de = "%04d-%02d-01" % (y, m + 1)
    return ds, de


# ============================================================
# 用户解析 / 角色
# ============================================================
def resolve_user(uid):
    """按 uid 解析用户；返回 dict 或 None。强制 user_type_id='hua_fu_oa' 且未删除。"""
    try:
        uid = int(uid)
    except Exception:
        return None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, dept_id, jobs_id, wx_id, if_marketing_department, "
                "if_marketing_department_id "
                "FROM users WHERE id=%s AND user_type_id='hua_fu_oa' AND deleted_at IS NULL LIMIT 1",
                (uid,))
            u = cur.fetchone()
        if not u:
            return None
        jobs_id = u.get("jobs_id")
        is_director = (jobs_id == 5) or (uid in DIRECTORS)
        is_manager = (not is_director) and (jobs_id == 11)
        is_marketing = (u.get("if_marketing_department") == "是")
        return {
            "id": u["id"],
            "name": u.get("name") or "",
            "dept_id": u.get("dept_id"),
            "jobs_id": jobs_id,
            "wx_id": u.get("wx_id") or "",
            "if_marketing_department_id": u.get("if_marketing_department_id"),
            "is_marketing": is_marketing,
            "is_director": is_director,
            "is_manager": is_manager,
            "is_leader": is_director or is_manager,
            # 拜访目标：总监/特殊 20，其余 40
            "visit_target": 20 if (is_director or uid in SPECIAL_VISIT) else 40,
            # 跟进项目目标：3553 为 8，其余 10
            "follow_target": 8 if uid == 3553 else 10,
            # 新增项目目标：3553 为 1，其余 2
            "newproj_target": 1 if uid == 3553 else 2,
            # 拜访每次分值：总监/特殊 0.75，其余 0.375
            "visit_per_score": 0.75 if (is_director or uid in SPECIAL_VISIT) else 0.375,
            # 调试账号切换（devbar）白名单：姓名包含匹配 + wx_id 精确兜底
            "in_whitelist": _in_whitelist(u.get("name"), u.get("wx_id")),
        }
    finally:
        conn.close()


def _user_wx_id(uid):
    """嗯批量场景下的 wx_id 轻量查询（深链 openId 免登用）。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT wx_id FROM users WHERE id=%s AND deleted_at IS NULL LIMIT 1", (uid,))
            r = cur.fetchone()
        return (r.get("wx_id") or "") if r else ""
    finally:
        conn.close()


def team_member_ids(uid):
    """领导团队成员 id 列表（market_user_permissions_table：user_related_id→user_id 逗号串）。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM market_user_permissions_table "
                "WHERE user_related_id=%s AND deleted_at IS NULL LIMIT 1", (uid,))
            r = cur.fetchone()
        if not r or not r.get("user_id"):
            return [uid]
        ids = [int(x) for x in str(r["user_id"]).split(",") if x.strip().isdigit()]
        return ids or [uid]
    finally:
        conn.close()


# ============================================================
# 生命条：流程阶段计算（沿用已验证 linkage）
# ============================================================
def project_flow_states_batch(ids):
    """批量返回 {pid: {flow_mk: max_state}} ，单次连接一次性算完所有项目。"""
    out = {i: {} for i in ids}
    if not ids:
        return out
    conds, params = [], []
    for tbl, d in FLOW_DEFS.items():
        conds.append("(SELECT '%s' AS mk, `%s` AS pid, id AS bid FROM `%s` "
                     "WHERE `%s` IN %%s AND deleted_at IS NULL)"
                     % (d["mk"], d["fk"], tbl, d["fk"]))
        params.append(tuple(ids))
    union = " UNION ALL ".join(conds)
    sql = """
        SELECT t.pid, t.mk, MAX(fp.state) AS st
        FROM (%s) t
        JOIN flow_procs fp ON fp.data_id = t.bid AND fp.model_key = t.mk AND fp.deleted_at IS NULL
        GROUP BY t.pid, t.mk
    """ % union
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for r in cur.fetchall():
                out.setdefault(r["pid"], {})[r["mk"]] = r["st"]
    finally:
        conn.close()
    return out


def bidding_won_batch(ids):
    """批量返回 {pid: bool} ：是否已有中标记录。"""
    out = {i: False for i in ids}
    if not ids:
        return out
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT project_name_id, COUNT(*) n FROM bidding_documents "
                "WHERE project_name_id IN %s AND whether_or_not_winning_id=1 AND deleted_at IS NULL "
                "GROUP BY project_name_id", (tuple(ids),))
            for r in cur.fetchall():
                out[r["project_name_id"]] = True
    finally:
        conn.close()
    return out


def project_flow_states(pid):
    """单项目版本（详情页用）：返回 {flow_mk: max_state}。"""
    return project_flow_states_batch([pid]).get(pid, {})


def flow_exists_batch(ids):
    """
    批量返回 {pid: {mk: {"bid": 业务记录id, "proc": 流程实例id|None}}}。
    注意：与 project_flow_states_batch 不同，这里不按并入表查——只要业务表里有该项目的
    记录就算「已提交过」，用于下阶段按钮分级；proc = 该业务记录关联 flow_procs 实例
    （data_id=bid AND model_key=mk），用于「查看合同」等已有流程的 handle 深链。
    """
    out = {i: {} for i in ids}
    if not ids:
        return out
    pieces = [
        ("bidding_documents", "project_name_id"),
        ("contract_order", "related_projects_id"),
    ]
    union = " UNION ALL ".join(
        "SELECT '%s' AS mk, `%s` AS pid, MAX(id) AS bid FROM `%s` "
        "WHERE `%s` IN %%s AND deleted_at IS NULL GROUP BY pid, mk"
        % (mk, fk, mk, fk) for mk, fk in pieces)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(union, [tuple(ids)] * len(pieces))
            recs = cur.fetchall()
            for r in recs:
                out.setdefault(r["pid"], {})[r["mk"]] = {"bid": int(r["bid"]), "proc": None}
            # 二段式批量取 flow_procs 实例 id（最近一次发起 = MAX(id)）
            pairs = []
            for r in recs:
                pairs.append((str(r["bid"]), r["mk"]))
            if pairs:
                where = " OR ".join(["(fp.data_id=%s AND fp.model_key=%s)"] * len(pairs))
                params = [x for pair in pairs for x in pair]
                cur.execute(
                    "SELECT fp.data_id, fp.model_key, MAX(fp.id) AS proc FROM flow_procs fp "
                    "WHERE (%s) AND fp.deleted_at IS NULL GROUP BY fp.data_id, fp.model_key" % where,
                    params)
                for r in cur.fetchall():
                    key = str(r["data_id"])
                    for r2 in recs:
                        if str(r2["bid"]) == key and r2["mk"] == r["model_key"]:
                            out.setdefault(r2["pid"], {}).setdefault(
                                r["model_key"], {"bid": int(r2["bid"]), "proc": None}
                            )["proc"] = int(r["proc"])
    finally:
        conn.close()
    return out


def flow_exists(pid):
    return flow_exists_batch([pid]).get(pid, {})


def flow_records(pid):
    """单项目版本：{mk: {"bid":..,"proc":..}}（详情页 next_action 用）。"""
    return flow_exists(pid)


def bidding_won(pid):
    """单项目版本（详情页用）。"""
    return bidding_won_batch([pid]).get(pid, False)


def compute_project_lifebar(pid, fs=None, won=None, info=None):
    """大节点生命条（项目）。fs=流程状态map，won=是否中标；均可在批量场景传入以避免重复查库。"""
    if fs is None:
        fs = project_flow_states(pid)
    if won is None:
        won = bidding_won(pid)
    # 各流程完成判定（state>=2 视为通过/完成）
    assess_ok = fs.get("assessment_review", 0) >= 2
    eco_ok = fs.get("economic_benefit_evaluation", 0) >= 2
    bid_eval_ok = fs.get("bid_economic_evaluation", 0) >= 2
    bid_ok = fs.get("bidding_documents", 0) >= 2
    contract_ok = fs.get("contract_order", 0) >= 2

    nodes = []
    # N1 立项（前期评估评审 + 初评-经济效益评估 都通过）
    if assess_ok and eco_ok:
        n1 = "done"
    elif assess_ok or eco_ok:
        n1 = "doing"
    else:
        n1 = "todo"
    nodes.append({
        "key": "setup", "name": "立项", "status": n1,
        "hint": "前期评估评审%s / 初评-经济效益评估%s"
                % ("✓" if assess_ok else "✗", "✓" if eco_ok else "✗"),
    })
    # N2 投标准入（投标文件申请 + 标前-经济效益评估 都通过 + 中标）
    admit = bid_ok and bid_eval_ok and won
    if admit:
        n2 = "done"
    elif (bid_ok or bid_eval_ok):
        n2 = "doing"
    else:
        n2 = "todo"
    nodes.append({
        "key": "bid", "name": "投标", "status": n2,
        "hint": "投标文件申请%s / 标前经济评估%s / 中标%s"
                % ("✓" if bid_ok else "✗", "✓" if bid_eval_ok else "✗", "✓" if won else "✗"),
    })
    # N3 合同（项目销售合同完成）
    n3 = "done" if contract_ok else ("doing" if fs.get("contract_order", 0) >= 1 else "todo")
    nodes.append({
        "key": "contract", "name": "合同", "status": n3,
        "hint": "项目销售合同流程%s" % ("✓" if contract_ok else "进行中/未开始"),
    })
    # N4 完成
    n4 = "done" if (contract_ok and admit) else "todo"
    nodes.append({"key": "done", "name": "完成", "status": n4, "hint": "全部大节点完成"})
    return nodes


def compute_opportunity_lifebar(opp):
    """大节点生命条（商机）。返回 [{name,status,hint}]。"""
    opp_status = opp.get("opportunity_status_id")
    biz_status = opp.get("business_opportunity_process_status_id")
    converted = (str(opp.get("whether_to_project")).strip() in ("是", "1", "2")) or (opp_status == 2)
    nodes = []
    # N1 商机确认
    n1 = "done" if opp_status in (0, 1, 2, None) or opp_status is not None else "todo"
    nodes.append({"key": "confirm", "name": "商机确认", "status": "done" if opp.get("id") else "todo",
                  "hint": "商机状态：%s" % (opp.get("opportunity_status") or "—")})
    # N2 前期评估
    n2 = "done" if biz_status in (1, 2, 3, 4) else ("doing" if biz_status == 0 else "todo")
    nodes.append({"key": "eval", "name": "前期评估", "status": n2,
                  "hint": "商机流程：%s" % (opp.get("business_opportunity_process_status") or "—")})
    # N3 立项转项目
    nodes.append({"key": "convert", "name": "立项转项目", "status": "done" if converted else "doing",
                  "hint": "是否转项目：%s" % (opp.get("whether_to_project") or "否")})
    return nodes


# ============================================================
# 流程页：商机 / 项目 列表
# ============================================================
def list_projects(uid, keyword=""):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            conds = ["pi.deleted_at IS NULL", "pi.marketing_director_id=%s"]
            params = [uid]
            if keyword:
                conds.append("(pi.project_name LIKE %s OR pi.item_no LIKE %s)")
                params += ["%" + keyword + "%", "%" + keyword + "%"]
            sql = ("SELECT pi.id, pi.project_name, pi.item_no, pi.project_type, "
                   "pi.belongs_to_regional, pi.marketing_director, pi.project_process_status, "
                   "pi.tender_production_status, pi.contract_states, pi.follow_up_state, "
                   "pi.scheme_state, pi.winning_amount, pi.created_at "
                   "FROM project_info pi WHERE " + " AND ".join(conds) +
                   " ORDER BY pi.created_at DESC LIMIT 200")
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    ids = [r["id"] for r in rows]
    states_map = project_flow_states_batch(ids)
    won_map = bidding_won_batch(ids)
    exists_map = flow_exists_batch(ids)
    wx = _user_wx_id(uid)
    for r in rows:
        r["lifebar"] = compute_project_lifebar(r["id"], states_map.get(r["id"], {}), won_map.get(r["id"], False), r)
        r["_flow_exists"] = exists_map.get(r["id"], {})
        r["next_action"] = next_action_for_project(r, wx)
        del r["_flow_exists"]
    return rows


def list_opportunities(uid, keyword=""):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            conds = ["so.deleted_at IS NULL", "so.creator_id=%s"]
            params = [uid]
            if keyword:
                conds.append("(so.theme LIKE %s OR so.corresponding_to_customer LIKE %s)")
                params += ["%" + keyword + "%", "%" + keyword + "%"]
            sql = ("SELECT so.id, so.theme, so.corresponding_to_customer, so.amount, "
                   "so.opportunity_status, so.opportunity_status_id, "
                   "so.business_opportunity_process_status, so.business_opportunity_process_status_id, "
                   "so.whether_to_project, so.corresponding_project_id, so.find_time, so.created_at "
                   "FROM sales_opportunities so WHERE " + " AND ".join(conds) +
                   " ORDER BY so.created_at DESC LIMIT 200")
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    wx = _user_wx_id(uid)
    for r in rows:
        r["lifebar"] = compute_opportunity_lifebar(r)
        r["next_action"] = next_action_for_opportunity(r, wx)
    return rows


def next_action_for_project(p, wx_id=None):
    """
    下阶段操作按钮分级（用户 2026-10-20 明确口径）：
      1) 已提交过「投标文件申请」→ 发起销售合同
      2) 已提交过「项目销售合同」→ 查看合同
      3) 其余（含已完成立项）→ 投标文件申请

    为什么不用「立项未完成→发起前期评估」：连库实测 project_info 8864 条里，
    前期评估评审 + 初评-经济效益评估 双流程齐全的仅 321 条（3.6%），
    若按立项分级会让 96% 的项目永远停在「发起前期评估」，与业务实际不符。
    故以业务表是否已有投标记录作为分级依据（实测已有 2857 条投标记录）。

    跳转说明（2026-10-20 改造）：不再用 window.goto/postMessage 试探，
    而是服务端直接下发老 OA 移动端实证深链 URL（制式同 flow_ui jump_url）：
      - 已有合同流程 → pageMode=handle&processId=流程实例（ OA 打开即到处理/查看页）
      - 发起新流程 → pageMode=add&modelKey=业务表&flowKey=流程key（移动端 add 模式实证字段）
    """
    ex = p.get("_flow_exists") or {}   # mk → {"bid":..,"proc":..}
    if "contract_order" in ex:
        rec = ex["contract_order"]
        wx = wx_id or ""
        if rec.get("proc"):
            return {"label": "查看合同", "flow_key": "", "model_key": "contract_order",
                    "stage": "contract_done", "jump_mode": "handle", "proc_id": rec["proc"],
                    "url": oa_deep_url(wx, "handle", model_key="contract_order",
                                       proc_id=rec["proc"])}
        return {"label": "查看合同", "flow_key": "", "model_key": "contract_order",
                "stage": "contract_done", "jump_mode": "detail", "data_id": rec.get("bid"),
                "url": oa_deep_url(wx, "detail", model_key="contract_order",
                                   data_id=rec.get("bid"))}
    if "bidding_documents" in ex:
        return {"label": "发起销售合同", "flow_key": "W-FtVlgkuCrH7cvUjfOoC",
                "model_key": "contract_order", "stage": "to_contract",
                "jump_mode": "add",
                "url": oa_deep_url(wx_id or "", "add", model_key="contract_order",
                                   flow_key="W-FtVlgkuCrH7cvUjfOoC")}
    return {"label": "投标文件申请", "flow_key": "OKE1vZMU4wPuM0g4K0W-b",
            "model_key": "bidding_documents", "stage": "to_bidding",
            "jump_mode": "add",
            "url": oa_deep_url(wx_id or "", "add", model_key="bidding_documents",
                               flow_key="OKE1vZMU4wPuM0g4K0W-b")}


def next_action_for_opportunity(o, wx_id=None):
    lb = {n["key"]: n["status"] for n in (o.get("lifebar") or [])}
    if lb.get("convert") != "done":
        return {"label": "推进立项", "flow_key": "tLN9TeCxSCHmnL2rGE8fO",
                "model_key": "assessment_review", "stage": "to_setup",
                "jump_mode": "add",
                "url": oa_deep_url(wx_id or "", "add", model_key="assessment_review",
                                   flow_key="tLN9TeCxSCHmnL2rGE8fO")}
    return {"label": "查看项目", "flow_key": "", "model_key": "sales_opportunities",
            "stage": "done", "jump_mode": "detail", "data_id": o.get("id"),
            "url": oa_deep_url(wx_id or "", "detail", model_key="sales_opportunities",
                               data_id=o.get("id"))}


def flow_detail(id_, kind, wx_id=None):
    if kind == "project":
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM project_info WHERE id=%s AND deleted_at IS NULL LIMIT 1", (id_,))
                info = cur.fetchone()
                # 三个业务流程的最近一条记录（详情页展示「已提交什么」）
                sub = {}
                for tbl, fk, order in (
                        ("bidding_documents", "project_name_id", "created_at"),
                        ("contract_order", "related_projects_id", "created_at"),
                        ("assessment_review", "name_id", "created_at")):
                    try:
                        cur.execute(
                            "SELECT * FROM `%s` WHERE `%s`=%%s AND deleted_at IS NULL "
                            "ORDER BY `%s` DESC LIMIT 1" % (tbl, fk, order), (id_,))
                        sub[tbl] = cur.fetchone()
                    except Exception:
                        sub[tbl] = None
        finally:
            conn.close()
        if not info:
            return {"success": False, "message": "项目不存在"}
        fs = project_flow_states(id_)
        won = bidding_won(id_)
        ex = flow_exists(id_)
        lifebar = compute_project_lifebar(id_, fs, won, info)
        label_map = {"assessment_review": "前期评估评审",
                     "economic_benefit_evaluation": "初评-经济效益评估",
                     "bid_economic_evaluation": "标前-经济效益评估",
                     "bidding_documents": "投标文件申请",
                     "contract_order": "项目销售合同"}
        state_label = {0: "未开始", 1: "进行中", 2: "已完成", 3: "撤回", 4: "废弃", 5: "废弃", 6: "搁置"}
        flows = []
        for mk in ("assessment_review", "economic_benefit_evaluation",
                   "bid_economic_evaluation", "bidding_documents", "contract_order"):
            if mk in fs:
                st = fs[mk]
                flows.append({"name": label_map.get(mk, mk), "state": st,
                              "state_label": state_label.get(st, str(st)),
                              "submitted": mk in ex})
        # 已提交的投标/合同明细（供前端展示）
        subs = []
        if sub.get("bidding_documents"):
            subs.append({"table": "bidding_documents", "name": "投标文件申请",
                         "fields": build_fields("bidding_documents", sub["bidding_documents"], limit=14)})
        if sub.get("contract_order"):
            subs.append({"table": "contract_order", "name": "项目销售合同",
                         "fields": build_fields("contract_order", sub["contract_order"], limit=14)})
        return {"success": True, "kind": "project", "info": info,
                "fields": build_fields("project_info", info, PROJECT_VIEW_ORDER),
                "lifebar": lifebar, "flows": flows, "submissions": subs,
                "next_action": next_action_for_project({"_flow_exists": flow_records(id_)}, wx_id)}
    else:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM sales_opportunities WHERE id=%s AND deleted_at IS NULL LIMIT 1", (id_,))
                info = cur.fetchone()
        finally:
            conn.close()
        if not info:
            return {"success": False, "message": "商机不存在"}
        lifebar = compute_opportunity_lifebar(info)
        # 商机关联流程（用户 2026-10-20 要求「推进立项要关联前期评估评审流程」）：
        #   assessment_review.name_id 是多态外键（实测命中商机 1521 / 项目 1398），
        #   economic_benefit_evaluation.sales_opportunities_id 直链商机（实测 47 条）。
        flows = []
        subs = []
        try:
            with get_connection().cursor() as cur:
                cur.execute(
                    "SELECT id, name, judging_results_id, rate, updated_at FROM assessment_review "
                    "WHERE name_id=%s AND deleted_at IS NULL ORDER BY id DESC LIMIT 1", (id_,))
                ar = cur.fetchone()
                cur.execute(
                    "SELECT id, project_name_id, total_score, updated_at FROM economic_benefit_evaluation "
                    "WHERE sales_opportunities_id=%s AND deleted_at IS NULL ORDER BY id DESC LIMIT 1", (id_,))
                econ = cur.fetchone()
                pairs = []
                if ar:
                    pairs.append(("assessment_review", ar["id"]))
                if econ:
                    pairs.append(("economic_benefit_evaluation", econ["id"]))
                states = {}
                for mk, bid in pairs:
                    cur.execute(
                        "SELECT MAX(state) AS st FROM flow_procs "
                        "WHERE data_id=%s AND model_key=%s AND deleted_at IS NULL", (bid, mk))
                    r = cur.fetchone()
                    states[mk] = int(r["st"]) if r and r["st"] is not None else None
                label_map2 = {"assessment_review": "前期评估评审",
                              "economic_benefit_evaluation": "初评-经济效益评估"}
                state_label = {0: "未开始", 1: "进行中", 2: "已完成", 3: "撤回",
                               4: "废弃", 5: "废弃", 6: "搁置"}
                if ar:
                    st = states.get("assessment_review")
                    flows.append({"name": label_map2["assessment_review"], "state": st,
                                  "state_label": state_label.get(st, "未提交"),
                                  "submitted": True})
                    subs.append({"table": "assessment_review", "name": "前期评估评审",
                                 "fields": build_fields("assessment_review", ar, limit=14)})
                if econ:
                    st = states.get("economic_benefit_evaluation")
                    flows.append({"name": label_map2["economic_benefit_evaluation"], "state": st,
                                  "state_label": state_label.get(st, "未提交"),
                                  "submitted": True})
        except Exception:
            pass
        return {"success": True, "kind": "opportunity", "info": info,
                "fields": build_fields("sales_opportunities", info, OPP_VIEW_ORDER),
                "lifebar": lifebar, "flows": flows, "submissions": subs,
                "next_action": next_action_for_opportunity(info, wx_id)}


# ============================================================
# 待办 / 周计划
# ============================================================
def list_week_plan(uid, yyyymm=None):
    """
    当前用户的周计划主表 + 明细，**只返回当月**（用户 2026-10-20 要求）。
    monthly 字段实测为月初 datetime 字符串（如 '2026-10-01 00:00:00'），
    故用 monthly >= 本月1号 AND monthly < 下月1号 过滤。

    客户框：实测存在两套字段——
      旧：morning_customer / afternoon_customer（3068 条，历史数据）
      新：morning_customer1 / afternoon_customer1（637 条，新表单）
    这里合并成 morning[] / afternoon[] 两个数组，前端每个客户渲染成一个可点击框。
    """
    ds, de = month_range(yyyymm)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, weekly, monthly, created_at FROM market_week_plan "
                "WHERE creator_id=%s AND deleted_at IS NULL "
                "AND monthly>=%s AND monthly<%s ORDER BY id ASC", (uid, ds, de))
            plans = cur.fetchall()
            for p in plans:
                cur.execute(
                    "SELECT id, plan_date, date, morning_customer, afternoon_customer, "
                    "morning_customer1, afternoon_customer1 "
                    "FROM market_weekly_plan_details "
                    "WHERE link_id=%s AND deleted_at IS NULL ORDER BY plan_date ASC", (str(p["id"]),))
                raw = cur.fetchall()
                # 同一天可能有多条明细（实测 2026-10-09 有两条），按日期合并客户框
                byday, order = {}, []
                for d in raw:
                    pd = d.get("plan_date") or d.get("date") or ""
                    ds_ = str(pd)[:10]
                    if ds_ not in byday:
                        byday[ds_] = {"date_str": ds_, "morning": [], "afternoon": [],
                                      "detail_ids": []}
                        order.append(ds_)
                    node = byday[ds_]
                    node["detail_ids"].append(d["id"])
                    for k in ("morning_customer", "morning_customer1"):
                        v = (d.get(k) or "").strip()
                        if v and v not in node["morning"]:
                            node["morning"].append(v)
                    for k in ("afternoon_customer", "afternoon_customer1"):
                        v = (d.get(k) or "").strip()
                        if v and v not in node["afternoon"]:
                            node["afternoon"].append(v)
                p["details"] = [byday[k] for k in order]
    finally:
        conn.close()
    return plans


WP_CN = {1: "第一周", 2: "第二周", 3: "第三周", 4: "第四周", 5: "第五周"}


def create_week_plan(body):
    """
    新建周计划（H5 「我的周计划」+ 新建按钮，2026-10-23）。

    body: {"uid": 123, "dates": {"2026-10-26": {"am": "拜访XX医院", "pm": "…"}, …}}
    口径对齐原 OA 表单 + trace_score v6.2：
      · 周次 = ceil(日/7)（1-5），weekly=第X周 / weekly_id=X；ceil 永不跨月，
        全部日期必须同月，否则 400
      · monthly = 'YYYY-MM-01 00:00:00'（与存量一致）
      · 每个有内容的日期一行明细：plan_date=date、新表单字段
        morning_customer1 / afternoon_customer1（自由文本）、link_id=主表id 字符串
    全部写入走 _audit 留痕（可 rollback.py 撤销）。
    """
    u = resolve_user(body.get("uid"))
    if not u:
        return {"success": False, "message": "用户不存在或未授权"}
    dates = body.get("dates") or {}
    if not isinstance(dates, dict) or not dates:
        return {"success": False, "message": "请先填写至少一天的内容"}

    clean = {}
    months = set()
    for ds, v in dates.items():
        ds = str(ds or "").strip()
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", ds)
        if not m or v is None:
            continue
        try:
            dd = datetime.strptime(ds, "%Y-%m-%d")
        except ValueError:
            continue
        am = str(v.get("am") or "").strip()
        pm = str(v.get("pm") or "").strip()
        if not am and not pm:
            continue
        clean[ds] = {"am": am or None, "pm": pm or None}
        months.add(ds[:7])
    if not clean:
        return {"success": False, "message": "请先填写至少一天的内容"}
    if len(months) > 1:
        return {"success": False, "message": "一次只能创建同一月份内的周计划"}

    first = min(clean)  # "YYYY-MM-DD"
    wn = (int(first[8:10]) + 6) // 7  # ceil(日/7)，与 v6.2 归周口径一致
    weekly = WP_CN.get(wn, "第%d周" % wn)
    monthly = "%s-01 00:00:00" % first[:7]
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection()
    acts = []
    plan_id = None
    detail_ids = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO market_week_plan (weekly, weekly_id, monthly, "
                "creator, creator_id, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (weekly, wn, monthly, u["name"], u["id"], now_s, now_s))
            plan_id = conn.insert_id()
            acts.append({"action": "insert", "table": "market_week_plan",
                         "id": plan_id,
                         "row": {"weekly": weekly, "weekly_id": wn,
                                 "monthly": monthly, "creator": u["name"],
                                 "creator_id": u["id"],
                                 "created_at": now_s, "updated_at": now_s}})
            for ds in sorted(clean):
                v = clean[ds]
                cur.execute(
                    "INSERT INTO market_weekly_plan_details "
                    "(link_id, plan_date, morning_customer1, afternoon_customer1, "
                    "creator, creator_id, created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (str(plan_id), ds, v["am"], v["pm"],
                     u["name"], u["id"], now_s, now_s))
                did = conn.insert_id()
                detail_ids.append(did)
                acts.append({"action": "insert", "table": "market_weekly_plan_details",
                             "id": did,
                             "row": {"link_id": str(plan_id), "plan_date": ds,
                                     "morning_customer1": v["am"],
                                     "afternoon_customer1": v["pm"],
                                     "creator": u["name"], "creator_id": u["id"],
                                     "created_at": now_s, "updated_at": now_s}})
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    _audit("week-plan", u["id"], u["name"], acts)

    return {"success": True, "message": "周计划已保存（%s，%d 天）" % (weekly, len(clean)),
            "plan_id": plan_id, "detail_ids": detail_ids}


def score_history(uid, limit=20):
    """
    最近的加分记录（「我的」页签底部冒泡窗口用）。
    数据源 = 真实业务行为折算成 9 项绩效指标的实时增量，只读、不写 achievements：
      · 外勤打卡（chcks type=2 & whether_field=1）→ 拜访客户量 +0.375 / +0.75
      · 某项目的**首次**跟进（follow_up_record）→ 跟进项目 +10/目标
      · 前期评估评审通过（assessment_review.judging_results_id=1）→ 新增项目 +5/目标
    """
    u = resolve_user(uid)
    if not u:
        return {"success": False, "message": "用户不存在", "events": []}
    per_visit = float(u["visit_per_score"])
    per_follow = round(10.0 / (u["follow_target"] or 10), 2)
    per_newproj = round(5.0 / (u["newproj_target"] or 2), 2)
    # 源查询抓取量大于展示 limit，保证本月合计不被截断（展示仍只取 limit 条）
    fetch_limit = max(int(limit), 200)
    events = []
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # 1) 外勤打卡 → 拜访客户量
            cur.execute(
                "SELECT id, created_at, content FROM chcks "
                "WHERE type=2 AND whether_field=1 AND creator_id=%s AND deleted_at IS NULL "
                "ORDER BY created_at DESC LIMIT %s", (uid, fetch_limit))
            for r in cur.fetchall():
                events.append({
                    "time": _fmt_value(r["created_at"]), "metric": "拜访客户量",
                    "delta": per_visit, "note": "外勤打卡",
                    "detail": (r.get("content") or "")[:40],
                })
            # 2) 首次跟进某项目 → 跟进项目
            cur.execute(
                "SELECT f.id, f.created_at, f.content, f.related_projects_id, f.field_clock, "
                "       p.project_name, "
                "       (SELECT MIN(f2.created_at) FROM follow_up_record f2 "
                "         WHERE f2.creator_id=f.creator_id "
                "           AND f2.related_projects_id=f.related_projects_id "
                "           AND f2.deleted_at IS NULL) AS first_at "
                "FROM follow_up_record f "
                "LEFT JOIN project_info p ON p.id=f.related_projects_id "
                "WHERE f.creator_id=%s AND f.related_projects_id>0 AND f.deleted_at IS NULL "
                "ORDER BY f.created_at DESC LIMIT %s", (uid, fetch_limit))
            for r in cur.fetchall():
                first_at = _fmt_value(r.get("first_at"))
                now_at = _fmt_value(r.get("created_at"))
                is_first = (first_at == now_at)
                events.append({
                    "time": now_at, "metric": "跟进项目",
                    "delta": per_follow if is_first else 0,
                    "note": ("首次跟进：" if is_first else "重复跟进：") +
                            (r.get("project_name") or "项目#%s" % r["related_projects_id"]),
                    "detail": (r.get("content") or "")[:40],
                })
            # 3) 前期评估评审通过 → 新增项目
            cur.execute(
                "SELECT id, name, updated_at, rate FROM assessment_review "
                "WHERE marketing_direct_person_id=%s AND judging_results_id=1 AND deleted_at IS NULL "
                "ORDER BY updated_at DESC LIMIT %s", (uid, fetch_limit))
            for r in cur.fetchall():
                events.append({
                    "time": _fmt_value(r.get("updated_at")), "metric": "新增项目",
                    "delta": per_newproj, "note": "评审通过：" + (r.get("name") or ""),
                    "detail": "评分 %s" % _fmt_value(r.get("rate")),
                })
    finally:
        conn.close()
    # 只保留有实际加分的记录，按时间倒序
    events = [e for e in events if e["delta"] > 0]
    events.sort(key=lambda e: e["time"], reverse=True)
    today = date.today().strftime("%Y-%m-%d")
    month_prefix = date.today().strftime("%Y-%m")
    today_total = round(sum(e["delta"] for e in events if e["time"].startswith(today)), 2)
    # 本月合计：只累加当月事件（limit 截断可能带入历史事件，不能全量加总）
    month_total = round(sum(e["delta"] for e in events if e["time"].startswith(month_prefix)), 2)
    events = events[:limit]   # 先统计后截断，保证合计完整
    return {"success": True, "events": events,
            "today_total": today_total, "month_total": month_total}


def list_follow_ups(uid, limit=30):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, content, related_projects_id, sales_opportunities_id, field_clock, "
                "map_location, associated_with_customer, created_at FROM follow_up_record "
                "WHERE creator_id=%s AND deleted_at IS NULL ORDER BY created_at DESC LIMIT %s", (uid, limit))
            return cur.fetchall()
    finally:
        conn.close()


def todo_checklist(uid, perf):
    """把当月考核要求转成可勾选的任务清单（目标-完成-剩余）。"""
    items = []
    # 拜访
    visit = perf.get("visit", {})
    items.append({
        "metric": "拜访客户量", "target": visit.get("target"), "done": visit.get("complete"),
        "remain": max(0, (visit.get("target") or 0) - (visit.get("complete") or 0)),
        "unit": "次", "weight": 15,
    })
    fu = perf.get("follow", {})
    items.append({
        "metric": "跟进项目", "target": fu.get("target"), "done": fu.get("complete"),
        "remain": max(0, (fu.get("target") or 0) - (fu.get("complete") or 0)),
        "unit": "个", "weight": 10,
    })
    np_ = perf.get("newproj", {})
    items.append({
        "metric": "新增项目", "target": np_.get("target"), "done": np_.get("complete"),
        "remain": max(0, (np_.get("target") or 0) - (np_.get("complete") or 0)),
        "unit": "个", "weight": 5,
    })
    return items


# ============================================================
# 我的：9 项绩效实时进度（只读聚合）
# ============================================================
def compute_weekplan_match(cur, uid, ds, de):
    """
    指标8 周计划匹配度 —— 严格复刻 trace_score v6.1 算法（绩效全景技术文档 §8.3）：

      每周得分 = A(周计划得分) + B(周执行得分)
        A 计划框数：market_weekly_plan_details 中上午/下午客户框非空的个数
                    （两套字段合并：morning_customer/morning_customer1、afternoon_customer/afternoon_customer1）
                    ≥8→2 分，4-7→1 分，<4→0 分
        B 执行框数：follow_up_record + chcks(type=2) 的 created_at 按
                    「月第几周_日期_上午/下午」去重后的组数
                    ≥8→3 分，4-7→2 分，<4→0 分
      最终得分 = 4 周平均（月末第 5 周按文档口径不计入），保留两位小数，封顶 5。

    返回 (score, detail_rows)；detail_rows 供前端展示计算来源。
    """
    # ① 当月周计划主表（monthly 实测为 '2026-10-01 00:00:00' 字符串，用范围过滤）
    cur.execute(
        "SELECT id, weekly FROM market_week_plan "
        "WHERE creator_id=%s AND monthly>=%s AND monthly<%s AND deleted_at IS NULL",
        (uid, ds, de))
    mains = cur.fetchall()
    if not mains:
        return 0.0, [], {"window": "每月 1-28 日（29-31 日不计），固定 4 周平均，封顶 5 分",
                          "plan_by": "按明细 plan_date 归周（与执行同口径）",
                          "label_mismatch_boxes": 0, "tail_ignored_records": 0}
    main_ids = [m["id"] for m in mains]
    # ② 明细 → 计划框数：**按明细 plan_date 的 ceil(日/7) 归周**（与执行框同口径）
    #    口径统一的原因：表单 weekly 标签与实际日期一致率仅 61.6%（10 月实测 76/198 框错位），
    #    若计划按标签、执行按日期，"计划-执行匹配"就不是在同一个周系统上比较。
    #    plan_date 缺失时回退到该周主表的表单标签。
    fmt = ",".join(["%s"] * len(main_ids))
    cur.execute(
        "SELECT link_id, plan_date, morning_customer, morning_customer1, "
        "afternoon_customer, afternoon_customer1 "
        "FROM market_weekly_plan_details WHERE link_id IN (%s) AND deleted_at IS NULL" % fmt,
        tuple(main_ids))
    # ③ weekly「第X周」→ 周次 1-4（仅作 plan_date 缺失时的兜底）
    cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
    week_main = {}
    label_week = {}          # link_id(主表) → 表单标签周次
    for m in mains:
        w = (m.get("weekly") or "").replace("第", "").replace("周", "")
        wn = cn.get(w)
        if wn is None:
            try:
                wn = int(w)
            except Exception:
                wn = None
        if wn:
            week_main[wn] = str(m["id"])   # 与 plan_boxes 的字符串键对齐
            label_week[str(m["id"])] = wn
    plan_boxes = {1: 0, 2: 0, 3: 0, 4: 0}       # 按日期归周
    plan_boxes_label = {1: 0, 2: 0, 3: 0, 4: 0}  # 按表单标签归周（仅用于口径差异提示）
    label_date_mismatch = 0
    for d in cur.fetchall():
        lid = str(d["link_id"])
        boxes = 0
        if (d.get("morning_customer") or "").strip() or (d.get("morning_customer1") or "").strip():
            boxes += 1
        if (d.get("afternoon_customer") or "").strip() or (d.get("afternoon_customer1") or "").strip():
            boxes += 1
        if not boxes:
            continue
        pd_ = d.get("plan_date")
        if pd_:
            dw = (pd_.day + 6) // 7
        else:
            dw = label_week.get(lid)
        if dw and 1 <= dw <= 4:
            plan_boxes[dw] += boxes
        else:
            continue                      # 月末第 5 周（29-31 日）不计入
        lw = label_week.get(lid)
        if lw and lw != dw:
            label_date_mismatch += boxes
        if lw and 1 <= lw <= 4:
            plan_boxes_label[lw] += boxes
    # ④ 执行框数：chcks(type=2) + follow_up_record 按「周_日期_时段」去重
    cur.execute(
        "SELECT created_at FROM chcks WHERE type=2 AND creator_id=%s "
        "AND created_at>=%s AND created_at<%s AND deleted_at IS NULL", (uid, ds, de))
    times = [r["created_at"] for r in cur.fetchall()]
    cur.execute(
        "SELECT created_at FROM follow_up_record WHERE creator_id=%s "
        "AND created_at>=%s AND created_at<%s AND deleted_at IS NULL", (uid, ds, de))
    times += [r["created_at"] for r in cur.fetchall()]
    keys = set()
    tail_ignored = 0
    for t in times:
        if not t:
            continue
        wk = (t.day + 6) // 7            # ceil(日/7)：1-7→第1周 … 22-28→第4周 29-31→第5周
        if wk > 4:                       # 用户 2026-10-20 拍板：不论月有几天，只算前四周
            tail_ignored += 1
            continue
        period = "AM" if t.hour < 13 else "PM"
        keys.add((wk, t.strftime("%Y-%m-%d"), period))
    actual_boxes = {1: 0, 2: 0, 3: 0, 4: 0}
    for wk, _d, _p in keys:
        actual_boxes[wk] += 1
    # ⑤ 逐周计分 + 固定 4 周平均（分母恒为 4：不论月有几周，只计算前四周）
    labels = {1: "第一周", 2: "第二周", 3: "第三周", 4: "第四周"}
    rows, total = [], 0
    for wk in (1, 2, 3, 4):
        p = plan_boxes[wk]
        a = actual_boxes[wk]
        pa = 2 if p >= 8 else (1 if p >= 4 else 0)
        pb = 3 if a >= 8 else (2 if a >= 4 else 0)
        rows.append({"week": labels[wk], "plan_box": p, "actual_box": a,
                     "plan_score": pa, "exec_score": pb, "week_score": pa + pb})
        total += pa + pb
    meta = {
        "window": "每月 1-28 日（29-31 日不计），固定 4 周平均，封顶 5 分",
        "plan_by": "按明细 plan_date 归周（与执行同口径）",
        "label_mismatch_boxes": label_date_mismatch,
        "tail_ignored_records": tail_ignored,
    }
    return round(total / 4.0, 2), rows, meta


def compute_performance(uid, yyyymm=None):
    ds, de = month_range(yyyymm)
    u = resolve_user(uid)
    if not u:
        return {"success": False, "message": "用户不存在"}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # 指标3 拜访：chcks type=2, creator_id, 当月
            cur.execute(
                "SELECT COUNT(*) AS n FROM chcks WHERE type=2 AND creator_id=%s "
                "AND created_at>=%s AND created_at<%s AND deleted_at IS NULL", (uid, ds, de))
            visit_complete = int(cur.fetchone()["n"])
            visit_score = round(min(15.0, visit_complete * u["visit_per_score"]), 2)

            # 指标4 跟进项目：follow_up_record 去重 related_projects_id, 当月
            # 目标值：以 achievements.follow_up_project_quantity 为准（官方 v6.1 同源），
            # 无 achievements 记录时兜底 3553=8 / 其余=10 硬编码（2026-09 实测 44 人全部=10，暂无差异）
            follow_target = u["follow_target"]
            try:
                cur.execute(
                    "SELECT follow_up_project_quantity FROM achievements "
                    "WHERE user_id=%s AND `date`=%s AND type_id=865 LIMIT 1",
                    (uid, yyyymm or month_range()[0][:7]))
                fa = cur.fetchone()
                if fa and fa.get("follow_up_project_quantity"):
                    follow_target = float(fa["follow_up_project_quantity"]) or follow_target
            except Exception:
                pass
            cur.execute(
                "SELECT COUNT(DISTINCT related_projects_id) AS n FROM follow_up_record "
                "WHERE creator_id=%s AND related_projects_id IS NOT NULL AND related_projects_id<>0 "
                "AND created_at>=%s AND created_at<%s AND deleted_at IS NULL", (uid, ds, de))
            follow_complete = int(cur.fetchone()["n"])
            follow_score = round(min(10.0, follow_complete / (follow_target or 10) * 10), 2)

            # 指标5 新增项目 + 指标6 前期技术：assessment_review 评审通过
            cur.execute(
                "SELECT COUNT(*) AS n, AVG(rate) AS avg_rate FROM assessment_review "
                "WHERE marketing_direct_person_id=%s AND judging_results_id=1 "
                "AND type LIKE '%%能源托管%%' AND updated_at>=%s AND updated_at<%s AND deleted_at IS NULL",
                (uid, ds, de))
            ar = cur.fetchone()
            newproj_complete = int(ar["n"] or 0)
            newproj_score = round(min(5.0, newproj_complete / u["newproj_target"] * 5), 2)
            # 指标6 前期技术 = avg(rate)，**无评审记录时完成值与评分均兜底 5 分**
            # （严格对齐 trace_score v6.1 指标6：else 分支 avgRate=5 / etScore=5；
            #   注意兜底条件是"无记录"而非"算出 0 分"——有记录但 rate=0 时官方给 0 分）
            if newproj_complete == 0:
                avg_rate = 5.0
                early_score = 5.0
            else:
                avg_rate = float(ar["avg_rate"] or 0)
                early_score = round(min(10.0, avg_rate), 2)

            # 指标7 周跟进：weekly_score 当月各周 score 平均（官方无兜底，无记录=0 分）
            # 注意：周绩效由月度脚本事后生成（每月 3 条：上旬/中旬/下旬），
            # 当月未生成时实时取不到，属数据时点问题而非口径问题。
            weekly_score = 0.0
            try:
                cur.execute(
                    "SELECT AVG(score) AS a FROM weekly_score WHERE user_id=%s "
                    "AND date_start>=%s AND date_end<%s AND deleted_at IS NULL", (uid, ds, de))
                w = cur.fetchone()
                if w and w["a"] is not None:
                    weekly_score = round(float(w["a"]), 2)
            except Exception:
                weekly_score = 0.0

            # 指标1/2 项目销售/毛利润：_bidding_documents_list 中标（简化：当月、中标、负责人=uid）
            sales_complete = 0.0
            gross_complete = 0.0
            sales_target = 0.0
            try:
                cur.execute(
                    "SELECT monthly_performance_targets, gross_profit_target FROM achievements "
                    "WHERE user_id=%s AND `date`=%s AND type_id=865 LIMIT 1", (uid, yyyymm or month_range()[0][:7]))
                a = cur.fetchone()
                if a:
                    sales_target = float(a.get("monthly_performance_targets") or 0)
            except Exception:
                pass
            try:
                cur.execute(
                    "SELECT winning_amount, gross_profit_margin, whether_energy_trusteeship_project_id, "
                    "service_life FROM `_bidding_documents_list` "
                    "WHERE marketing_director_id=%s AND whether_or_not_winning='是' AND state=2 "
                    "AND final_review_result='中标' "
                    "AND bidding_date>=%s AND bidding_date<%s "
                    "AND process_to_update_the_time>=%s AND process_to_update_the_time<%s "
                    "AND deleted_at IS NULL", (uid, ds, de, ds, de))
                for r in cur.fetchall():
                    wa = float(r.get("winning_amount") or 0)
                    life = float(r.get("service_life") or 1) or 1
                    wa_eff = wa / life if r.get("whether_energy_trusteeship_project_id") == 1 else wa
                    sales_complete += wa_eff
                    gm = float(r.get("gross_profit_margin") or 0) * 0.01
                    gross_complete += wa_eff * gm
            except Exception:
                pass
            sales_score = round(min(120.0, sales_complete / sales_target * 20), 2) if sales_target else 0.0
            gross_score = round(min(120.0, gross_complete / (sales_target * 0.15) * 20), 2) if sales_target else 0.0

            # 指标8 周计划匹配度：trace_score v6.1 算法（4 周平均，计划框+执行框分段计分）
            weekplan_score, weekplan_rows, weekplan_meta = compute_weekplan_match(cur, uid, ds, de)

            # 指标9 领导评分：leader_score（最新一条有效）
            leader_score = None
            try:
                cur.execute(
                    "SELECT total_score FROM leader_score WHERE associated_performance_id IN "
                    "(SELECT id FROM achievements WHERE user_id=%s AND `date`=%s) "
                    "AND tag=1 ORDER BY id DESC LIMIT 1", (uid, yyyymm or month_range()[0][:7]))
                ls = cur.fetchone()
                if ls and ls["total_score"] is not None:
                    leader_score = round(float(ls["total_score"]), 2)
            except Exception:
                leader_score = None
    finally:
        conn.close()

    metrics = [
        {"key": "sales", "name": "项目销售", "weight": 20, "target": round(sales_target, 2),
         "complete": round(sales_complete, 2), "score": sales_score, "unit": "元"},
        {"key": "gross", "name": "毛利润", "weight": 20, "target": round(sales_target * 0.15, 2),
         "complete": round(gross_complete, 2), "score": gross_score, "unit": "元"},
        {"key": "visit", "name": "拜访客户量", "weight": 15, "target": u["visit_target"],
         "complete": visit_complete, "score": visit_score, "unit": "次"},
        {"key": "follow", "name": "跟进项目", "weight": 10, "target": follow_target,
         "complete": follow_complete, "score": follow_score, "unit": "个"},
        {"key": "newproj", "name": "新增项目", "weight": 5, "target": u["newproj_target"],
         "complete": newproj_complete, "score": newproj_score, "unit": "个"},
        {"key": "early", "name": "前期技术", "weight": 10, "target": 10,
         "complete": round(avg_rate, 2), "score": early_score, "unit": "分",
         "note": ("本月无评审通过记录，完成值与评分均按保底 5 分" if newproj_complete == 0 else "")},
        {"key": "weekfollow", "name": "周跟进", "weight": 10, "target": 10,
         "complete": min(10.0, weekly_score), "score": min(10.0, weekly_score), "unit": "分",
         "note": "周绩效由月度脚本事后生成（每月 3 条），当月未生成时为 0 分"},
        {"key": "weekplan", "name": "周计划匹配度", "weight": 5, "target": 100,
         "complete": "—", "score": weekplan_score, "unit": "%",
         "detail": weekplan_rows, "meta": weekplan_meta},
        {"key": "leader", "name": "领导评分", "weight": 5, "target": 5,
         "complete": "—", "score": leader_score, "unit": "分"},
    ]
    total_score = sum(m["score"] for m in metrics if isinstance(m["score"], (int, float)))
    return {
        "success": True, "yyyymm": yyyymm or month_range()[0][:7], "user": u,
        "metrics": metrics, "total_score": round(total_score, 2),
        "visit": {"target": u["visit_target"], "complete": visit_complete, "score": visit_score},
        "follow": {"target": u["follow_target"], "complete": follow_complete, "score": follow_score},
        "newproj": {"target": u["newproj_target"], "complete": newproj_complete, "score": newproj_score},
    }


# ============================================================
# 选：跟进弹窗「客户/项目/商机」图标选择器的候选数据（只读、轻量）
# ============================================================
# ============================================================
# 跟进表单字典（2026-10-22 依《外勤打卡表单_复现规格与数据核对》+ 生产数据反查）
#   · 三选一：关联项目=1 / 关联客户=2 / 销售机会=3（follow_up_record 3.4万行实证）
#   · 是/否系 select：是=1 否=2；本次达成节点 4 选项 id：2/3/4/5
#   · 项目状态 project_status：0商机确认…7项目终止（与 project_status_change_apply 一致）
#   · 五组阶段进度快照：选项 id 即列表序号（1 起），_id 逗号串与文本同序拼接
# ============================================================
ONE_OUT = {"project": ("关联项目", 1), "customer": ("关联客户", 2), "opp": ("销售机会", 3)}
EFFECTIVE = {"是": 1, "否": 2}
NODE_ID = {"公司内部立项": 2, "业主内部立项": 3, "招标意向公示": 4, "招标挂网": 5}
PS_TEXT = {0: "商机确认", 1: "前期评估", 2: "项目立项", 3: "项目采购",
           4: "合同签订", 5: "项目实施", 6: "运营管理", 7: "项目终止"}
PS_ID = dict((t, k) for k, t in PS_TEXT.items())
INSIGHT_FIELDS = [
    ("decision_making_department_information", "决策部门信息"),
    ("competitor_information", "竞争对手信息"),
    ("risk_discovery_identification", "风险发现与识别"),
    ("our_advantages", "我方优势"),
    ("required_support", "需要的支持"),
    ("owner_feedback", "业主对已提交方案的反馈"),
]
E_GROUPS = [
    ("pre_data_collection", "前期沟通与资料收集",
     ["项目建筑概况", "年度能耗规模", "政策沟通", "介绍能源托管模式",
      "对接了客户主管部门", "对接了客户主要领导", "项目产权及经营管理主体"]),
    ("pre_investigation_confirmation", "初步勘察确认",
     ["3~5年能耗分析数据", "供热模式及系统情况", "供冷模式及系统情况", "供水系统情况",
      "变配电系统情况", "新能源建设情况", "数字化平台建设情况",
      "运维人员数量及费用", "年度维修维护情况及费用", "初步经济性评估"]),
    ("plan_formulation_and_communication", "方案编制及沟通",
     ["经济测算评估可行", "项目托管边界", "能源基准期", "节能改造措施要求",
      "客户同意采用能源托管模式合作"]),
    ("internal_project_initiation", "内部立项",
     ["提供项目建议方案", "客户上会立项", "确定能源审计单位", "完成能源审计报告",
      "完成能源费用托管实施方案及专家评审", "方案上会批准实施"]),
    ("bidding_and_tendering_stage", "招投标阶段",
     ["客户确定招标代理公司", "客户发出采购予公告", "了解招标文件编制进展及内容",
      "了解评标办法评分标准", "招标挂网", "投标", "定标", "合同签订",
      "内部合同交底/项目移交"]),
]

# ============================================================
# 打卡 tab（2026-10-22 新增，对齐 OA「内勤打卡/外勤打卡」脚本 + 生产数据实证）
#   · 公司坐标：内勤 chcks 行 1.8 万条聚类 (118.85895, 32.02256)，可用环境变量覆盖
#   · 300m 分界：≤300m 内勤，>300m 外勤（半径可配）
#   · 时间窗 7:00–20:00（原内勤脚本拦截文案原样保留）
#   · 上/下班：单按钮上午打上班(type=0)、下午打下班(type=1)
#     迟到分界 08:30（生产 8.0-8.5 桶 正常上班 6.0 万 vs 迟到 5 条实证）
#     早退分界 17:30（17.5-18.0 桶 正常下班 2.1 万实证）
#   · 外勤需外出/出差申请：仅 if_marketing_department_id != 717 且不在白名单时校验
#     （对齐原 OA 脚本的 isFieldDept 判定），白名单工号原样照抄
#   · 绩效安全：拜访客户量只数 type=2，纯打卡(0/1)不动计分
# ============================================================
CLOCK_COMPANY_LNG = float(os.getenv("MH5_COMPANY_LNG", "118.85895"))
CLOCK_COMPANY_LAT = float(os.getenv("MH5_COMPANY_LAT", "32.02256"))
CLOCK_RADIUS_M = float(os.getenv("MH5_CLOCK_RADIUS_M", "300"))
CLOCK_EXEMPT_IDS = {
    "3155", "3506", "1826234081", "1826234217", "10404", "10464", "10463", "10459",
    "1826234000", "1826234088", "1826234097", "1996234394", "1826234003", "1826234180",
    "1826234162", "1996234272", "1826234215", "1826234226", "1926234273", "1926234272",
    "1826234268", "1826234193", "1826234071", "1826234188", "1826234189", "1826234190",
    "1826234191", "1826234192", "3381", "1926234271", "1996234391", "1826234241",
    "1826234267", "3184", "10649", "1996234401",
}
CLOCK_MULTI_ACCOUNT_IDS = {"1996234397", "1996234398"}   # 周剑双号


def _clock_distance_m(lat, lng):
    """Haversine 距离（米），公司坐标为基准点。"""
    import math
    r = 6371000.0
    p1, p2 = math.radians(CLOCK_COMPANY_LAT), math.radians(lat or 0.0)
    dl = math.radians((lng or 0.0) - CLOCK_COMPANY_LNG)
    dp = p2 - p1
    a = (math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _clean(v):
    """字符串入参去空；数组转逗号串（options 多选）。"""
    if isinstance(v, (list, tuple)):
        return ",".join((str(x) or "").strip() for x in v if (str(x) or "").strip())
    return ("" if v is None else str(v)).strip()


def picker_options(uid, q=""):
    """
    2026-10-22 新增。跟进录入弹窗三个图标（客户/项目/商机）各自的候选条目：

      · 客户（纯文本，写 follow_up_record.associated_with_customer；库里无独立客户表，
        来源聚合自：历史跟进关联客户 + 本人全部周计划客户框（两套字段合并）
        + 本人商机的业主单位 corresponding_to_customer）
      · 项目（写 related_projects_id；取 marketing_director_id=本人 的项目）
      · 商机（写 sales_opportunities_id；取 creator_id=本人 的商机）

    三类一起返回，q 同时过滤三类（客户在 Python 侧过滤，项目/商机走 SQL LIKE）。
    """
    u = resolve_user(uid)
    if not u:
        return {"success": False, "message": "未授权",
                "customers": [], "projects": [], "opportunities": []}
    kw = (q or "").strip()
    like = "%" + kw + "%"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # 1) 客户：历史跟进关联客户
            names = []
            cur.execute(
                "SELECT associated_with_customer AS n FROM follow_up_record "
                "WHERE creator_id=%s AND deleted_at IS NULL "
                "AND associated_with_customer IS NOT NULL AND associated_with_customer<>'' "
                "LIMIT 500", (u["id"],))
            names += [r["n"] for r in cur.fetchall()]
            # 2) 客户：本人周计划客户框（旧 morning_customer/afternoon_customer 与
            #    新 morning_customer1/afternoon_customer1 两套字段合并；link_id 是 varchar）
            cur.execute(
                "SELECT d.morning_customer n1, d.afternoon_customer n2, "
                "d.morning_customer1 n3, d.afternoon_customer1 n4 "
                "FROM market_weekly_plan_details d "
                "JOIN market_week_plan p ON d.link_id=CAST(p.id AS CHAR) "
                "WHERE p.creator_id=%s AND p.deleted_at IS NULL "
                "AND d.deleted_at IS NULL LIMIT 2000", (u["id"],))
            for r in cur.fetchall():
                for k in ("n1", "n2", "n3", "n4"):
                    v = (r.get(k) or "").strip()
                    if v:
                        names.append(v)
            # 3) 客户：本人商机的业主单位
            cur.execute(
                "SELECT corresponding_to_customer AS n FROM sales_opportunities "
                "WHERE creator_id=%s AND deleted_at IS NULL "
                "AND corresponding_to_customer IS NOT NULL AND corresponding_to_customer<>'' "
                "LIMIT 500", (u["id"],))
            names += [r["n"] for r in cur.fetchall()]
            seen, customers = set(), []
            for v in names:
                v = (v or "").strip()
                if not v or v in seen:
                    continue
                if kw and kw not in v:
                    continue
                seen.add(v)
                customers.append(v)
                if len(customers) >= 50:
                    break
            # 4) 项目：我负责的
            conds = ["pi.deleted_at IS NULL", "pi.marketing_director_id=%s"]
            params = [u["id"]]
            if kw:
                conds.append("(pi.project_name LIKE %s OR pi.item_no LIKE %s)")
                params += [like, like]
            cur.execute(
                "SELECT pi.id, pi.project_name, pi.item_no, pi.project_type, "
                "pi.correlation_information "
                "FROM project_info pi WHERE " + " AND ".join(conds) +
                " ORDER BY pi.created_at DESC LIMIT 50", params)
            projects = cur.fetchall()
            # 5) 商机：我创建的
            conds = ["so.deleted_at IS NULL", "so.creator_id=%s"]
            params = [u["id"]]
            if kw:
                conds.append("(so.theme LIKE %s OR so.corresponding_to_customer LIKE %s)")
                params += [like, like]
            cur.execute(
                "SELECT so.id, so.theme, so.corresponding_to_customer "
                "FROM sales_opportunities so WHERE " + " AND ".join(conds) +
                " ORDER BY so.created_at DESC LIMIT 50", params)
            opportunities = cur.fetchall()
    finally:
        conn.close()
    return {"success": True, "q": kw, "customers": customers,
            "projects": projects, "opportunities": opportunities}


# ============================================================
# 写：新增跟进 + 外勤双写 chcks
# ============================================================
def create_follow_up(body):
    """2026-10-22 v2：按原外勤打卡表单（chcks 模型 + 提交后脚本对齐）收集字段。

    分区与来源（body 参数）：
      ① 关联对象三选一 object_kind: project/customer/opp（原来=多选并存，现对齐原表单）
         关联客户 buildout：project→correlation_information；opp→corresponding_to_customer
      ② 跟进信息（→ follow_up_record 全套列）：visit_purpose(拜访目的,仅chcks)、
         this_follow_up_node(本次达成节点,回写project_info.project_phase)、
         follow_up_arrangement(后续安排,回写project_info)、next_follow_up_time、
         effective_follow_up、intention(意向,record 落 owner_feedback_submitted_proposals)、
         follow_up_feedback
      ③ 状态变更：whether_change + project_status(0-7) → 仅留痕 record/chcks，
         不发起 project_status_change_apply 审批流（原 OA 该触发为悬空设计）
      ④ 五组阶段进度快照（多选）→ record/chcks 的 *_id 逗号串与文本同序
      ⑤ 业务洞察 6 字段 → record/chcks
    回写（best-effort，失败不影响主记录）：仅项目分支 → project_info
      follow_up_state=跟进(1)、follow_up_arrangement、project_phase(+project_phase_logs)，
      并回填该跟进行的 last_follow_up_node/last_follow_up_arrangement（为下次快照）。
    得分口径不变：打卡+拜访量分；首跟项目+跟进分。
    """
    uid = body.get("uid")
    u = resolve_user(uid)
    if not u:
        return {"success": False, "message": "用户不存在或未授权"}
    content = (body.get("content") or "").strip()
    if not content:
        return {"success": False, "message": "跟进内容不能为空"}

    # ---- 关联对象（三选一）----
    pid = body.get("related_projects_id") or None
    oid = body.get("sales_opportunities_id") or None
    customer = (body.get("associated_with_customer") or "").strip()
    kind = (body.get("object_kind") or "").strip()
    if not kind:
        kind = "project" if pid else ("opp" if oid else ("customer" if customer else ""))
    pid_int = int(pid) if (pid not in (None, "", "null") and str(pid).isdigit()) else None
    oid_int = int(oid) if (oid not in (None, "", "null") and str(oid).isdigit()) else None
    if kind == "project" and pid_int is None:
        kind = ""
    if kind == "opp" and oid_int is None:
        kind = ""
    if kind == "customer" and not customer:
        kind = ""

    # ---- 跟进信息 / 状态变更 / 快照 / 洞察 ----
    node = _clean(body.get("this_follow_up_node"))
    if node and node not in NODE_ID:
        node = ""
    arr = _clean(body.get("follow_up_arrangement"))
    next_time = _clean(body.get("next_follow_up_time"))
    if next_time and not next_time.isdigit():          # 期望 YYYY-MM-DD
        next_time = ""
    eff = _clean(body.get("effective_follow_up"))
    eff = eff if eff in EFFECTIVE else ""
    intent = _clean(body.get("intention"))
    fb = _clean(body.get("follow_up_feedback"))
    wc = _clean(body.get("whether_change"))
    wc = wc if wc in EFFECTIVE else ""
    ps = body.get("project_status")
    try:
        ps_int = int(ps) if (body.get("project_status", "") != "" and
                             int(ps) in PS_TEXT) else None
    except (TypeError, ValueError):
        ps_int = None
    purpose = _clean(body.get("visit_purpose"))
    insight = {}
    for key, _label in INSIGHT_FIELDS:
        v = _clean(body.get(key))
        if v:
            insight[key] = v[:255]
    # 五组多选：文本串 + 同序 id 串
    e_snap = {}
    for key, _label, opts in E_GROUPS:
        chosen = _clean(body.get(key))
        if not chosen:
            continue
        texts = [t.strip() for t in chosen.split(",")]   # 前端按选项文本提交
        ids = []                                         # 同序 id 串
        for t in texts:
            if t in opts:
                ids.append(str(opts.index(t) + 1))
        e_snap[key] = (",".join(texts), ",".join(ids))

    # 状态变更=是 但未选目标状态（对齐原表单：是/状态 成对出现）
    if wc == "是" and ps_int is None:
        return {"success": False, "message": "勾选了'是否更改当前阶段=是'，请选择目标项目状态"}

    field_clock = (body.get("field_clock") or "").strip()
    map_location = (body.get("map_location") or "").strip()
    plan_detail_id = body.get("plan_detail_id") or None
    creator = u["name"]
    now = datetime.now()
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")

    # 得分口径（与 v1 相同）：打卡 +visit_per_score；项目首跟 +10/目标
    month_start = now.strftime("%Y-%m-01 00:00:00")
    delta_visit = float(u["visit_per_score"] or 0)
    delta_follow = round(10.0 / (float(u["follow_target"] or 10) or 10), 2)
    first_follow = False

    one_out = ONE_OUT.get(kind)
    new_id = chcks_id = None
    audit_row_f = audit_row_c = None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if pid_int is not None:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM follow_up_record "
                    "WHERE creator_id=%s AND related_projects_id=%s AND deleted_at IS NULL "
                    "AND created_at>=%s", (u["id"], pid_int, month_start))
                first_follow = (cur.fetchone()["c"] == 0)

            proj = opp = cust_id = None
            if pid_int is not None:
                cur.execute(
                    "SELECT project_name, project_phase, project_phase_id, "
                    "follow_up_state, follow_up_state_id, follow_up_arrangement "
                    "FROM project_info WHERE id=%s AND deleted_at IS NULL", (pid_int,))
                proj = cur.fetchone()
            if oid_int is not None:
                cur.execute("SELECT theme FROM sales_opportunities WHERE id=%s", (oid_int,))
                opp = cur.fetchone()
            if customer:
                try:                      # 客户框文本 → customer_infomation.id（可选）
                    cur.execute(
                        "SELECT id FROM customer_infomation WHERE customer_name=%s "
                        "AND deleted_at IS NULL ORDER BY id DESC LIMIT 1", (customer,))
                    r = cur.fetchone()
                    cust_id = r["id"] if r else None
                except Exception:
                    cust_id = None

            obj_name = (proj or {}).get("project_name") or \
                       (opp or {}).get("theme") or customer or None

            # ---- 主写 follow_up_record ----
            rec = {
                "content": content, "related_projects_id": pid_int,
                "sales_opportunities_id": oid_int, "creator": creator,
                "creator_id": u["id"],
                "associated_with_customer": customer or None,
                "associated_with_customer_id": cust_id,
                "follow_up_object": obj_name,
                "field_clock": field_clock or None,
                "map_location": map_location or None,
                "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if one_out:
                rec["one_out_of_three"], rec["one_out_of_three_id"] = one_out
            if node:
                rec["this_follow_up_node"] = node
                rec["this_follow_up_node_id"] = NODE_ID[node]
            if arr:
                rec["follow_up_arrangement"] = arr
            if next_time:
                rec["next_follow_up_time"] = next_time
            if eff:
                rec["effective_follow_up"] = eff
                rec["effective_follow_up_id"] = EFFECTIVE[eff]
            if intent:
                rec["owner_feedback_submitted_proposals"] = intent
            if fb:
                rec["follow_up_feedback"] = fb
            if wc:
                rec["whether_to_change_the_project_status"] = wc
                rec["whether_to_change_the_project_status_id"] = EFFECTIVE[wc]
            if ps_int is not None:
                rec["project_status"] = PS_TEXT[ps_int]
                rec["project_status_id"] = ps_int
            for k, v in insight.items():
                rec[k] = v
            for k, (t_, i_) in e_snap.items():
                rec[k], rec[k + "_id"] = t_, i_
            # 上次跟进快照：仅项目分支（现值留痕，供"上次跟进后续安排"展示）
            if proj:
                if proj.get("project_phase"):
                    rec["last_follow_up_node"] = proj["project_phase"]
                if proj.get("follow_up_arrangement"):
                    rec["last_follow_up_arrangement"] = proj["follow_up_arrangement"]

            cur.execute(
                "INSERT INTO follow_up_record (%s) VALUES (%s)"
                % (", ".join(rec), ", ".join(["%s"] * len(rec))),
                tuple(rec.values()))
            new_id = conn.insert_id()
            audit_row_f = dict(rec)
            audit_row_f["id"] = new_id

            # ---- 外勤打卡：同步写 chcks（type=2 市场部日报，whether_field=1 外勤）----
            if field_clock:
                ch = {
                    "type": 2, "whether_field": 1, "creator": creator,
                    "creator_id": u["id"],
                    "content": ("市场部外勤跟进打卡 · " + obj_name) if obj_name
                               else "市场部外勤跟进打卡",
                    "created_at": now_s, "updated_at": now_s,
                }
                if one_out:
                    ch["one_out_of_three"], ch["one_out_of_three_id"] = one_out
                if kind == "project" and proj:
                    ch["xi_ng_m_m_ng_ch_ng"] = proj["project_name"]
                    ch["xi_ng_m_m_ng_ch_ng_id"] = pid_int
                if kind == "opp" and opp:
                    ch["opportunity"] = opp["theme"]
                    ch["opportunity_id"] = oid_int
                if customer:
                    ch["k_h_m_ng_ch_ng"] = customer
                    if cust_id:
                        ch["k_h_m_ng_ch_ng_id"] = cust_id
                if purpose:
                    ch["b_i_f_ng_m_de"] = purpose
                if intent:
                    ch["intention"] = intent
                if node:
                    ch["this_follow_up_node"] = node
                    ch["this_follow_up_node_id"] = NODE_ID[node]
                if arr:
                    ch["follow_up_arrangement"] = arr
                if next_time:
                    ch["next_follow_up_time"] = next_time
                if eff:
                    ch["effective_follow_up"] = eff
                    ch["effective_follow_up_id"] = EFFECTIVE[eff]
                if fb:
                    ch["follow_up_feedback"] = fb
                if wc:
                    ch["whether_to_change_the_project_status"] = wc
                    ch["whether_to_change_the_project_status_id"] = EFFECTIVE[wc]
                if ps_int is not None:
                    ch["project_status"] = PS_TEXT[ps_int]
                    ch["project_status_id"] = ps_int
                for k, v in insight.items():
                    ch[k] = v
                for k, (t_, i_) in e_snap.items():
                    ch[k], ch[k + "_id"] = t_, i_
                cur.execute(
                    "INSERT INTO chcks (%s) VALUES (%s)"
                    % (", ".join("`%s`" % c for c in ch),
                       ", ".join(["%s"] * len(ch))),
                    tuple(ch.values()))
                chcks_id = conn.insert_id()
                audit_row_c = dict(ch)
                audit_row_c["id"] = chcks_id
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # ---- 项目分支回写（best-effort，失败不影响已保存的跟进）----
    acts = []
    if audit_row_f:
        acts.append({"action": "insert", "table": "follow_up_record", "id": new_id,
                     "row": audit_row_f})
    if audit_row_c:
        acts.append({"action": "insert", "table": "chcks", "id": chcks_id,
                     "row": audit_row_c})
    if kind == "project" and proj is not None:
        try:
            c2 = get_connection()
            with c2.cursor() as cur2:
                sets = {"follow_up_state": "跟进", "follow_up_state_id": 1}
                if arr:
                    sets["follow_up_arrangement"] = arr
                if node:
                    sets["project_phase"] = node
                    sets["project_phase_id"] = NODE_ID[node]
                cur2.execute(
                    "UPDATE project_info SET %s WHERE id=%%s"
                    % ", ".join(k + "=%s" for k in sets),
                    tuple(sets.values()) + (pid_int,))
                acts.append({"action": "update", "table": "project_info", "id": pid_int,
                             "before": {"project_phase": proj.get("project_phase"),
                                        "project_phase_id": proj.get("project_phase_id"),
                                        "follow_up_state": proj.get("follow_up_state"),
                                        "follow_up_state_id": proj.get("follow_up_state_id"),
                                        "follow_up_arrangement":
                                            proj.get("follow_up_arrangement")},
                             "after": sets})
                old_phase = proj.get("project_phase")
                if node and node != old_phase:
                    cur2.execute(
                        "INSERT INTO project_phase_logs "
                        "(project_name, project_name_id, project_phase, project_phase_id, "
                        " creator, creator_id, type, created_at, updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (proj["project_name"], str(pid_int), node, str(NODE_ID[node]),
                         creator, u["id"], "考勤打卡", now_s, now_s))
                    acts.append({"action": "insert", "table": "project_phase_logs",
                                 "id": c2.insert_id(),
                                 "row": {"project_name": proj["project_name"],
                                         "project_name_id": str(pid_int),
                                         "project_phase": node,
                                         "project_phase_id": str(NODE_ID[node]),
                                         "creator": creator, "creator_id": u["id"],
                                         "type": "考勤打卡",
                                         "created_at": now_s, "updated_at": now_s}})
                # 为"上次跟进"快照回填本条（仅项目分支有）
                upd = {"last_follow_up_node": old_phase or None,
                       "last_follow_up_arrangement": proj.get("follow_up_arrangement") or None}
                cur2.execute(
                    "UPDATE follow_up_record SET last_follow_up_node=%s, "
                    "last_follow_up_arrangement=%s WHERE id=%s",
                    (upd["last_follow_up_node"], upd["last_follow_up_arrangement"], new_id))
                acts.append({"action": "update", "table": "follow_up_record", "id": new_id,
                             "before": {"last_follow_up_node": None,
                                        "last_follow_up_arrangement": None},
                             "after": upd})
            c2.commit()
            c2.close()
        except Exception as e:
            try:
                sys.stderr.write("mh5 writeback warn: %s\n" % str(e)[:200])
            except Exception:
                pass
    _audit("follow-up", u["id"], creator, acts)

    # 明确加分与提示（score_delta=0 或缺失 → 前端只提示已保存）
    if field_clock:
        score_delta = round(delta_visit, 2)
        note = "外勤打卡，拜访客户量 +" + str(score_delta)
    elif pid_int is not None and first_follow:
        score_delta = delta_follow
        note = "本月首次跟进该项目，跟进项目 +" + str(score_delta)
    elif pid_int is not None:
        score_delta = 0
        note = "该项目本月已跟进过，重复跟进不重复计分"
    else:
        score_delta = 0
        note = "未关联项目，不产生跟进项目加分"
    return {"success": True, "message": "跟进已保存（" + note + "）", "follow_id": new_id,
            "chcks_id": chcks_id, "field_clock_written": bool(field_clock),
            "associated_with_customer": customer or None,
            "score_delta": score_delta}


# ============================================================
# 写：打卡 tab（一键打卡，内勤/外勤自动分流）
# ============================================================
def create_clock(body):
    """单按钮打卡：
      · GPS ≤300m → 内勤：type=0(上午)/1(下午)，clock_in_type 按 08:30/17:30 档判定
      · GPS >300m → 外勤：type 同上，whether_field=1，clock_in_type='外勤打卡'；
        非 717 部门且非白名单需有在时效内的外出或出差申请；外勤说明 content 必填
      · 时间窗 7:00–20:00（服务器时间，与 OA 平台时钟一致）
      · chcks 行不落 artificial type=2，不影响 9 项指标计分
    响应：{kind: in|field, clock_in_type, type_label, today_count, dist_m}
    """
    uid = body.get("uid")
    u = resolve_user(uid)
    if not u:
        return {"success": False, "message": "用户不存在或未授权"}
    try:
        lat = float(body.get("lat"))
        lng = float(body.get("lng"))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError
        if abs(lat) < 0.01 and abs(lng) < 0.01:
            raise ValueError
    except (TypeError, ValueError):
        return {"success": False, "message": "未能获取定位，请开启手机定位后重试"}

    note = (body.get("note") or "").strip()
    content = (body.get("content") or "").strip()

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # 服务器时间（DB NOW 与 OA 平台时钟对齐，避免容器时区差）
            cur.execute("SELECT NOW() nw")
            now_db = cur.fetchone()["nw"]
            hour = now_db.hour
            today = now_db.strftime("%Y-%m-%d")
            if hour < 7:
                conn.close()
                return {"success": False, "message": "早于 7:00，暂不能打卡"}
            if hour >= 20:
                conn.close()
                return {"success": False, "message": "已超过 20:00，今日无法打卡"}

            dist_m = round(_clock_distance_m(lat, lng), 1)
            is_field = dist_m > CLOCK_RADIUS_M
            if is_field:
                # 外勤分组校验：对齐原 OA 脚本（仅非 717 且非白名单者需要）
                if u["if_marketing_department_id"] != 717 and \
                        str(u["id"]) not in CLOCK_EXEMPT_IDS and \
                        str(u["id"]) not in CLOCK_MULTI_ACCOUNT_IDS:
                    g1 = now_db + timedelta(minutes=60)
                    cur.execute(
                        "SELECT id FROM `out` WHERE creator_id=%s AND deleted_at IS NULL "
                        "AND start_time<=%s AND end_of_time>=%s LIMIT 1",
                        (u["id"], g1.strftime("%Y-%m-%d %H:%M:%S"),
                         now_db.strftime("%Y-%m-%d %H:%M:%S")))
                    out_ok = cur.fetchone() is not None
                    if not out_ok:
                        cur.execute(
                            "SELECT id FROM business_travel WHERE creator_id=%s "
                            "AND deleted_at IS NULL AND start_time<=%s AND end_of_time>=%s "
                            "LIMIT 1",
                            (u["id"], today + " 23:59:59", today))
                        travel_ok = cur.fetchone() is not None
                    if not out_ok and not travel_ok:
                        conn.close()
                        return {"success": False,
                                "message": "没有相应的外出或出差申请，请先发起申请"}
                if not content:
                    conn.close()
                    # 外勤说明必填（原脚本"未填写外勤说明"），返回提示由前端追问
                    return {"success": False, "need_content": True, "is_field": True,
                            "message": "外勤打卡需填写外勤说明"}

            ctype = 0 if hour < 12 else 1
            if is_field:
                clock_in_type = "外勤打卡"
            elif ctype == 0:
                clock_in_type = "正常上班" if now_db.time() < _t(8, 30) else "迟到"
            else:
                clock_in_type = "正常下班" if now_db.time() >= _t(17, 30) else "早退"

            cur.execute(
                "INSERT INTO chcks (type, whether_field, clock_in_type, x, y, location, "
                "message, content, creator, creator_id, created_date, created_time, "
                "created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (ctype, 1 if is_field else 0, clock_in_type,
                 lng, lat, "%.5f,%.5f" % (lat, lng), note or None,
                 content or None, u["name"], u["id"],
                 today, now_db.strftime("%Y-%m-%d %H:%M:%S"),
                 now_db.strftime("%Y-%m-%d %H:%M:%S"), now_db.strftime("%Y-%m-%d %H:%M:%S")))
            new_id = conn.insert_id()
            cur.execute(
                "SELECT COUNT(*) AS c FROM chcks WHERE creator_id=%s AND deleted_at IS NULL "
                "AND created_date=%s", (u["id"], today))
            today_count = cur.fetchone()["c"]
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    _audit("clock", u["id"], u["name"], [{"action": "insert", "table": "chcks",
                                          "id": new_id,
                                          "row": {"type": ctype,
                                                  "whether_field": 1 if is_field else 0,
                                                  "clock_in_type": clock_in_type,
                                                  "x": lng, "y": lat,
                                                  "message": note or None,
                                                  "content": content or None,
                                                  "created_date": today,
                                                  "created_time": now_db.strftime(
                                                      "%Y-%m-%d %H:%M:%S")}}])

    return {"success": True, "clock_id": new_id,
            "kind": "field" if is_field else "in",
            "type_label": "上班打卡" if ctype == 0 else "下班打卡",
            "clock_in_type": clock_in_type,
            "today_count": today_count, "dist_m": dist_m}


def clock_status(uid):
    """今日打卡状态（圆钮下方文案）。"""
    u = resolve_user(uid)
    if not u:
        return {"success": False, "message": "用户不存在或未授权"}
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT NOW() nw")
            now_db = cur.fetchone()["nw"]
            cur.execute(
                "SELECT created_time, whether_field, clock_in_type FROM chcks "
                "WHERE creator_id=%s AND deleted_at IS NULL AND created_date=%s "
                "ORDER BY id DESC LIMIT 1", (u["id"], now_db.strftime("%Y-%m-%d")))
            last = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*) AS c FROM chcks WHERE creator_id=%s AND deleted_at IS NULL "
                "AND created_date=%s", (u["id"], now_db.strftime("%Y-%m-%d")))
            today_count = cur.fetchone()["c"]
    finally:
        conn.close()
    return {"success": True, "today_count": today_count,
            "server_now": now_db.strftime("%Y-%m-%d %H:%M"),
            "last": ({"time": last["created_time"].strftime("%H:%M"),
                      "clock_in_type": last["clock_in_type"],
                      "is_field": bool(last["whether_field"])} if last else None)}


def _t(h, m):
    import datetime as _dt
    return _dt.time(h, m)


# ============================================================
# HTTP 服务
# ============================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "market-h5/1.0"

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False, default=json_default).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type="text/html; charset=utf-8"):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except Exception:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    def log_message(self, fmt, *args):
        # 2026-10-22 恢复访问日志（unit 把 stdout/stderr 收进 /var/log/market-h5.log）
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_OPTIONS(self):
        self._send_json(204, {})

    def _check_key(self):
        # 纵深防御：若配置了 MH5_API_KEY，则必须携带正确 X-MH5-Key
        if not MH5_API_KEY:
            return True
        return self.headers.get("X-MH5-Key") == MH5_API_KEY

    def do_GET(self):
        if not self._check_key():
            self._send_json(403, {"success": False, "message": "forbidden"})
            return
        parsed = urlparse(self.path)
        p = parsed.path
        q = parse_qs(parsed.query)

        if p in ("/", "/index.html"):
            self._send_file(os.path.join(BASE_DIR, "index.html"))
            return

        def gv(name, default=""):
            return q.get(name, [default])[0].strip()

        uid = gv("uid")

        try:
            if p == "/api/me":
                u = resolve_user(uid)
                if not u:
                    self._send_json(401, {"success": False, "message": "未授权：uid 无效"})
                    return
                self._send_json(200, {"success": True, "user": u})

            elif p == "/api/wx-login":
                h_wx_login(self)

            elif p == "/api/wx-callback":
                h_wx_callback(self, q)

            elif p == "/api/todo":
                u = resolve_user(uid)
                if not u:
                    self._send_json(401, {"success": False, "message": "未授权"})
                    return
                perf = compute_performance(u["id"])
                self._send_json(200, {
                    "success": True,
                    "checklist": todo_checklist(u["id"], perf),
                    "week_plan": list_week_plan(u["id"]),
                    "recent_followups": list_follow_ups(u["id"], 10),
                })

            elif p == "/api/week-plan":
                u = resolve_user(uid)
                if not u:
                    self._send_json(401, {"success": False, "message": "未授权"})
                    return
                self._send_json(200, {"success": True, "week_plan": list_week_plan(u["id"])})

            elif p == "/api/follow-ups":
                u = resolve_user(uid)
                if not u:
                    self._send_json(401, {"success": False, "message": "未授权"})
                    return
                self._send_json(200, {"success": True, "followups": list_follow_ups(u["id"], 50)})

            elif p == "/api/flow":
                u = resolve_user(uid)
                if not u:
                    self._send_json(401, {"success": False, "message": "未授权"})
                    return
                tab = gv("tab", "project")
                if tab == "opportunity":
                    rows = list_opportunities(u["id"], gv("keyword"))
                else:
                    rows = list_projects(u["id"], gv("keyword"))
                self._send_json(200, {"success": True, "tab": tab, "rows": rows})

            elif p == "/api/flow-detail":
                u = resolve_user(uid)
                if not u:
                    self._send_json(401, {"success": False, "message": "未授权"})
                    return
                id_ = gv("id")
                kind = gv("kind", "project")
                if not id_.isdigit():
                    self._send_json(400, {"success": False, "message": "id 无效"})
                    return
                self._send_json(200, flow_detail(int(id_), kind, u.get("wx_id")))

            elif p == "/api/performance":
                u = resolve_user(uid)
                if not u:
                    self._send_json(401, {"success": False, "message": "未授权"})
                    return
                self._send_json(200, compute_performance(u["id"], gv("yyyymm") or None))

            elif p == "/api/score-history":
                # 最近的加分记录（「我的」页签底部冒泡窗口）
                u = resolve_user(uid)
                if not u:
                    self._send_json(401, {"success": False, "message": "未授权"})
                    return
                lim = gv("limit") or "20"
                lim = int(lim) if lim.isdigit() else 20
                self._send_json(200, score_history(u["id"], min(100, lim)))

            elif p == "/api/pickers":
                # 跟进弹窗「客户/项目/商机」图标选择器的候选数据（只读、轻量）
                u = resolve_user(uid)
                if not u:
                    self._send_json(401, {"success": False, "message": "未授权"})
                    return
                self._send_json(200, picker_options(u["id"], gv("q")))

            elif p == "/api/clock-status":
                # 打卡 tab：今日打卡状态（次数 / 上次记录 / 服务器当前时间）
                u = resolve_user(uid)
                if not u:
                    self._send_json(401, {"success": False, "message": "未授权"})
                    return
                self._send_json(200, clock_status(u["id"]))

            else:
                self._send_json(404, {"success": False, "message": "not found"})
        except Exception:
            self._send_json(500, {"success": False, "message": traceback.format_exc()})

    def do_POST(self):
        if not self._check_key():
            self._send_json(403, {"success": False, "message": "forbidden"})
            return
        p = urlparse(self.path).path
        body = self._read_body()
        if p == "/api/follow-up":
            try:
                with WRITE_LOCK:
                    res = create_follow_up(body or {})
                self._send_json(200, res)
            except Exception:
                self._send_json(400, {"success": False, "message": traceback.format_exc()})
        elif p == "/api/clock":
            try:
                with WRITE_LOCK:
                    res = create_clock(body or {})
                self._send_json(200, res)
            except Exception:
                self._send_json(400, {"success": False, "message": traceback.format_exc()})
        elif p == "/api/week-plan":
            try:
                with WRITE_LOCK:
                    res = create_week_plan(body or {})
                self._send_json(200, res)
            except Exception:
                self._send_json(400, {"success": False, "message": traceback.format_exc()})
        else:
            self._send_json(404, {"success": False, "message": "not found"})


def serve(host="0.0.0.0", port=8766):
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[INFO] 市场部企微 H5 后端启动：http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main():
    if len(sys.argv) > 1 and sys.argv[1].strip().lower() == "serve":
        return serve()
    print("run `python server.py serve` to start (default port 8766)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
