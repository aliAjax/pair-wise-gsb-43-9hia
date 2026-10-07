# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、规则评分、利益冲突、澄清、废标、投诉重评、授标快照和联合体版本管理服务。

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
- `POST /api/vendors/qualifications`、`POST /api/vendors/qualifications/status`：登记资质证书、暂停/过期/恢复
- `POST /api/consortiums`、`POST /api/consortiums/revise`、`GET /api/consortiums/{id}`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/tenders/open`：截止后开标并核验承诺哈希
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/tenders/award`：锁定评分轮次并保存排名快照

## 联合体版本

- 创建或修订联合体时冻结牵头方、成员份额和成员资质快照，并计算版本快照哈希；投标提交时绑定当前有效版本。
- 成员变更只生成新版本（`consortium.revised`），原版本置为 `superseded`，不改写原投标和承诺哈希。
- 成员证书过期或资格暂停时（含懒扫描发现的到期），包含该成员的原版本置为 `invalid`。
- 版本失效时：未开标投标整组失效（`invalid`）；已开标投标保留原快照，但评分和授标被拦截，授标快照记录 `excluded_bids`。
- 版本失效后截止前可按新的有效版本重新提交，同一项目同一联合体只保留一条投标；内容完全一致的重复提交直接返回原记录，不重复审计。
- 页面与审计时间线按联合体版本展示成员、份额、资质状态（含当前状态）和受影响投标。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评、角色权限，以及联合体快照冻结、成员变更版本化、证书暂停/过期失效、牵头方变更重投和重复提交去重。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。资质到期采用懒检测，在提交、开标、评分、授标和查询时扫描失效，没有后台定时任务。
