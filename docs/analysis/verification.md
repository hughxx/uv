# 本轮诊断与证据

日期：2026-09-14。目录：`D:\claude_max\coregeek`。本机：Windows、Python 3.12.10。

§1–5保留首轮诊断记录；积分规则刷新核对追加在§6，同伴评审与Git初始化记录在§7。

## 1. 实际执行范围

- 读取全部原始任务书、接口文档、请求／响应样例及demo全部源码。
- 检查压缩包成员路径后解包到 `Demo/CoreGeek/`；保留压缩包，未改demo文件。
- 通过 `python -B -` 在内存构造场景调用原函数，不创建策略、框架或测试源码，不产生Python字节码缓存。
- 使用本地回环地址和系统分配的临时端口启动原HTTP Handler，验证请求后关闭服务。没有访问外部判题器或启动完整比赛。
- 新增本目录8份Markdown。没有初始化Git、修改原始材料、安装依赖或实现框架。

## 2. 已观察结果

| 检查 | 实际结果 | 能说明什么 |
| --- | --- | --- |
| 原始request送入decide | 3条move：10010→(6,22)，10011→(9,13)，10012→(9,17) | 样例可被demo处理；不证明对应行动在裁判里成功 |
| sample敌墙(28,7)是否blocked | False | B01：可见敌墙未进入障碍集合 |
| `next_step(worker.pos)` | `KeyError: Pos(x=5,y=23)` | B06：起终点相同边界失败 |
| 25金币、双工可在不同目标造塔 | 两条build，gatling@(9,22)、railgun@(9,23) | B02：总需求50大于现有25 |
| 已在其他内圈位置有3塔 | 仍输出上述两条新建 | B03：没有全局塔数保护 |
| L2加特林、可操控、目标在射程 | 只输出1个targetPos | B04：与等级对应数量要求不符 |
| 工人100格满包铜、旁边石矿 | 输出collect | B05：近矿分支漏容量检查 |
| 两工各邻一座就绪炮，交换配对可双开火 | 两条move，分配距离为3、4 | 固定zip配对损失本轮可用火力 |
| `(10,24)`基地固定炮位 | (9,22),(9,23),(9,24) | 布局沿全局左侧成列 |
| 固定墙位数及去重 | 19个、去重后19个 | 留门实现没有重复墙位，不能误报此处有重复计数bug |
| 昼夜边界 | 0夜；1昼；70昼；71夜；130夜；131昼；200昼；201夜；1300夜 | demo采用1起算，不证明裁判也如此 |
| response原样JSON解析 | 第66行第13列 `Expecting ',' delimiter` | 样例缺逗号，不能原样当fixture |
| 仅在内存补逗号后保留重复键读取 | 17项、4个不同key；10010×10、10011×4、10012×2、10020×1 | 这是动作目录，标准dict会覆盖前项 |

### HTTP与耗时

原版Handler通过 `127.0.0.1` 临时端口收到完整样例：HTTP 200，顶层只有 `roleCommandMap`，含3条指令。收到非法JSON `{`：HTTP 200，同样只有 `roleCommandMap`，值为空对象。

此检查确认了本地异常兜底行为，没有验证裁判是否要求三个顶层字段、是否接受空动作或真实5秒预算内的最坏情况。

对同一request连续运行50次 `decide()`，使用 `time.perf_counter()`：

| 指标 | 本次测量 |
| --- | --- |
| 中位数 | 1.751 ms |
| P95，nearest rank，第48个排序样本 | 2.304 ms |
| 最大 | 2.501 ms |

这组数据不含网络传输、任务工具执行、全局搜索，也不是暖机／多地图严格性能基准。当前数据没有显示这个样例存在Python计算性能问题；不同机器复跑值可以不同。

## 3. 核心问题复现片段

从工作目录运行以下PowerShell片段。它只加载原样例和demo，在内存构造输入并输出诊断，不修改文件；输出包含故意触发的错误示例。

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

