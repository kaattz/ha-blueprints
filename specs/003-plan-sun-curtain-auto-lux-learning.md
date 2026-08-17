# 003 阳光窗帘照度阈值自动学习实施计划

> **执行要求：** 未经用户再次明确批准不得修改蓝图、HA 或 Pyscript；不得创建 worktree。实施时使用 `investigate`、`test-driven-development`、`home-assistant-best-practices` 和 `verification-before-completion`。

**Goal:** 自动学习落地窗直射照度分界，高置信度更新三个阳光窗帘共用阈值，并通过现有消息链路通知。

**Architecture:** 纯 Python 核心负责变化点、双簇分组、置信度和更新决策；Pyscript 适配层负责 HA 状态、JSONL、助手、调度和通知；蓝图优先读取正式阈值助手并保留固定值回退。

**Tech Stack:** Home Assistant 2026.8、Pyscript、Python 标准库、YAML/Jinja、HA config API、pytest。

---

### Task 1：实施前检查、线上蓝图同步与 Pyscript 根因门禁

<files>

- 读取：`specs/003-req-sun-curtain-auto-lux-learning.md`
- 读取：`specs/003-spec-sun-curtain-auto-lux-learning.md`
- 读取：`docs/plans/2026-08-17-sun-curtain-auto-lux-learning-design.md`
- 对比：`智能阳光窗帘控制.yaml`
- 读取：HA `/config/blueprints/automation/my_custom/智能阳光窗帘控制.yaml`
- 读取：HA Pyscript 配置、日志和集成状态

<action>

- 读取 `home-assistant-best-practices` 的 safe refactoring、automation patterns、template guidelines 和 blueprint guide。
- 记录三个窗帘实例、`script.alert_notify` 和相关助手的配置哈希与当前状态。
- 通过只读 Samba/SSH 或受管接口取得完整线上蓝图，确认季节高度输入和逻辑；先备份并同步到仓库，再审查差异，禁止用本地旧版覆盖线上文件。
- 使用 `investigate` 查明 Pyscript `not_loaded` 的根因；检查日志、配置、版本和依赖。未确认根因不得修复或进入部署任务。
- 明确 Pyscript 应用配置、`allow_all_imports`、原生模块路径和 `task.executor` 能力；若设计所需能力不可用，停止并回到规格评审，不临时切换 MariaDB 或 shell 方案。

<verify>

- 线上蓝图和仓库待编辑基线内容一致，三个实例输入均可解析。
- Pyscript 根因有日志证据，恢复方案可验证且不依赖长期令牌。
- 所有读取均为只读，没有修改 HA 运行状态。

<done>

- 获得可信代码基线和 Pyscript 执行前提；任何门禁失败都显式阻止后续任务。

### Task 2：先写纯算法失败测试与正式 fixture

<files>

- 创建：`tests/test_sun_curtain_learning.py`
- 创建：`tests/fixtures/sun_curtain_learning/clear_day.jsonl`
- 创建：`tests/fixtures/sun_curtain_learning/cloudy_overlap.jsonl`
- 创建：`tests/fixtures/sun_curtain_learning/cover_transition.jsonl`
- 创建：`tests/fixtures/sun_curtain_learning/corrupt_rows.jsonl`
- 创建：`pyscript_modules/sun_curtain_learning_core.py`

<action>

- 先写失败测试，固定 JSONL schema、坏行比例、30 天窗口、14 天/10 事件门槛和确定性输出。
- 覆盖变化点持续 5 分钟、前后 10 分钟窗口、双簇分组、分布重叠、两方法不一致和连续 3 次稳定。
- 覆盖 95% 置信门槛、1000–50000 lx 边界、100 lx 对齐和单次 ±20% 限幅。
- fixture 只放在测试路径，来源和预期分类写入测试说明；生产路径禁止 mock。

<verify>

- 运行 `python -m pytest tests/test_sun_curtain_learning.py -v`。
- 实现前测试因核心函数缺失而失败，失败原因必须对应规格而非 fixture 语法错误。

<done>

- 失败测试完整覆盖 REQ-3、REQ-4、REQ-5 的关键边界。

### Task 3：实现纯 Python 学习核心

<files>

- 修改：`pyscript_modules/sun_curtain_learning_core.py`
- 修改：`tests/test_sun_curtain_learning.py`

<action>

- 实现版本化 JSONL 解析、有限数值校验、坏行隔离和时间窗口过滤。
- 实现按日会话、稳健分析视图、变化点事件和前后窗口统计。
- 实现确定性一维双簇分组、分离门槛和候选阈值。
- 实现四项置信得分取最小值、3 次稳定状态机和结构化 `reason_code`。
- 实现候选范围校验、100 lx 对齐和 ±20% 应用值计算。
- 不引入第三方统计库，除非规格评审重新批准。

<verify>

- 定向测试全部通过且重复运行结果一致。
- 使用大于预期 30 天数据规模的 fixture 运行性能测试，分析在约定上限内完成。

<done>

- 核心算法纯函数化、可重复、无 HA 依赖，满足 REQ-3 至 REQ-5。

### Task 4：实现 Pyscript 采样、持久化、更新和人工覆盖

<files>

