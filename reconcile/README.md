# 区间汇总核对 · 缺日补齐（reconcile）

日结存在漏报时，用**区间汇总单**（网点 + 起止日 + 区间总量）把缺日当作非负整数变量，
交叠单据的方程共同约束，仅在全部缺日有**唯一非负整数解**时补齐。

零第三方依赖：仅使用 Python 3.11 标准库（`http.server` + `sqlite3`），无需 Docker/数据库服务。

## 启动

```sh
cd reconcile
./run.sh                      # 默认 http://localhost:18309 ，PORT 可覆盖
PYTHONPATH=. python3 -m scripts.seed_demo   # 可选：灌入 唯一解/多解/矛盾 三个演示网点
```

打开 http://localhost:18309 即可录单、核对、查看来源、修订、更正、受理预测。

## 核心规则（与需求逐条对应）

1. **登记**：按网点登记起止日与总量，单据初始为 `pending`（待核对）。
2. **试算不写库**：`GET /api/branches/{branch}/check` 返回
   - `unique`：全部缺日取值唯一，附每个缺日的解；
   - `multiple`：列出仍不确定的日期 `uncertain`，并给一个示例解；
   - `infeasible`：无解，列出冲突单据（直接超限单据单独标注 `direct_conflicts`，
     其余经删除过滤给出极小不可行单据组）。
3. **核对生效与补齐**：`POST /api/forms/{id}/verify`
   - 仅 `unique` 可生效；同网点参与同一套约束的待核对单据一并生效；
   - 补齐行 `source=derived`，`source_form_ids` 保留所有覆盖该日的单据作为来源；
   - **已有日量不改**（reported/manual 行原样保留）；
   - 重试幂等：已生效单据再次核对不重复补录（`INSERT OR IGNORE` + 行级存在判断）。
4. **人工修订参与约束**：`POST /api/daily`（reported/manual）。修订后整套重算；
   约束矛盾则事务回滚、**保留原数据**并返回冲突单据；待核对阶段不提前补齐。
5. **生效单据更正**：`PUT /api/forms/{id}`。连同原补齐值整套重算（derived 行删除重建，
   区间缩小时失去覆盖的补齐日自动清除）；多解/矛盾均拒绝并保留原数据，成功才版本 +1。
6. **预测受理**：`POST /api/forecasts`
   - 覆盖目标日的待核对单据阻止受理（未核对部分阻止预测）；
   - 依据 = 全部生效单据 + 全部固定日量的哈希快照；
   - 重复受理时**旧预测保留原值**，仅置 `retained=1`，不产生第二条记录。
7. **并发**：所有写事务经 `BEGIN IMMEDIATE` + 进程写锁串行化，只取整套一致依据；
   回滚路径的拒绝审计通过回滚后钩子保证留痕。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST/GET | `/api/forms` | 登记 / 列表（`?branch=`） |
| PUT | `/api/forms/{id}` | 生效单据更正（整套重算） |
| POST | `/api/forms/{id}/verify` | 核对生效 + 唯一解补齐 |
| GET | `/api/branches/{branch}/check` | 只读试算 |
| POST/GET | `/api/daily` | 日结上报/人工修订 / 台账 |
| POST/GET | `/api/forecasts` | 预测受理 / 列表 |
| GET | `/api/audit` | 操作审计 |

错误统一为 `{code, detail, ...}`，状态码 404/422/409/400/500。

## 求解器（`app/solver.py`）

- 每张单据一个线性方程：`sum(区间日量) = total`，固定日量并入右端常量；
- 回溯枚举 + 区间传播（上界收紧、零和强制归零），域最小变量优先；
- 解上限 500、节点上限 20 万：触顶则剩余变量保守列为不确定，绝不猜测补齐；
- 无解时用删除过滤求极小冲突单据组。

## 测试

```sh
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

- `tests/test_rules.py`（14 项）：交叠唯一解与来源、重试幂等、多解列不确定日、
  直接/方程矛盾、人工修订冲突保留原值、更正重算与孤儿补齐清除、预测阻止/保留、并发争抢。
- `tests/test_http.py`（3 项）：真实 HTTP 全流程、参数校验、8 路混合并发最终一致。

## 数据

SQLite 文件默认 `reconcile/data.db`（WAL 模式），可用 `RECONCILE_DB` 环境变量改位置。
