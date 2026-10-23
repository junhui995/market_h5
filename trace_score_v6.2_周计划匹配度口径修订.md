# 周计划匹配度口径修订（trace_score v6.1 → v6.2 补丁）

> 配套 H5：`market_h5/server.py` 的 `compute_weekplan_match()`（已按新口径实现并上线测试机）
> 修订日期：2026-10-20　　适用范围：市场部 `leader_reported_score` 的「周计划匹配度」（权重 5 分）

---

## 一、为什么要改（3 个实测缺陷）

### 缺陷 1：计划侧与执行侧分周口径不一致 ← 主因

| 侧 | 现行归周方式 | 数据源 |
|---|---|---|
| 计划框 A | 按**表单人工标签** `market_week_plan.weekly`（"第一周/第二周…"） | 周计划主表 |
| 执行框 B | 按**记录日期** `ceil(日/7)` | `chcks` + `follow_up_record` |

实测 2026-10：**表单周次与明细日期 `ceil(日/7)` 一致率仅 61.6%**，全市场部 **198 个计划框中有 76 个（38%）归周错位**。

典型案例（市场部-李真强，10 月）：

| 明细日期 | ceil(日/7) | 表单标签 | 错位 |
|---|---|---|---|
| 10-12 / 10-13 / 10-14 | 第 2 周 | 第三周 | ✗ 错位一周 |
| 10-19 / 10-20 / 10-21 | 第 3 周 | 第四周 | ✗ 错位一周 |

后果：计划和执行不在同一个周系统上比较，"周计划**匹配度**"名不副实——第 2 周执行框虚高到 8（≥8 给满 3 分）、第 3 周被压到 3（<4 得 0 分）。

### 缺陷 2：月末 29-31 日的执行记录被丢弃

计分循环为 `allWeeks = ["1","2","3","4"]`，第 5 周（29-31 日）虽被收集进 `actualBoxCountByWeek["5"]` 但**从不读取** → 月底冲刺的跟进/打卡**在当月和下月都不产生分数**。

> 说明：这一条是**已确认的口径**（2026-10-20 用户拍板："一个月不论有几周，只计算前四周"），**保留丢弃行为**，但在脚本中显式注释，避免后续被误当成 bug 改掉。

### 缺陷 3：生产版脚本的时区错位

`C:/Users/陈俊惠/Documents/绩效全景/trace_score.txt`（8-12 版）使用 `recDate.toISOString().slice(0,10)`，会把 **00:00-08:00 的记录挪到前一天**，导致同日同时段去重失效（可能重复计框）。v6.1 已修（改用本地日期分量），**但生产文件尚未同步**。

---

## 二、修订后的口径（v6.2）

**考核窗口：每月只算前四周（1-28 日），不论月份有 28/29/30/31 天；固定 4 周平均，封顶 5 分。**

每周得分 = A（计划框得分）+ B（执行框得分）：

| 框数 | A: 计划得分 | B: 执行得分 |
|------|-------------|-------------|
| ≥8 | 2 | 3 |
| 4-7 | 1 | 2 |
| <4 | 0 | 0 |

- **A 计划框**：周计划明细中上午/下午客户框非空的个数（`morning_customer`/`morning_customer1`/`afternoon_customer`/`afternoon_customer1`），**按明细 `plan_date` 的 `ceil(日/7)` 归周**（与执行同口径）；`plan_date` 缺失时回退到该周主表的表单标签
- **B 执行框**：`chcks(type=2)` + `follow_up_record` 的 `created_at`，按「`ceil(日/7)`_日期_AM/PM」去重后的组数
- 最终得分 = 4 周得分**平均值**（分母恒为 4），封顶 5

---

## 三、代码改动（trace_score v6.1 → v6.2）

### 改动 1：明细查询补 `plan_date` 字段

