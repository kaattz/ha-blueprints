# 003 阳光窗帘照度阈值自动学习规格

## 架构

保留现有智能阳光窗帘蓝图和三个实例，引入一个正式阈值助手、一个学习开关、一套 Pyscript 状态适配层和原生 Python 学习/存储模块。Pyscript 只负责状态读取、调度、助手写入和通知；原生模块通过 `task.executor` 执行 JSONL I/O 和批量分析，避免阻塞 HA 事件循环。

Pyscript 当前配置条目存在但为 `not_loaded`，必须先按系统化排查确认日志、配置和版本兼容性。未恢复前禁止部署适配层，也不允许以数据库直查或外部令牌脚本静默替代。

## 代码证据

- 仓库 `智能阳光窗帘控制.yaml` 仍是单一 `min_elevation` 的旧版；HA 已部署 `my_custom/智能阳光窗帘控制.yaml`，包含冬夏最低/最高高度角。实施前必须从 HA 同步完整线上源文件并做差异审查，不能用本地旧版覆盖线上功能。
- 三个实例均使用 `sensor.livingroom_balcony_lux`、`weather.he_feng_tian_qi`、`input_boolean.auto_control_curtains_based_on_sunlight` 和固定 `lux_threshold: 8000`。
- 传感器实际位于落地窗脚边，阳光进入时可直接照到；它不是仅测室外散射光的传感器。
- `script.alert_notify` 当前通过 `bemfa_wechat.send_message` 发消息，并调用 `script.add_notification_to_file` 写入 `sensor.active_notifications`；手机 App 和原生持久通知步骤均禁用。
- Pyscript 集成条目状态为 `not_loaded`；AppDaemon 和原生 `python_script` 未安装。MariaDB 已安装，但本功能不直接依赖数据库。
- Pyscript 官方文档要求应用在 `apps` 配置中有同名条目才会加载；普通文件 I/O 应放在原生 Python 模块并通过 `task.executor` 调用。实施必须验证当前版本的 `allow_all_imports` 和应用配置生效方式。

## 文件与部署边界

计划新增以下仓库文件，部署时镜像到 HA 对应目录：

- `pyscript_modules/sun_curtain_learning_core.py`：原生纯 Python 算法。
- `pyscript_modules/sun_curtain_learning_store.py`：原生 JSONL I/O、轮换和批量入口。
- `pyscript/apps/sun_curtain_learning/__init__.py`：Pyscript 状态和服务适配。
- `pyscript/sun_curtain_learning.config.example.yaml`：应用配置示例，不包含密钥。
- `tests/test_sun_curtain_learning.py`：算法与适配结构测试。
- `tests/fixtures/sun_curtain_learning/*.jsonl`：正式测试 fixture。

部署目标为 `/config/pyscript/apps/sun_curtain_learning/` 和 `/config/pyscript_modules/`。运行数据只写 `/config/pyscript_data/sun_curtain_lux_samples-YYYY-MM-DD.jsonl`。部署工具必须创建专用目录、限制文件大小并保留 30 天；不得写仓库测试目录、`.storage` 或通知文件。

应用配置必须包含照度实体、客厅学习窗帘、天气实体、自动化开关、人工灯光保护实体、正式阈值助手、学习开关、通知脚本和窗户方位范围。实体不得硬编码在应用代码中。通过 UI 配置的 Pyscript 若忽略 YAML 中 `allow_all_imports`，必须在集成选项中显式启用；无法启用时停止实施。

## 阈值接口与蓝图兼容

新增助手：

| 实体 | 类型 | 初始值 | 约束 |
| --- | --- | --- | --- |
| `input_number.sun_curtain_lux_threshold` | input_number | 8000 | 1000–50000 lx，step 100 |
| `input_boolean.sun_curtain_threshold_auto_learning` | input_boolean | on | 人工覆盖后自动 off |

蓝图新增可选 `lux_threshold_helper` 实体输入，不删除或改名现有 `lux_threshold`。运行时若助手存在且为有限数值则优先使用；未配置、`unknown`、`unavailable`、非数值或越界时使用固定输入，并在 trace 中记录来源。现有三个实例迁移为选择同一助手。

## 数据模型

每行 JSONL 是一个版本化对象：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 当前固定为 1 |
| `timestamp` | 带时区 ISO 8601 |
| `lux` | 有限非负照度 |
| `sun_azimuth` / `sun_elevation` | 采样时太阳属性 |
| `weather_state` | 天气状态 |
| `cover_state` / `cover_position` | 客厅窗帘状态和位置 |
| `automation_enabled` | 阳光窗帘总开关状态 |
| `artificial_light_guard` | 人工灯光保护状态 |
| `eligible` | 是否进入学习集合 |
| `exclude_reason` | 被排除的唯一明确原因 |

读取器逐行校验版本、类型、有限数值和时间范围。坏行隔离计数，其余有效行继续；坏行比例超过 1% 时整次分析失败，不更新阈值。

## 采样

Pyscript 监听照度状态变化。采样时一次性读取关联实体，形成一致快照。学习资格要求：

- 学习开关和自动化总开关均为 on；
- 照度、太阳属性和窗帘位置有效；
- 客厅窗帘为 open 且位置 100，最近没有 opening/closing；
- 人工灯光保护实体为 off；
- 天气不是 rainy、pouring、lightning-rainy 等下雨类状态；
- 太阳位于实例配置的窗户方位范围且高于地平线。现有动态最低/最高高度角只记录为特征，不参与采样资格，防止循环偏差。

不合格样本仍可低频保留诊断，但不得进入算法输入。相同时间戳去重，乱序样本在分析阶段排序。

## 学习算法

核心输入为最近 30 天合格样本，先按本地日期分组。

### 变化点方法