## 4. 材料指纹

首次分析时SHA256如下。后续更新原始资料时可以用它们识别是否仍在讨论同一版。

| 文件 | SHA256 |
| --- | --- |
| Demo/CoreGeek.tar.gz | `BA391BD7CB67751722FF48F4B4B566427590E6E22E08A28269EEAE31A4533C7A` |
| docs/任务书.md | `694810EE7562165BD3A7DDDA2516B031BD54D5F2EE39CF8DADF61BFECBD5D16E` |
| docs/接口文档.md | `931A65D09F881B30644225CA8D6B1BFBC7B2BEA7B4F6CE50D1F70CF858CB144D` |
| docs/request.txt | `5FC36943CE3D7BBCA678B6342CD4A6C092D91AE8032E3107E8E840BA8CC88D82` |
| docs/response.txt | `5DAF8278D7F097417B50082165A28DC4CE03163323730DC160EE4F7A534ED8D0` |

另用tar包内字节与解包文件逐一比较，7/7相同。本次解包增加可阅读的原版文件，不构成demo修改。

## 5. 没有验证的内容

没有运行官方判题器、整场1300回合对局、真实LLM任务、宝藏开箱或策略胜率评测。没有实测最优炮型、金币投资比例、机器人AI、跨武器弹道和任务速度奖励。

因此所有依赖这些信息的结论在其他文档中保持为条件推导或待验证项。不能用上述局部测试报告“游戏规则已经全部验证”或“新策略必胜”。

## 6. 2026-09-14 积分规则刷新核对

用户更新后的任务书§六以LaTeX明确完整任务公式 `S+5*T/t`、部分公式 `S*p`；本次同步的是分析文档，没有编辑用户更新的任务书，没有修改demo或新增业务代码。最初任务书指纹继续保留在§4，便于追溯原“公式排版损坏”的结论对应哪一版。

当前 `docs/任务书.md` SHA256：`1676C370B9FC829892B315505CB67B8B4C6DC76A170434573AFD47F1E321D585`。接口文档、两个请求／响应样例及demo压缩包的SHA256仍与§4首次记录一致。

本地使用Python标准库 `fractions.Fraction` 核算文档算例，以S=50、假设T=60计算：

- t=1/2/3/5/10/20/60时，完整任务分分别为350/200/150/110/80/65/55。
- 从t=2/10/20多等1轮，分值损失分别为50、30/11、5/7。
- 90%部分完成45分，t=10完整完成80分，差35分。

这些验证的是公式的数值计算，T=60不是样例请求提供的数据。未调用判题器，不证明t=1必然可达、t=T必然在超时前结算，也不解决取整、完成记账、真实任务正确率及胜率。保持§5关于未进行完整对局验证的限制。

## 7. 同伴评审与Git初始化

完整读取 `docs/同伴分析.txt` 的1253行，SHA256：`9034AC6EE7925381A9B4D159C8A86B094FEE799D9277C9E5311E3ECCD036D22D`。逐项对照当前源码、任务书和已有架构文档；原文不作修改，评审见[peer-review.md](peer-review.md)。

对评审中的角色—工作成本矩阵 `((1,2),(1,4))`，本地用 `itertools.permutations` 枚举两种一一匹配，总成本分别5和3；这验证数学算例，不表示已在真实地图跑过路线。

Git操作在用户明确给出的仓库范围内执行：初始化main，origin设为 `https://github.com/hughxx/uv.git`；初次远程读取成功且无refs。首个提交 `bf55c0c`（first commit）保存评审合入前的分析、原版demo、同伴原文和根目录README／Git配置文件。

原始demo、任务书、接口文档及txt材料使用 `.gitattributes` 禁止文本换行转换，便于按字节追溯。没有修改demo业务代码，没有安装依赖，没有从同伴文字推导新的已证实bug或胜率结论。后续评审增补单独提交，远程同步结果通过Git命令核对。
