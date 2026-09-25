# 跨区域水资源使用权分配与转让

一个仅使用 Python 标准库实现的水权账户、计量、转让审批、年度结转和干旱情景服务。SQLite 保存账户额度、取水记录、季节规则、上下游影响规则、结转批次和完整审计日志。

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
- `POST /api/carryover/batches`：为一组账户生成年度结转批次（`account_ids` 留空表示全部账户）。先扣掉已用水量和等待审批的转出量，最多把许可额度的两成结入下一年的结转池，其余作废；同一账户同一年只能结转一次，重复提交会指出原批次。
- `GET /api/carryover/batches`、`GET /api/carryover/batches/{id}`：查看批次及按账户的结转前后明细（许可额度、已用、待审批转出、剩余、两成上限、实际结转、作废、结转池余额）。
- `GET /api/accounts/{id}/carryover`：查看账户历年结转条目和结转池余额。
- `GET /api/accounts/{id}/available?as_of=YYYY-MM-DD`：查看扣减实际用量和待审批预占后的可用额度，并给出该年份可用的结转池水量。
- `GET /api/drought/simulate?supply=1000&reduction=0.3`：按高优先级先行分配，同级账户按剩余额度比例分配。
- `GET /api/audit`：完整操作审计。

余额计算和审批使用 `BEGIN IMMEDIATE`，把余额判断与写入放在同一事务中；因此并发提交不会绕过额度检查。最小留存比例按转出账户的当前许可额度计算。

## 年度结转

结转计算是纯函数，放在 `carryover.py`；存储在 `app.py` 的 `Database`（`carryover_batches` + `carryover_entries`，按账户和年份唯一）；页面在 `static/index.html`，三者分离，不引入新依赖。

- 批次按账户记录结转前后明细快照，结转池余额随次年取水扣减（`accounts.used` 只记当年额度消耗）。
- 下一年取水先扣当年额度，再按年份先后动结转池；结转池水量只在水源年份的下一年可用。
- 转让只认当年额度，不认结转水量。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖转让审批与实际计量、重复计量事件、季节/最小留存规则、预占导致余额不足、发起人自审冲突，以及年度结转的两成封顶与作废、重复批次指向原批次、次年取水先当年后结转池、转让不认结转水量。
