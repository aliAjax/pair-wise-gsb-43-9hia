# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评和授标快照服务。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/qualifications`、`POST /api/qualifications/suspension`：登记证书、暂停/恢复成员资格
- `POST /api/consortia`、`GET /api/consortia`、`GET /api/consortia/{id}`：联合体版本
- `POST /api/tenders/open`：截止后开标并核验承诺哈希
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/tenders/award`：锁定评分轮次并保存排名快照

## 联合体版本规则

- 提交联合体（`POST /api/consortia`，字段 `consortium_no`、`lead_vendor_id`、`members:[{vendor_id,share}]`，份额合计必须为 100、牵头方必须在成员中）即冻结一个版本：牵头方、成员份额、各成员证书/暂停资质快照与 `snapshot_hash` 一并固化。
- 成员或份额变化只能生成新版本（版本号递增），旧版本标记 `superseded`，其投标内容、承诺哈希和联合体快照哈希永不改写。
- 未开标的旧版本投标在成员变更或成员资格暂停时**整组失效**（`invalid`）；已开标的投标保留原快照与正文，但评分接口返回 409、授标排名排除该投标并把它记入 `blocked_consortium_bids`。
- 成员证书过期（按 `expires_at` 动态判定）或资格暂停时，包含该成员的原版本同样失效；开标瞬间再做一次扫描。恢复资格后版本恢复有效。
- 相同牵头方 + 成员 + 份额的重复提交幂等，只保留一条版本（响应含 `deduplicated: true`）。
- 联合体投标通过 `POST /api/bids` 的 `consortium_id` 关联，且只有牵头方可以提交；开标时同时校验投标承诺哈希与联合体快照哈希。
- 页面、`/api/state`、`/api/consortia` 与审计时间线均按版本展示成员、份额、冻结/当前资质状态和受影响投标。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评、角色权限，以及联合体版本冻结、重复提交去重、成员变更、证书过期、资格暂停对评分与授标的拦截。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