```js
// 原
var detailsRes = asArray(await this.$api.list({
    modelKey: 'market_weekly_plan_details',
    filter: { link_id$in: mainIds },
    fields: ['link_id', 'morning_customer', 'morning_customer1', 'afternoon_customer', 'afternoon_customer1'],
    size: 500
}));
```
```js
// 改
var detailsRes = asArray(await this.$api.list({
    modelKey: 'market_weekly_plan_details',
    filter: { link_id$in: mainIds },
    fields: ['link_id', 'plan_date', 'morning_customer', 'morning_customer1', 'afternoon_customer', 'afternoon_customer1'],
    size: 500
}));
```

### 改动 2：计划框改为「按明细日期归周」

```js
// 原：按主表 link_id 聚合（表单标签口径）
var planBoxCountByWeek = {};
for (var di = 0; di < detailsRes.length; di++) {
    var detail = detailsRes[di];
    var linkId = detail.link_id;
    if (!planBoxCountByWeek[linkId]) planBoxCountByWeek[linkId] = 0;
    var hasMorning = (detail.morning_customer && String(detail.morning_customer).trim() !== '') ||
        (detail.morning_customer1 && String(detail.morning_customer1).trim() !== '');
    if (hasMorning) planBoxCountByWeek[linkId]++;
    var hasAfternoon = (detail.afternoon_customer && String(detail.afternoon_customer).trim() !== '') ||
        (detail.afternoon_customer1 && String(detail.afternoon_customer1).trim() !== '');
    if (hasAfternoon) planBoxCountByWeek[linkId]++;
}
```
```js
// 改：按明细 plan_date 的 ceil(日/7) 归周（与执行框同口径）
// 说明：字符串切分取日，避免 new Date() 的时区问题
var planBoxByWeek = { "1": 0, "2": 0, "3": 0, "4": 0 };
var cnWeekMap = { "一": "1", "二": "2", "三": "3", "四": "4" };
var labelWeekOfMain = {};                       // link_id → 表单标签周次（plan_date 缺失时兜底）
for (var wi2 = 0; wi2 < weekPlanMainList.length; wi2++) {
    var wp2 = weekPlanMainList[wi2];
    if (wp2.weekly) {
        var wn2 = cnWeekMap[wp2.weekly.replace(/[第周]/g, '')] || wp2.weekly.replace(/[第周]/g, '');
        labelWeekOfMain[String(wp2.id)] = wn2;
    }
}
for (var di2 = 0; di2 < detailsRes.length; di2++) {
    var det2 = detailsRes[di2];
    var boxes2 = 0;
    var hasM2 = (det2.morning_customer && String(det2.morning_customer).trim() !== '') ||
                (det2.morning_customer1 && String(det2.morning_customer1).trim() !== '');
    var hasA2 = (det2.afternoon_customer && String(det2.afternoon_customer).trim() !== '') ||
                (det2.afternoon_customer1 && String(det2.afternoon_customer1).trim() !== '');
    if (hasM2) boxes2++;                        // 上午有客户 = 1 框
    if (hasA2) boxes2++;                        // 下午有客户 = 1 框
    if (boxes2 === 0) continue;

    var wk2 = null;
    if (det2.plan_date) {
        var dayParts = String(det2.plan_date).split('-');
        var dayNum = parseInt(dayParts[2], 10);
        if (!isNaN(dayNum)) wk2 = Math.ceil(dayNum / 7);   // 1-7→1 8-14→2 15-21→3 22-28→4 29-31→5
    }
    if (!wk2) wk2 = labelWeekOfMain[String(det2.link_id)]; // 兜底：表单标签周次
    // ★ 只计算前四周：第 5 周（29-31 日）不计入
    if (wk2 && wk2 >= 1 && wk2 <= 4) planBoxByWeek[String(wk2)] += boxes2;
}
```

### 改动 3：计分循环取数改为按周

```js
// 原
var planBoxCount = mainId ? (planBoxCountByWeek[mainId] || 0) : 0;
```
```js
// 改
var planBoxCount = planBoxByWeek[wk] || 0;     // 直接按周次取，与执行框同口径
```

