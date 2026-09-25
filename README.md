# 跨区域水资源使用权分配与转让

一个仅使用 Python 标准库实现的水权账户、计量、转让审批和干旱情景服务。SQLite 保存账户额度、取水记录、季节规则、上下游影响规则和完整审计日志。

## 运行

```bash
python app.py --init
python app.py --port 8007
```

打开 <http://127.0.0.1:8007>。`--init` 会创建北区水库和河口灌区两个示例账户，并添加一条 7 月季节上限和一条最小留存规则。数据库默认是 `water_rights.db`，可用 `--db` 或 `WATER_DB` 修改。

## API

请求头 `X-User` 和 `X-Role` 用来模拟身份。角色包括 `editor`、`reviewer`、`meter`、`viewer`。

- `POST /api/accounts`：建立账户（额度、优先级、有效期）。
- `POST /api/rules/season`：设置某地区某月份的用水比例上限。
- `POST /api/rules/impact`：设置上下游转让的最小留存比例。
- `POST /api/transfers`：发起转让；待审批金额立即预占，避免同一额度被重复转卖。
- `POST /api/transfers/{id}/approve|reject`：审核；发起人不能审批自己的记录。
- `POST /api/usage`：按计量事件登记实际取水，同一账户同一事件编号只会入账一次。
- `POST /api/carryover/batches`：年度切换，为一组账户生成结转批次。先扣已用水量和当年待审批转出量，最多把许可额度两成带入下一年结转池，其余作废；同一账户同一年不能结转第二次，重复账户会跳过并指出原批次编号。
- `GET /api/carryover/batches`：历史批次及每个账户结转前后的快照明细。
- `GET /api/accounts/{id}/annual-balance?year=2027`：账户某年的当年许可、结转池、可转让额度和可取水合计。
- `GET /api/carryover/pools?year=2027`：年度台账列表。
- `GET /api/drought/simulate?supply=1000&reduction=0.3`：按高优先级先行分配，同级账户按剩余额度比例分配。
- `GET /api/audit`：完整操作审计。

余额计算和审批使用 `BEGIN IMMEDIATE`，把余额判断与写入放在同一事务中；因此并发提交不会绕过额度检查。最小留存比例按转出账户的当前许可额度计算。

### 年度结转规则

结转计算与存储、页面分离：纯算术在 `carryover.py`（`plan_carryover`、`draw_water`、`transfer_capacity`），不接触数据库或 HTTP；`app.py` 负责 SQLite 事务，`static/index.html` 负责按账户展示结转前后明细。

- 结转结余 = `max(0, 许可额度 − 当年已用水量 − 当年待审批转出量)`；结转量 = `min(结余, 许可额度 × 20%)`，其余作废。
- 下一年取水先扣当年许可额度，不足再动结转池（`carryover_draws` 记录每笔的拆分，计量事件按账户+年份幂等）。
- 转让只认当年许可额度，结转池水量不能转让；已结转年度批准转让时，额度移动只作用于年度台账的许可列。
- 结转批次写入快照（许可、已用、待批、上限、结转、作废），重复账户在响应 `skipped` 中给出 `original_batch_id`；全部重复时返回 409。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖转让审批与实际计量、重复计量事件、季节/最小留存规则、预占导致余额不足和发起人自审冲突；以及结转两成上限与作废、待批转出扣减、重复批次指出原批次、下一年先当年后结转池取水、转让不认结转水量。
