# 官方 demo 审查与验证

更新：2026-09-14。对象为解包后的 `Demo/CoreGeek/`，原文件未修改。代码观察不等同于判题器行为；复现方法和验证范围见§8–9。

## 1. 总评

这是一个容易阅读的最小参赛程序，覆盖 HTTP 收发、部分协议解析、单角色寻路、固定建造和简单夜防。它能帮助理解协议接线，但并不具备完整竞争策略所需的数据模型与调度机制。

其架构瓶颈可以从代码直接解释：`Turn.load()` 丢掉任务、新闻、敌方和反馈信息，`decide()` 每轮从零运行昼夜分支，角色逐个生成动作。后续即使增加更聪明的选靶函数，也无法自动补上全队预算、持续任务、历史传闻、失败恢复和任务沙盒闭环。

现有测量不支持因性能原因必须更换Python的结论。样例 50 次决策中位数 1.751 ms、P95 2.304 ms；这只覆盖一个普通请求，既不是最坏情况性能保证，也没有提供整局胜率证据。

## 2. 优点：值得保留的设计原则

| 内容 | 证据 | 为什么有用 |
| --- | --- | --- |
| 零运行时第三方依赖、入口短 | [pyproject.toml](../../Demo/CoreGeek/pyproject.toml)，[main3.py](../../Demo/CoreGeek/main3.py) L8–25 | 便于离线部署和启动排障；新框架也应控制依赖 |
| 通信、协议、寻路、策略有初步分层 | `server.py / protocol.py / grid.py / brain.py` | 可以独立观察动作和路径，不是所有逻辑挤在 handler |
| Pos 不可变可哈希，实体用 dataclass | [protocol.py](../../Demo/CoreGeek/src/agent/protocol.py) L24、50 | 坐标集合和状态描述简洁；但 Turn 内 `zones` 仍是可变 dict，不是深度不可变 |
| 距离和基地四格展开正确 | 同文件 L37–47 | 符合切比雪夫距离及左上角坐标语义 |
| 八邻接 A* 允许斜穿角，启发式与等成本步长一致 | [grid.py](../../Demo/CoreGeek/src/agent/grid.py) L6–47 | 在其已建模的静态障碍图中，属于合适的最短路方法 |
| 优先读取运行时射程、处理火箭 cooldown | `protocol.py` L83–90；`brain.py` L113–115 | 避免完全依赖样例或硬编码射程 |
| 攻击按武器 ID 输出、操作者单列 | [brain.py](../../Demo/CoreGeek/src/agent/brain.py) L118；`protocol.py` L210–215 | 理解了攻击涉及两个 ID 的协议关系 |
| 解析／决策异常有空动作兜底 | [server.py](../../Demo/CoreGeek/src/agent/server.py) L15–24 | 本地试验确认 malformed JSON 不会让该处理线程直接在决策阶段崩掉；判题器是否接受空响应仍待确认 |
| 使用固定排序使行为确定 | `brain.py`、`protocol.py` 多处 | 同一输入结果可复现，便于回放定位 |

保留这些思想，不代表应复制或继承 demo 的业务模块。

## 3. 中规中矩：可用，但不是竞争优势

| 实现 | 评价 |
| --- | --- |
| Python 标准库 ThreadingHTTPServer | 当前无状态 demo 的简单选择；没有证据表明线程本身有 bug。引入共享比赛状态后必须处理并发和重复请求 |
| A* 单起点到单终点，每次重新建障碍 | 在 1312 格小图上通常够用；同一目标周围逐格多次 A* 可换成多目标 BFS，但不应先把它称为已测出的热点 |
| 每回合重新读当前盘面 | 对战斗即时状态是合理做法；对历史新闻、任务工具结果和未完成计划不够 |
| `claimed` 预占坐标 | 能减少己方同回合撞同一格；没有统一表达金币、炮台、操作者、路径边和持续目标 |
| 固定 19 面墙并留一个入口 | 有保留通路的意图，实际位置 19 个且无重复；入口和炮位选择没有经过地图与进攻方向优化 |
| 射程内选择最近机器人 | 作为基本开火规则可用；距离不代表伤害效率、威胁或击杀价值 |
| 保守地把其他我方当前格都当障碍 | 可避免部分互换碰撞；也阻止本来合法的跟随腾挪，应在中央联合移动层改进 |

## 4. 做得差：结构性缺失或策略损失

### 4.1 信息在解析入口被丢弃

`protocol.py` L93–135 仅保留机器人 ID、位置、血量，以及有限我方信息。`teamEnemy`、机器人种类／目标阵营／眩晕、任务信息、价格、新闻、LLM／沙盒结果、上回合动作结果和 errors 都没有进入决策模型。