- 创建：`pyscript_modules/sun_curtain_learning_store.py`
- 创建：`pyscript/apps/sun_curtain_learning/__init__.py`
- 创建：`pyscript/sun_curtain_learning.config.example.yaml`
- 修改：`tests/test_sun_curtain_learning.py`
- 创建运行目录：HA `/config/pyscript_data/`

<action>

- 先增加适配层结构失败测试，再实现照度状态触发和一致快照读取。
- 按规格生成合格/排除样本，窗帘移动和人工灯光变化邻近窗口必须排除。
- 在原生 store 模块实现原子追加、按日文件、30 天轮换、大小上限、坏行统计和分析超时；Pyscript 只能通过 `task.executor` 调用。
- 从 `pyscript.app_config` 读取所有实体和方位配置；缺项时显式加载失败，不使用代码默认实体。
- 实现每日夜间分析服务、诊断实体和相同错误每日一次通知去重。
- 实现内部写入标记、正式阈值回读事务和人工修改自动暂停。
- 调用现有 `script.alert_notify`；成功、warning、critical 使用既有合法 level。

<verify>

- 本地适配结构测试通过。
- 适配层通过本地语法、结构和依赖边界测试；本任务不提前修改或加载 HA Pyscript。
- dry-run 的 HA 内验证延后到 Task 6，在根因修复和部署后执行。

<done>

- Pyscript 能安全采样、恢复、分析和通知，失败时保持旧值。

### Task 5：修改蓝图、创建助手并迁移三个实例

<files>

- 修改：`智能阳光窗帘控制.yaml`
- 修改：蓝图结构测试
- 创建：HA `input_number.sun_curtain_lux_threshold`
- 创建：HA `input_boolean.sun_curtain_threshold_auto_learning`
- 修改：三个“智能阳光窗帘控制”实例

<action>

- 先写失败测试，确认新增可选 `lux_threshold_helper`、现有 input key 不变、助手优先和固定值回退。
- 更新已同步的线上蓝图基线，只增加阈值来源，不改方位角、季节高度、天气、等待时间或隐私逻辑。
- 通过 HA config API 创建助手；不直接编辑 `.storage`。
- 使用配置哈希保护迁移三个实例选择同一阈值助手，保留各实例现有固定 `8000` 作为回退。
- 更新后逐个回读配置并重载蓝图/自动化。

<verify>

- 静态测试、蓝图导入校验和三个实例加载均通过。
- 助手有效时 trace 显示 helper；助手不可用时 trace 显示 fixed_fallback。
- 实际方位角、季节高度和 `no_sun_duration` 与修改前一致。

<done>

- 三个实例安全共享正式阈值，并保持所有既有太阳几何逻辑。

### Task 6：恢复 Pyscript、部署学习器并进入现场学习期

<files>

- 修改：仅 Task 1 根因证据批准的 Pyscript 配置
- 部署：HA `/config/pyscript_modules/sun_curtain_learning_core.py`
- 部署：HA `/config/pyscript_modules/sun_curtain_learning_store.py`
- 部署：HA `/config/pyscript/apps/sun_curtain_learning/__init__.py`
- 配置：HA Pyscript `apps.sun_curtain_learning`

<action>

- 按已确认根因做最小 Pyscript 修复，加载后检查 repair、日志和实体。
- 按官方约束验证 `allow_all_imports`、原生模块路径和 `apps.sun_curtain_learning` 配置已生效。
- 部署经过测试的核心与适配层，开启自动学习助手。
- 手动调用一次 dry-run 分析，只更新诊断，不修改正式阈值。
- 首次运行只采样；未达到 14 个有效天前不得自动更新。
- 验证通知链路仅在人工覆盖、真实错误或成功更新时触发。
- 记录部署时间和第一轮预计最早分析日期。

<verify>

- Pyscript 状态为 loaded，诊断实体为 learning 或 insufficient_data，而非 error。
- JSONL 文件正常增长、轮换边界安全，样本排除原因符合现场状态。
- 14 个有效天门槛前正式阈值仍为 8000。

<done>

- 系统进入可观察学习期；此时只算阶段完成，不 archive、不宣称最终准确。

### Task 7：14 天现场验收、分层 Review 与漂移检查

<files>

- 修改：`README.MD`
- 复核：本 feature 的 design、req、spec、plan
- 验证：Pyscript 诊断、样本文件、通知记录和三个自动化 trace

<action>

- 学习期结束后复核有效天数、事件数、天气覆盖、分布间隔、两方法一致性和连续稳定次数。
- 若门槛不足，保持学习状态并明确原因；不得降低 95% 门槛换取结果。
- 首次自动更新后现场确认有直射时关帘、无直射和多云散射时不误关。
- 产品 Review：逐条核对 REQ-1 至 REQ-8 和非目标。
- 工程 Review：检查文件原子性、重启恢复、人工覆盖、通知去重、超时和限幅。
- UI Review：检查助手名称、单位、范围和现有通知中心可读性；不新增独立界面。
- 验证 Review：检查单元测试、Pyscript 日志、HA trace 和现场观察证据。
- 完成前漂移检查：确认未修改方位/高度逻辑、未引入数据库直查、长期令牌、手机通知或生产 mock。

<verify>

- `python -m pytest -v` 全部通过。
- req、spec、实现和现场证据逐条可追踪。
- 所有失败和长期 insufficient_data 状态显式呈现。

<done>

- 首次可靠自动更新和现场行为均通过，文档同步完成；用户最终验收后才能 archive。
