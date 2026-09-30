# 影视拍摄连续性管理

项目使用 Python 标准库、SQLite 和 `http.server` 管理非线性拍摄中的连续性。场次与镜头分别记录叙事顺序和拍摄顺序，角色、服装、道具、伤痕状态按叙事链检查，冲突可由调整方案或正式豁免处理，镜头只有在无未处理冲突时才能锁定。

## 运行与测试

```bash
python app.py
python -m unittest discover -s tests -v
```

默认端口 `8115`，页面 <http://127.0.0.1:8115>。首次启动创建“雨夜追踪”示例，其中拍摄顺序与叙事顺序相反，并生成一个伤痕回退冲突。数据库和端口可分别用 `CONTINUITY_DB`、`PORT` 指定。

## 连续性算法

每个元素选择一种规则：

- `stable`：沿叙事顺序状态必须一致。
- `monotonic`：使用 `numeric_value` 比较，数值不能下降，适合伤痕、污损或破坏程度。
- `allowed`：只有预先登记的状态转移才能通过。

检测按叙事顺序执行，与剪辑和拍摄顺序无关。调整方案必须由制片人或场记提出、由另一位审片人批准；批准后写入镜头状态并重新检查。也可以为确实需要保留的冲突写入豁免理由。锁定会再次检查场次，豁免之外的活跃冲突会阻止锁定，锁定后直接改状态会失败。

## 主要接口

- `POST /api/users`、`POST /api/productions`
- `POST /api/productions/{id}/scenes`、`POST /api/scenes/{id}/shots`
- `POST /api/productions/{id}/elements`、`POST /api/elements/{id}/transitions`
- `POST /api/shots/{id}/states`、`POST /api/scenes/{id}/check`
- `POST /api/conflicts/{id}/plans`、`POST /api/plans/{id}/review`
- `POST /api/conflicts/{id}/exemptions`
- `POST /api/shots/{id}/lock`
- `GET /api/productions/{id}/continuity`

## 离线回传合并

山里断网时场记在设备上记下镜头状态（`state`）和冲突豁免（`exemption`），每条带设备号 `device_id`、镜头基线版本 `base_version` 和设备内连续编号 `seq`，回棚后整批提交：

- `POST /api/sync/batch`：批量回传。条目先逐条落库，再按连续编号逐条用独立事务合并。
- `POST /api/sync/resume`：从中断处继续；服务启动时会自动调用，未完成（`pending`）条目接着处理。
- `GET /api/sync/progress`、`GET /api/sync/entries`、`GET /api/sync/review`：各设备续传进度、全部条目与待核对清单。

合并规则：

- 合并前比对镜头当前版本与 `base_version`。棚内已改动（版本不符）、镜头锁定、冲突已处理等情况一律记为 `review`（待核对），**绝不回写**连续性元素状态、冲突、调整方案或豁免。
- 同一设备同一 `seq` 重复回传沿用原条目与原结果（幂等），新载荷被忽略；两台设备回传同一镜头则按设备分别记录，先到且版本匹配者合并，其余留待核对。
- 状态合并成功后镜头版本 +1 并重新检测冲突；豁免合并只把对应开放冲突置为豁免。
- 条目结果为 `merged`、`review` 或 `pending`，每条都保留来源设备号；页面“离线回传合并”区域展示合并结果、待核对、各设备续传进度及卡住编号。