后果是没有经济闭环、任务闭环、新闻历史、宝藏推理，也没有失败归因。机器人攻击对手还是我方都一视同仁；这不意味着攻击对方浪潮总是错，但意味着无法有意识比较帮助对方清怪与己方防守、抢分的收益。

### 4.2 固定角色配对浪费已就位的火力

`brain.py` L125–126 将“按 ID 排序的角色”与“按坐标排序的炮台”直接 zip。配对与距离、冷却、炮台威胁、正在进行的任务无关；死亡后排序缩短还会整体换岗。

本地复现：两名工人各自已经在一座炮旁，换配对即可双炮开火，demo 却给两人都下移动指令，分配距离分别为 3、4。此项属于确定可复现的决策缺陷，不是协议错误。

### 4.3 开拓者没有发挥独有能力

`brain.py` L55–62 白天只让开拓者靠近固定炮台；不接任务、不提交答案、不召唤宝藏。工人只采石筑墙，无 `sell/buy/use`，用完 75 初始金币就没有主动补充资金的流程。

这使 demo 的收益来源被限制为低级武器防守及生存。2026-09-16新增的真实R1–R249证据中，任务、卖矿、购物、用道具确实均为零，R238基地从观测中消失；这是单局结果，不能推定其他对手和地图上能活几天。详见[真实日志审计](framework-review.md)。

### 4.4 单回合贪心缺少截止时间与整体布局

白天最后一轮仍可能往矿区走，直到入夜才召回。炮台冷却期间操作者直接空闲，不利用安全的修复、用药或短时换炮机会。

`_tower_sites()` 从内圈按 x、y 取前三个，相当于固定在基地左侧成列；上下半场均偏向全局左侧。`_stand_cells()` 先按距基地及坐标排序，不比较到交互站位的实际路径成本；`_worker_day()` 遇到第一目标就返回，即使该目标没有可行步，也不一定继续尝试其他有收益的工作。

`claimed` 只在真正 build 时预占建造目标；走向目标时主要预占下一步，多个角色可长期追同一工程目标。反之，采矿时对矿点做排他预占又与“允许多人同采”不完全匹配。应区分“资源可共享”“站位互斥”和“任务唯一归属”。

### 4.5 武器没有独立战斗模型

`_attack_target()` 只做距离筛选：没有电磁穿透预算、加特林扇区、火箭溅射中心优化、跨炮伤害协同或威胁分析。多座塔可以同时对着很低血的同一机器人开火，损失本可分配给别处的火力。

“建筑是否挡子弹”“同回合致死是否阻止反击”尚未明确，不能把没有实现某种自猜弹道规则列为确证 bug；正确做法是先建立可切换规则模型并验证。

### 4.6 HTTP 返回类型阻塞任务扩展

`decide()` 返回角色命令 map，`server.py` L20 将它包装成唯一顶层 `roleCommandMap`。因此即使往决策器补任务逻辑，也没有现成的顶层 `prompt`、`executeCmd` 通道。

这属于真实的架构接口限制；官方是否容许不提供空 `prompt/executeCmd` 没有明确，因此“demo 当前响应缺字段必然违规”不是已证实结论。

## 5. Bug 清单

以下严重度用于修复／规避排序，不代表已测得比赛损失。B02–B05 的实际异常计数归类需判题器实测；不能把执行失败全算作异常。

| ID | 问题与位置 | 本地证据 | 影响及触发范围 |
| --- | --- | --- | --- |
| B01，高 | `Turn.load/blocked` 完全忽略可见敌方占格；`protocol.py` L115–135、189–195 | 官方请求中的敌墙 `(28,7)` 不在 blocked 集合 | 寻路可计划走进已知敌墙／基地／角色；在到达相关区域时触发 |
| B02，高 | 全队金币没有逐命令预留；`brain.py` L74 | 仅 25 金币，两工人同回合分别 build 加特林、电磁，合计需要 50 | 缺乏联合可执行性；有 25–49 金币且双工可建时即可出现 |
| B03，中 | 根据固定槽位缺失建塔，没有检查全局已有 3 塔；`brain.py` L40–48、74–80 | 已有 3 个其他合法内圈炮位，仍发两条新建命令 | 对合法外部盘面不健壮；demo 原生固定建造轨迹未证明可自然触发 |
| B04，高 | `attack_command` 永远只有 1 个目标；`protocol.py` L210–215 | L2 加特林输出 1 目标 | 与接口“目标数等于等级”不符；L2/L3 火箭同样如此。demo 自身无升级流程，所以主要在升级盘面或扩展后触发 |
| B05，中 | 近矿快捷路径绕过容量检查；`brain.py` L84–89，对比 L167–168 | 100 格满包全是铜、旁边石矿，仍发 collect | 无法采入仍重复浪费动作；当前 demo 自己不挖铜，复现用合法构造盘面 |
| B06，低 | A* 起终点相同会查不存在前驱；`grid.py` L27–28、51–55 | `next_step(turn,worker,worker.pos)` 抛 KeyError | helper 边界 bug；主流程 `_step_toward` L194–195 已挡住部分调用，不能说正常对局必崩 |

