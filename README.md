# uv

《未来战争》编程竞赛的自研参赛工程、规则研究和策略设计。

当前已实现独立入口、盘面与回合记忆、空间规则、全队候选调度、动作校验和基础Playbook。可执行建塔、炮手到位／开火、采矿／卖矿、升级券购买／运输／使用、药品补给、用药／持包修墙、危险撤步和通用任务工具闭环。真实题型SOP、布局优化、新闻宝藏和完整对抗策略仍待实现，尚未通过网站对局验收。

## 工程结构

```text
CoreGeek/                 自研运行工程，压缩包内同名顶层目录
  main3.py                Python启动入口
  run.sh                  对战平台启动入口
  pyproject.toml          Python版本与包元数据
  src/agent/
    application.py        有状态决策入口与事务提交
    protocol.py           完整原始观测与响应字段
    server.py             HTTP传输与串行决策入口
    world.py              单位、资源、几何与可切换规则配置
    geometry.py           到交互站位集合的八方向寻路
    actions.py            全队动作及预算校验
    memory.py             回合回执、状态隔离和观测变化
    planning.py           候选组合、共享资源与统一效用
    playbooks.py          已注册的机会检测与打法定义
    economy.py            升级投资、采购及物流机会成本
    forecast.py           显式短期威胁假设与生存检查
    strategy.py           滚动重评、打法进度与结果核对
    tasks.py              接任务、跨轮工具关联和答案提交
scripts/package.py        标准库打包脚本
tests/                    协议、HTTP和独立包启动测试
dist/CoreGeek.tar.gz      生成的提交包，不纳入Git
docs/                     官方资料及技术文档
Demo/                     临时参考与诊断基线，功能验收后移除
```

自研代码不导入、不复制demo业务实现；运行包不包含Demo、文档、测试、私人笔记或本地环境。

## 启动

运行时需要Python 3.11及以上，无第三方运行依赖，不需要联网安装或执行pip。

Linux／对战平台在解压后的 `CoreGeek/` 内启动：

```sh
bash run.sh 9000
```

也可直接运行，Windows本地调试使用同一入口：

```sh
python CoreGeek/main3.py 9000
```

端口由平台传入，监听地址为 `0.0.0.0`。脚本不依赖调用时的工作目录；默认解释器为 `python3`，可用 `COREGEEK_PYTHON` 指定已有解释器。

完整盘面请求产生选定动作；仅含回合号的连通性探测或无可行行动时返回：

```json
{"roleCommandMap": {}, "prompt": "", "executeCmd": ""}
```

## 测试与打包

从仓库根目录运行：

```sh
python -B -m unittest discover -s tests -v
python -B scripts/package.py
```

默认生成 `dist/CoreGeek.tar.gz`，内含单一 `CoreGeek/` 顶层目录，与官方参考包一致。重复生成时显式覆盖：

```sh
python -B scripts/package.py --force
```

可用 `--output <路径.tar.gz>` 指定产物位置。打包仅收录三个顶层运行文件和 `src/agent/` 下非隐藏的Python源码；未来增加配置／数据资源时需显式扩展白名单及测试。脚本统一文本为UTF-8／LF，设置 `run.sh` 的执行权限，清除归档内的本机用户名、路径与时间元数据。

验证覆盖：完整响应字段、异常请求、官方请求样例、归档白名单与可复现性、解压后从无关目录独立启动及HTTP收发。Shell启动测试在可用Bash环境下运行；Windows＋Git Bash通过不等于已在平台Linux镜像通过。

策略回归覆盖共享金币／未来预留、炮手联合匹配、碰撞、满包、等级目标数、经营阶段推进以及新威胁触发打法交接。当前效用权重、12／20格建造掩码、部分弹道和风险预测均为显式基线假设，不代表已标定的最优策略。

联合选择默认先减少短期模型预测的角色死亡数，再比较Utility。模型暂按可见敌对机器人移动一格、攻击一次评估，不预支本回合击杀来免除反击；这是可关闭的保护策略，不是官方AI或真实死亡概率。当前任务仅允许确认不离开驻留区域的避让，无法确认时不会擅自放弃任务。

任务测试使用构造的LLM／沙盒返回，覆盖请求关联、重复观测、旧结果拒收、部分答案后继续改进和工具预算。沙盒命令仅经响应交给判题器；本地不执行任务命令。当前没有真实任务成功样例，通用闭环不等于已经掌握题型解法。

## 平台验收与demo移除条件

接口文档给出 `bash run.sh <port>` 和HTTP约定，但编译运行环境说明、网站解压目录与启动配置尚未取得。当前按官方包目录和已知接口准备，仍需网站试运行确认Python版本、启动工作目录、空动作响应及超时处理；本地启动成功不能代替平台验收。

Demo仅在以下条件满足后移除，不由打包脚本自动删除：

- 联合控制、经济、任务、战斗和机会驱动策略完成相应验收。
- 自研边界测试覆盖需要保留的demo问题，不再依赖demo源码或压缩包。
- 提交包通过目标环境和网站对局验证，相关文档引用完成迁移。

## 技术资料

- [技术文档索引](docs/analysis/README.md)
- [自研框架设计](docs/analysis/architecture.md)
- [事件驱动战术与实时修正](docs/analysis/opportunity-playbooks.md)
- [demo审查与复现证据](docs/analysis/demo-review.md)
- [任务书](docs/任务书.md)与[接口文档](docs/接口文档.md)

技术结论注明来源、成立条件及验证状态；官方资料保持原样，个人工作笔记不纳入版本控制。