- 按一分钟重建分析视图：在相邻有效原始样本之间携带最后值，原始间隔超过 30 分钟则该段记为缺失；重建只用于分析，不改写原始数据。
- 以每日一分钟序列一阶差分的 MAD 估计噪声。候选水平变化量必须大于 `max(1000 lx, 变化前中位数的 20%, 6 × MAD)`。
- 候选水平变化必须持续至少 5 个连续有效分钟桶。
- 变化前后各 10 分钟窗口至少各有 8 个有效分钟桶，且不能跨越窗帘、人工灯光或资格状态变化。
- 使用变化前后窗口中位数生成成对样本，较低一侧标为非直射候选、较高一侧标为直射候选；变化点候选阈值为所有有效成对分界的中位数。

### 独立分组方法

- 对候选窗口照度执行确定性一维双簇分组。
- 初始中心固定为样本最小值和最大值，最多迭代 100 次，中心变化不超过 1 lx 时收敛；距离相等时归入较低簇，空簇直接返回 `ambiguous`。
- 非直射簇的高分位必须严格低于直射簇的低分位，否则返回 `ambiguous`。
- 分组候选阈值为非直射簇 P95 与直射簇 P05 的中点。

### 一致性与置信度

- 变化点分界与双簇分界的相对差异不得超过 5%；最终候选取两者中点。
- 有效天数至少 14、候选事件至少 10。
- `coverage_score = min(valid_days / 14, 1)`。
- `event_score = min(event_count / 10, 1)`。
- 令 `gap = direct_p05 - indirect_p95`、`required_gap = max(1000, direct_p05 × 10%)`，则 `separation_score = clamp(gap / required_gap, 0, 1)`。
- `agreement_score = clamp(1 - abs(change_threshold - cluster_threshold) / max((change_threshold + cluster_threshold) / 2, 1), 0, 1)`。
- 最终置信度取上述四项最低值，不做可掩盖短板的加权平均。
- 最终置信度必须不低于 0.95。
- 候选值需连续 3 次夜间分析满足 `(max - min) / median <= 5%`。

算法返回结构化结果，不返回模糊的默认阈值：`status`、`candidate_lux`、`confidence`、`valid_days`、`event_count`、`reason_code` 和诊断统计。

## 更新事务

更新前再次读取当前正式阈值和学习开关。候选必须在 1000–50000 lx，学习仍为 on。实际应用值限制在当前值的 ±20%，按 100 lx 对齐。

Pyscript 先将预期数值和短期有效时间写入独立内部标记文件，再调用 `input_number.set_value`。回读助手确认成功后才更新诊断状态和发送成功通知；任一步失败时旧值或实际回读值为准，发送 warning，不得宣称成功。

## 人工覆盖

阈值状态触发器检查内部写入标记。只有在时间窗内且值完全匹配才视为自动写入；其余变化均视为人工覆盖，调用 `input_boolean.turn_off` 暂停学习并发送 warning 通知。学习开关重新打开时清除旧稳定候选计数，但保留 30 天原始样本。

## 通知接口

统一调用：

```text
script.alert_notify(title, message, level)
```

- 自动更新：`level=info`。
- 人工覆盖暂停和可恢复错误：`level=warning`。
- 数据文件持续损坏或安全边界异常：`level=critical`。

相同 `reason_code` 每个本地日期最多通知一次。样本不足和正常学习只更新诊断实体，不发送消息。

## 诊断实体

Pyscript 维护 `sensor.sun_curtain_lux_learning`。状态使用 REQ-7 枚举；属性包括：`last_analysis`、`sample_count`、`valid_days`、`event_count`、`candidate_lux`、`applied_lux`、`confidence`、`reason_code`、`bad_line_count`、`analysis_duration_ms`。

## 错误处理

- 状态读取或解析失败：记录明确原因，跳过样本。
- 文件追加、轮换或分析超时：状态设为 error，保持阈值。
- 数据重叠或门槛不足：分别返回 ambiguous 或 insufficient_data。
- 通知失败不回滚已经成功写入的正式阈值，但诊断必须记录 notification_failed，供重试。
- Pyscript 加载失败必须保留 HA 错误和日志，不创建替代 shell 定时任务。

## 验证策略

- 纯算法单元测试覆盖两种方法、一致性、置信度、稳定门槛、越界和限幅。
- fixture 覆盖晴天、阴天、云边效应、窗帘动作、人工灯光、不可用、重启、乱序、坏行和重叠分布。
- Pyscript 适配测试覆盖采样资格、文件轮换、内部写入标记、助手回读和通知去重。
- 蓝图结构测试覆盖 helper 优先、固定值回退、input key 兼容和 trace 来源。
- HA 受控验证读取配置哈希、实体状态、Pyscript 日志和自动化 trace。
- 首次现场验收至少跨 14 个有效天；首次自动更新后人工确认实际直射/非直射行为。

## 需求追踪

| REQ | 规格章节 | 实施任务 | 验证 |
| --- | --- | --- | --- |
| REQ-1 | 阈值接口与蓝图兼容 | 4、5 | 蓝图测试、实例回读 |
| REQ-2 | 人工覆盖 | 4、5 | 覆盖暂停测试 |
| REQ-3 | 数据模型、采样 | 2、4 | fixture、采样测试 |
| REQ-4 | 学习算法 | 2、3 | 核心算法测试 |
| REQ-5 | 更新事务 | 3、4 | 事务和限幅测试 |
| REQ-6 | 通知接口 | 4、5 | 参数和去重测试 |
| REQ-7 | 诊断实体、错误处理 | 1、4、6 | 重启、损坏、现场状态 |
| REQ-8 | 文件与部署边界、验证 | 1、4、7 | 安全与性能复核 |