附加风险，暂不列为已确认游戏 bug：

- `occupied_cells()` 不过滤死亡单位，若判题器仍发送零血残影，会留下虚假障碍；死亡单位是否留在数组待确认。
- `station()` 不检查血量；基地已毁但还保留记录时，白天流程是否还应围绕它造物需要胜负及调度规则补全。
- `Content-Length` 转换和读取在 try 外，输出写入也在 try 外，未设读取期限。对畸形头／断连的隔离不完整，但没有证据表明正规判题器会这么发包。
- 异常时整轮空动作会丢掉原本可执行的防守，不应作为唯一降级层。

## 6. 文档／交付问题

| 项目 | 分类 | 证据 |
| --- | --- | --- |
| response.txt 第 66 行前缺逗号 | 样例确定错误 | 标准 JSON parser 报 `Expecting ',' delimiter` |
| response.txt 重复角色 key | 已说明的目录写法，非多动作授权 | 临时在内存补逗号后共有 17 项、4 个唯一 key；常规 dict 解析会覆盖前项 |
| 样例 L1 射程 4、7、2147483647，与规则 3、6、10 不同 | 材料冲突 | 新框架应保存来源，运行时采用有效字段并告警 |
| 样例没有 cooldown、timeoutRounds、targetTeam | 样例不完整 | 不应反推这些正式字段无用，也不该用全 0 默认消除“不知道” |
| 包内无 run.sh、无环境说明、无测试或回放 | 交付／验证缺失 | 不代表主办方一定要求按唯一文件名启动；正式接入时确认 |

## 7. 重写取舍

建议保留标准库优先、显式坐标、确定性、基础分层等原则。重新设计完整观测模型、跨回合状态、联合动作预算、任务状态机和协议编译层。原版 demo 保持原样作为基线对手和兼容性参考，不能担任规则裁判。

## 8. 诊断与复现

<a id="diagnostics"></a>

诊断环境：Windows、Python 3.12.10。使用原版源码及样例，在内存构造输入；HTTP检查仅访问回环临时端口，未连接官方判题器。

### 8.1 补充观察结果

B01–B06的构造输入、实际结果和触发范围见§5；其余观测如下。

| 检查 | 实际结果 | 能说明什么 |
| --- | --- | --- |
| 原始request送入decide | 3条move：10010→(6,22)，10011→(9,13)，10012→(9,17) | 样例可被demo处理；不证明对应行动在裁判里成功 |
| `(10,24)`基地固定炮位 | (9,22),(9,23),(9,24) | 布局沿全局左侧成列 |
| 固定墙位数及去重 | 19个、去重后19个 | 留门实现没有重复墙位，不能误报此处有重复计数bug |
| 昼夜边界 | 0夜；1昼；70昼；71夜；130夜；131昼；200昼；201夜；1300夜 | demo采用1起算，不证明裁判也如此 |
| response原样JSON解析 | 第66行第13列 `Expecting ',' delimiter` | 样例缺逗号，不能原样当fixture |
| 仅在内存补逗号后保留重复键读取 | 17项、4个不同key；10010×10、10011×4、10012×2、10020×1 | 这是动作目录，标准dict会覆盖前项 |

### 8.2 HTTP与耗时

原版Handler通过 `127.0.0.1` 临时端口收到完整样例：HTTP 200，顶层只有 `roleCommandMap`，含3条指令。收到非法JSON `{`：HTTP 200，同样只有 `roleCommandMap`，值为空对象。

此检查确认了本地异常兜底行为，没有验证裁判是否要求三个顶层字段、是否接受空动作或真实5秒预算内的最坏情况。

对同一request连续运行50次 `decide()`，使用 `time.perf_counter()`：

| 指标 | 测量值 |
| --- | --- |
| 中位数 | 1.751 ms |
| P95，nearest rank，第48个排序样本 | 2.304 ms |
| 最大 | 2.501 ms |

这组数据不含网络传输、任务工具执行、全局搜索，也不是暖机／多地图严格性能基准。当前数据没有显示这个样例存在Python计算性能问题；不同机器复跑值可以不同。