### 改动 4：执行框侧补时区修正（若用的是 8-12 生产版）

```js
// 原（生产版，有时区错位）
var dateStr = recDate.toISOString().slice(0, 10);
```
```js
// 改（v6.1 已有此修法，生产版需同步）
var yy = recDate.getFullYear();
var mm = ('0' + (recDate.getMonth() + 1)).slice(-2);
var dd = ('0' + recDate.getDate()).slice(-2);
var dateStr = yy + '-' + mm + '-' + dd;
```

### 改动 5：明细行输出增加「日期归周」信息（便于核对）

```js
weekPlanDetailRows.push({
    week_label: weekLabels[wk],
    week_plan_id: mainId || null,
    plan_box_count: planBoxCount,      // 已改为按日期归周后的框数
    actual_box_count: actualBoxCount,
    part_a_score: partA,
    part_b_score: partB,
    week_total_score: weekTotal
});
```
（此项可不改，仅便于在 KPI 明细里核对口径。）

---

## 四、影响面评估（2026-10 实测，市场部在职 372 人）

| 项 | 结果 |
|---|---|
| 分数有变动 | **4 人**（368 人完全不变） |
| 变动幅度 | 均 ±0.25 分（一周分差 ÷ 4） |
| 上升 / 下降 | 1 人 / 3 人 |
| 部门平均分 | 0.097 → 0.096（基本不变） |

变动样例：

| 人员 | 旧分 | 新分 | 差异原因 |
|---|---|---|---|
| 北区-张朝辉 | 1.00 | 0.75 | 旧：W2 计划10框→2分；新：W2 计划6框→1分（8 框进 W3） |
| 龚徐辉 | 2.25 | 2.50 | 旧：W3 计划0框→0分；新：W3 计划4框→1分 |
| 潘巍 | 1.50 | 1.25 | 旧：W3 计划10框→2分；新：W3 计划4框→1分 |
| 市场部-李真强 | 2.00 | 1.75 | 旧：W4 计划8框→2分；新：W4 计划2框→0分（8 框进 W2） |

---

## 五、配套的 H5 侧实现（已上线测试机，可对照）

`market_h5/server.py` → `compute_weekplan_match(cur, uid, ds, de)`：

- 计划框：`market_weekly_plan_details` 的 `plan_date` → `ceil(日/7)`，`link_id` 按字符串对齐（**注意：`link_id` 是 varchar，必须与主表 id 同为 str**，否则框数恒为 0，这是实测踩过的坑）
- 执行框：`chcks(type=2)` + `follow_up_record` 的 `created_at` → `ceil(日/7)` + 时段去重
- 只算第 1-4 周；分母固定 4
- 额外返回 `meta`：`label_mismatch_boxes`（表单标签与日期错位的框数）、`tail_ignored_records`（月末被忽略的记录数），前端在「我的」页展示

---

## 六、另需知悉的业务背景（非代码问题）

周计划匹配度当前**覆盖率极低**——市场部 372 人中，每月只有 **7-15 人**填周计划：

| 月份 | 填周计划人数 | 市场部日报打卡人数 |
|---|---|---|
| 2026-05 | 7 | 40 |
| 2026-06 | 14 | 41 |
| 2026-07 | 10 | 37 |
| 2026-08 | 15 | 38 |
| 2026-09 | 12 | 37 |
| 2026-10 | 10 | 36 |

因此部门平均分仅 0.097/5 是**真实反映**（多数人该指标为 0），不是计算错误。若希望该指标有区分度，需要先推动"每周填周计划"这个动作——H5 的待办页已把"本周填周计划"做成任务项。

---

## 七、上线步骤建议

1. 用 v6.2 覆盖 trace_score 脚本（**先在预发/月度演练跑一遍**）
2. 与 H5 对同一个用户、同一个月比对周计划匹配度分数是否一致
3. 一致后再切生产定时任务
4. 首次运行建议保留 `weekPlanDetailRows` 输出（4 周的计分过程）便于市场部解释争议