### 8.3 核心问题复现片段

从仓库根目录运行以下PowerShell片段。它只加载原样例和demo，在内存构造输入并输出诊断，不修改文件；输出包含故意触发的错误示例。

```powershell
@'
import sys, json
sys.path.insert(0, 'Demo/CoreGeek/src')
from agent.protocol import Turn, Pos
from agent.brain import decide
from agent.grid import next_step

with open('docs/request.txt', encoding='utf-8-sig') as f:
    sample = json.load(f)

def unit(i, kind, x, y):
    return dict(id=i, roleType=kind, pos=dict(x=x, y=y),
                health=220 if kind == 'worker' else 1000,
                level=1, backpack=[], backPackCapability=100)

def request(roles, gold=75, round_no=1, robots=None, zones=None):
    return dict(roundNo=round_no,
                mapInfo=dict(width=41, height=32, zones=zones or []),
                teamOur=dict(type='challenger', goldNum=gold, roles=roles),
                teamEnemy=dict(roles=[]), robot=dict(roles=robots or []))

t = Turn.load(sample)
print('B01 enemy wall blocked:', Pos(28, 7) in t.blocked(t.workers()[0]))
try:
    next_step(t, t.workers()[0], t.workers()[0].pos)
except KeyError as e:
    print('B06 start=goal:', repr(e))

base = unit(10013, 'station', 10, 24)
roles = [base, unit(10010, 'worker', 8, 22), unit(10012, 'worker', 8, 23)]
print('B02 gold=25:', decide(request(roles, gold=25)))

roles += [unit(10020, 'gatling', 11, 25),
          unit(10030, 'railgun', 12, 25), unit(10040, 'rocket', 12, 24)]
print('B03 already 3 towers:', decide(request(roles, gold=25)))

tower = unit(10020, 'gatling', 9, 23)
tower.update(level=2, attackRange=5)
robot = dict(id=30001, pos=dict(x=7, y=23), health=40,
             roleType='smallRobot', targetTeam='challenger')
print('B04 L2 targets:', decide(request(
    [base, unit(10010, 'worker', 8, 23), tower],
    round_no=71, robots=[robot])))

worker = unit(10010, 'worker', 5, 5)
worker['backpack'] = ['copper'] * 100
print('B05 full backpack:', decide(request(
    [base, worker], gold=0,
    zones=[dict(neutralType='stone', pos=dict(x=4, y=5))])))

robot['pos'] = dict(x=10, y=20)
print('pairing, both could fire:', decide(request(
    [base, unit(10010, 'worker', 12, 22), unit(10012, 'worker', 8, 22),
     unit(10020, 'gatling', 9, 22), unit(10030, 'railgun', 12, 23)],
    round_no=71, robots=[robot])))

with open('docs/response.txt', encoding='utf-8-sig') as f:
    raw = f.read()
try:
    json.loads(raw)
except json.JSONDecodeError as e:
    print('response syntax:', e.msg, 'line', e.lineno, 'column', e.colno)
'@ | python -B -
```

这里的构造盘面用于验证函数对合法状态的健壮性，不声称它们都能沿原demo自身策略自然出现。尤其B03的非固定炮位、B04的升级塔、B05的满铜背包，需要其他策略或外部状态才能形成。

## 9. 材料指纹与验证范围

以下SHA256标识诊断依据；任务书为2026-09-14计分公式版本。

| 文件 | SHA256 |
| --- | --- |
| Demo/CoreGeek.tar.gz | `BA391BD7CB67751722FF48F4B4B566427590E6E22E08A28269EEAE31A4533C7A` |
| docs/任务书.md | `1676C370B9FC829892B315505CB67B8B4C6DC76A170434573AFD47F1E321D585` |
| docs/接口文档.md | `931A65D09F881B30644225CA8D6B1BFBC7B2BEA7B4F6CE50D1F70CF858CB144D` |
| docs/request.txt | `5FC36943CE3D7BBCA678B6342CD4A6C092D91AE8032E3107E8E840BA8CC88D82` |
| docs/response.txt | `5DAF8278D7F097417B50082165A28DC4CE03163323730DC160EE4F7A534ED8D0` |

解包源码与压缩包成员逐字节核对，7/7相同。任务计分算术校验见[策略定量比较](greedy-strategies.md)§3.3，分配成本算例见[中央计划器](architecture.md)§4。

尚未运行官方判题器、完整1300回合对局、真实LLM任务、宝藏开启或策略胜率评测；未实测最优炮型、投资比例、机器人AI、跨武器弹道及任务速度奖励的实际结算。以上局部诊断不能作为规则已全部验证或新策略必胜的证据。
