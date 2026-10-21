# AILT9019-gp38 项目框架计划（plan.md）

> **项目**：Campus Recycling Helper（品牌 → 包装部件 → 分类规则匹配）
> **Repo**：https://github.com/Richard-ZJC/AILT9019-gp38 ｜ 账号：u3648101@connect.hku.hk（Richard-ZJC）
> **本文件用途**：跨电脑、跨项目协作的**唯一事实源**。每完成一个任务，必须在同一 commit 里更新 §8 进度表；每次代码修改必须走 §6 测试流程。

---

## 0. 优先级分层（先读这个）

| 层 | 含义 | 内容 |
|---|---|---|
| 🟥 核心 must | 不做 = 不达验收 | 文本输入、规则匹配、AI 识别与解释、来源展示、安全拒答、双通道路由、严格响应模版、会话状态、README |
| 🟦 选作 should | **接口预留，实现不排优先级**；核心全绿且有余力再做 | 多模态识图、difflib 模糊识别、AI 搜索、AI 动态生成反问、SSE 流式、本地记忆落盘 |

> 铁律：选作模块**先定义接口与降级路径**（调用方按接口写），实现留 `NotImplementedError` 或返回 `status="UNAVAILABLE"` 的 stub；核心链路不得依赖选作模块的成功。

## 1. 项目一句话

用户输入品牌/产品名（如 "Vitasoy lemon tea"、"Nissin cup noodles"）→ AI 识别产品及包装部件（bottle / cap / label / cup / wrapper…）→ 与**固定规则集（16 条官方校园回收规则，来源 HKU *re* Campaign）**匹配 → 输出「每个部件的回收分类 + 匹配规则 + 来源」；识别不了 → 反问包装特征；规则集没有 → **安全拒答，绝不编造**。

## 2. 技术选型（目标：不同配置电脑都能直接用）

| 项 | 决定 | 理由 |
|---|---|---|
| 语言 | **Python ≥ 3.10** | macOS 自带；Windows 官网一键安装；无编译 |
| 第三方依赖 | **零依赖**（仅标准库 `http.server` / `urllib` / `threading` / `queue` / `difflib`） | 换电脑不用 `pip install`，克隆即跑 |
| 前端 | 单文件 HTML + 原生 JS | 浏览器直接打开，无 Node、无构建链 |
| AI 模型 | 学校提供的 key（阿里云百炼网关），双通道：**OpenAI 兼容通道（qwen3.8-flash）优先，15s 硬超时切 Anthropic 兼容通道**，均失败 → `SERVICE_UNAVAILABLE`；两路都**关闭 thinking**（实测同一 prompt 12–67s → 2–5s） | 仅做识别/解释/搜索，**不做规则来源** |
| AI 调用 | HTTP 直连 REST（`urllib`，支持非流式与 SSE 两种模式） | 核心用非流式，流式为选作 |
| AI 响应 | **严格 JSON 模版 + schema 校验**（§4.4） | 输出不可控 = 不可测试 |
| 输出语言 | **英文优先**（UI / 报告 / 分类 / 来源 / 拒答话术全英文），中文输入支持为 should-have | 目标用户为英文使用者 |
| 异步处理 | 🟦 选作：标准库 `threading` 双路（SSE 推前端 + 本地记忆落盘） | 接口预留为 `stream=True` 参数 |
| 规则存储 | `rules.json` | 纯数据、可 diff、可直接做测试用例 |
| API key | 学校 key 写入 `config.json`（模板入库，真身 .gitignore） | 不硬编码、不入库 |

**用户启动方式（验收「1–2 条命令」）**：`python server.py` → 浏览器打开 `http://localhost:8000`。

## 3. 目录结构（代码管理分类）

```
AILT9019-gp38/
├── plan.md                 # 本文件：优先级 / 进度 / 规范
├── README.md               # Setup / start / API key / sample questions / supported items（英文）
├── server.py               # 入口：本地 HTTP 服务器（标准库）
├── app/
│   ├── config.py           # 🟥 config.json 加载 + 环境变量覆盖
│   ├── factory.py          # 🟥 组装 pipeline（唯一知道所有模块的地方）
│   ├── session.py          # 🟥 会话状态唯一持有者：session_id / 轮次 / 上下文 / 待答问题
│   ├── pipeline.py         # 🟥 编排：0成本层 → AI 层 → match 唯一裁决 → 输出
│   ├── ai_plugin/
│   │   ├── router.py       # 🟥 通道路由（provider 列表注入，10s 首 token 超时，超时不重试）
│   │   ├── openai_client.py    # 🟥 非流式；🟦 stream=True 分支
│   │   ├── anthropic_client.py # 🟥 同上
│   │   ├── base.py         # 🟥 urllib POST / SSE，错误归一化
│   │   ├── mock_client.py  # 🟥 离线确定性子（无 key 时的 demo/测试替身）
│   │   └── schema.py       # 🟥 响应模版 + 校验（含 options ⊆ features.json、id ∈ rules.json）
│   ├── rules_loader.py     # 🟥 加载 + 校验 rules.json（缺字段 fail fast）
│   ├── matcher.py          # 🟥 精确关键词匹配（0 成本层第一级）
│   ├── match.py            # 🟥 **唯一裁决口**：对照 rules.json 出 category + source
│   ├── identify.py         # 🟥 组装 prompt + 调 router + 解析模版
│   ├── fallback.py         # 🟥 固定选项反问（features.json），2 轮上限
│   ├── refusal.py          # 🟥 拒答（本地 hazards + 未匹配 + 双通道全挂）
│   ├── explain.py          # 🟥 解释文本（核心走合并调用，见 §4.8）
│   ├── fuzzy.py            # 🟦 选作：difflib 模糊识别，未实现返回 None
│   └── memory.py           # 🟦 选作：本地记忆落盘，未实现为 no-op
├── data/
│   ├── rules.json          # 16 条规则：id / keywords / parts / materials / category / source / effective_date / action
│   ├── features.json       # 反问选项唯一来源（bottle/cup/bag/box/wrapper/can/jar…）
│   └── memory/             # 🟦 运行时生成（.gitignore）
├── web/
│   └── index.html          # 单页前端：输入 + 结果卡片（英文 UI）
├── tests/
│   ├── test_brand_items.json      # 10 个品牌物品
│   ├── test_low_confidence.json   # 5 个低置信度场景
│   ├── test_unsupported.json      # 5 个不支持/有害物品
│   ├── test_ai_plugin.py          # mock provider：路由/模版/降级/session 轮次
│   ├── run_tests.py               # 三组用例评测，输出 pass/fail
│   ├── smoke_test.py              # 冒烟：服务器起 + 三类路径各 1 条
│   └── live_ai_test.py            # 真实 API（需 key+网络，不进冒烟）
├── config.example.json     # providers 顺序 / key / save_memory / timeout
└── .gitignore              # config.json、data/memory/、__pycache__
```

## 4. 各模块细节

### 4.1 rules_loader.py
- 字段：`id / keywords / material / category / source（官方文件名+URL）/ effective_date / action`
- `action`: `RECYCLE | REFUSE`（有害品/大件直接内置为 REFUSE 条目，**不靠 AI 判**）。
- 启动校验：字段缺失、`source` 为空、category 不在白名单 → 直接报错退出（fail fast）。

### 4.2 session.py（会话状态唯一持有者）🟥
- 内存 dict：`session_id -> {rounds, pending_question, context, created_at}`；单机本地运行，不落盘也成立（落盘属选作 memory）。
- 所有入口（`/identify`、`/answer`）必须带 `session_id`；缺失则新建并回传。
- `fallback` 的 2 轮上限**在此计数**，超过 → `REFUSE`（前端绕过无效，因为计数在服务端）。
- 过期清理：30 分钟未访问即丢（避免内存无限增长）。

### 4.3 ai_plugin/router.py（双通道路由）🟥

```
请求 → OpenAI 兼容（15s 硬超时：非流式，覆盖整个生成，不是首 token）
        ├─ 成功 → 返回（原始 JSON 文本）
        └─ 超时/报错 → Anthropic 兼容（同样 15s）
                        ├─ 成功 → 返回
                        └─ 超时/报错 → status=SERVICE_UNAVAILABLE
                                       文案："This request involves sensitive information and cannot be processed right now."
```
- **超时只 failover，不重试**（最坏 30s，而非 60s）；仅「JSON 解析失败」重试 1 次（快失败）。
- `providers` 顺序来自 `config.json`（默认 `["openai","anthropic"]`），**构造注入**，便于测试 mock 与将来加第三家。
- client 统一签名：`complete(messages, image=None, stream=False) -> str | Iterator[str]`。
- 每次响应记录 `raw_model`，便于评测分组统计。

### 4.4 响应模版（严格 JSON + schema 校验）🟥

```json
{
  "status": "OK | CLARIFY | REFUSE | SERVICE_UNAVAILABLE",
  "confidence": 0.0,
  "product": {"name": "Vitasoy lemon tea", "brand": "Vitasoy"},
  "components": [{"part": "bottle", "material": "plastic", "confidence": 0.9}],
  "clarify": {"question": "Which packaging do you see?", "options": ["bottle", "cup", "bag", "box"]},
  "explanations": {"bottle": "Plastic bottles go into the plastic recycling bin..."},
  "search_used": false,
  "source_hint": null,
  "raw_model": "openai | anthropic"
}
```

校验规则（`schema.py` 逐条实现，失败即降级）：
1. `status` ∈ 四态；`confidence` ∈ [0,1]；字段类型正确。
2. `clarify.options` ⊆ `features.json`（**唯一来源**，AI 不得自创选项）。
3. `source_hint` 必须是 `rules.json` 里存在的 id，否则置 null（**AI 提名、代码验证**）。
4. 解析失败 → 重试 1 次 → 仍失败 → 降级 `CLARIFY`（宁可反问，不可瞎猜）。

### 4.5 0 成本层（在 AI 之前）🟥 / 🟦
- 🟥 **精确匹配**：`matcher.py` 对 `rules.json` 的 keywords 做大小写归一 + 命中判定；命中直接进 `match.py`，**完全不发 API**。
- 🟦 **模糊匹配（选作）**：`fuzzy.py` 用 `difflib.SequenceMatcher` 对关键词做近似（阈值 0.8）；未实现时直接 `return None`，pipeline 视为未命中，落回 AI 路径。
- 判据：0 成本层命中 → API 调用次数必须为 0（有专门测试）。

### 4.6 match.py（唯一裁决口）🟥
- **所有路径必经**：0 成本命中、AI OK、CLARIFY 回填后重算，都汇到这里。
- 输入：部件/特征 + 可选 `source_hint`；输出：`category + source`（或 `REFUSE`）。
- 裁决优先级：`source_hint`（已校验存在）→ 关键词/材质匹配 → 未命中 → `REFUSE`。
- 未命中话术："This packaging is not covered by the current rule set."

### 4.7 fallback.py（防瞎猜）🟥
- 只渲染 `clarify.question + options`（选项来自 `features.json`，英文文案）。
- 用户选择后：`session.context` 累积 → 重跑 `match.py`（不必再调 AI）。
- 🟦 选作升级：由 AI 动态生成更口语化的反问句（接口相同，`clarify.source = "ai" | "fixed"`）。

### 4.8 explain.py 🟥
- **核心实现：合并调用**——identify 的 prompt 中注入 top-3 候选规则原文，AI 在同一个 JSON 里返回 `explanations`（每个候选一句），`match.py` 选定哪条就直接取用，**只花 1 次 API 调用**。
- 兜底：若 `explanations` 缺失/不合规 → 回退为「规则原文 + 来源」直接展示（不发第二次请求）。
- 备选方案（不采用，除非合并效果差）：match 之后再调一次 explain（延迟翻倍）。

### 4.9 server.py + web/index.html 🟥
- 接口（核心，非流式）：
  - `POST /identify` `{session_id?, text}` → 完整 JSON 结果
  - `POST /answer` `{session_id, choice}` → 完整 JSON 结果
  - `POST /identify/stream` 🟦 **接口预留**：未实现时返回 `501 {status:"UNAVAILABLE"}`；实现后推 SSE 进度事件（`stage: identifying|matching|explaining`）+ 最终结果单事件
- 前端：输入框 + 结果卡片（部件 / 分类 / 规则摘要 / 来源链接），**英文 UI**；拒答红色提示；🟦 图片上传入口预留（`<input type="file">` 存在但请求不带图时走文本路径）。
- ⚠️ 流式必须用 `fetch` + `ReadableStream`，`EventSource` 不支持 POST。

### 4.10 选作模块接口契约（先定接口，实现留空）
| 模块 | 接口 | 未实现时行为 | 降级影响 |
|---|---|---|---|
| `identify(image=...)` | `complete(messages, image=b64, stream=False)` | 忽略 image 字段 | 仅文本可用 |
| `fuzzy.match(text)` | `-> RuleMatch \| None` | 返回 None | 全部落 AI 路径 |
| `router.complete(..., stream=True)` | `-> Iterator[str]` | 抛 `NotImplementedError` | 前端走非流式 |
| `memory.record(entry)` | `-> None` | no-op | 无本地记忆 |
| `clarify.ai_question` | `-> str \| None` | 返回 None | 用固定文案 |

### 4.11 不做的事（红线）
不发明规则；不覆盖有害垃圾/大件家具/电子垃圾；不冒充官方系统；AI 输出永远附规则来源；**AI 永远不是分类裁决者**（match.py 对照 rules.json 才是）。
（存储红线修订为「本地会话记忆 + `save_memory=false` 可关闭」，需同步回 proposal。）

## 5. 语言与文案规范

| 对象 | 语言 |
|---|---|
| UI 文案、分类名、来源、拒答话术、README、demo | **英文** |
| AI prompt 与 schema 字段 | **英文**（保证模型输出稳定） |
| `rules.json` / `features.json` 内容 | **英文**（source 用官方英文名；中文官方名可加 `source_zh` 备查） |
| 代码注释 / commit message | 英文 |
| plan.md（本文件）/ 团队内部沟通 | 中文 |

中文查询支持（should-have）：输入层做简单归一（繁简/空格/全半角），不改变输出语言。

## 6. 测试策略（每次修改必走）

固定流程：**改代码 → 写/更新用例 → `run_tests.py` → `smoke_test.py` → 全绿才 commit → 同 commit 更新 §8 进度表**

| 层级 | 内容 | 通过线 |
|---|---|---|
| 冒烟 smoke_test.py | 服务器启动、`/identify` 与 `/answer` 各 1 条 200、拒答路径固定话术、`/identify/stream` 未实现时返回 501 | 全过 |
| AI 插件 test_ai_plugin.py（mock，不真调） | ① 模版 schema 校验 ② OpenAI 超时→切 Anthropic ③ 双超时→SERVICE_UNAVAILABLE ④ 解析失败→重试 1 次→降级 CLARIFY ⑤ **超时路径不重试**（调用计数=1）⑥ `source_hint` 非法 id → 置 null ⑦ `options` 越界 → 降级 ⑧ session 第 3 轮 clarify 被拒 | 8 项全过 |
| 0 成本层 | 精确命中时 **API 调用计数 = 0** | 必过 |
| 用例 run_tests.py | 品牌 10 条（正确分类+来源）；低置信 5 条（转反问不瞎猜）；不支持 5 条（安全拒答） | 各组 ≥ 80% |
| 回归 | 改任何模块后全部用例重跑 | 不低于修改前 |

> `live_ai_test.py`（真 key + 网络）单独跑，**不进冒烟**，保证无 key 电脑也能全绿。

## 7. Git 协作规范

- 分支：`main`（稳定，测试全绿才合入）+ `feat/<任务ID>-<名称>`
- commit：`[T3] feat: matcher with zero-API fast path`；进度表更新放同一 commit
- **改完即存**：每个修改批次结束就 `git add` + commit（+ push），不留未提交的工作区；一次 commit 只做一件事，便于 `git revert`
- 换电脑：先 `pull` 再改；plan.md 冲突手动合并（唯一事实源）
- plan.md 定稿后 push 到仓库根目录

## 8. 任务进度追踪

图例：⬜ 未开始 ｜ 🔶 进行中 ｜ ✅ 完成（测试通过）｜ ⏸️ 阻塞 ｜ 🟦 选作（接口预留，实现延后）

| ID | 任务 | 层 | 对应 Proposal | 状态 | 测试用例 | 备注 |
|---|---|---|---|---|---|---|
| T0 | 仓库骨架（目录 / .gitignore / config.example） | 🟥 | W5 | ✅ | smoke_test | 目录齐全，`python server.py --mock` 可跑 |
| T1 | rules.json 初版（10–15 条，英文，含来源+日期+action） | 🟥 | W5 | ✅ | rules_loader 校验 | 实为 **16 条**（HKU photo guide 组件粒度更细）；来源已核对 wastereduction.hku.hk |
| T2 | rules_loader.py（含 REFUSE 条目校验） | 🟥 | W6–8 | ✅ | 校验用例 | fail-fast：缺字段/空来源/未知 category 直接退出 |
| T2a | session.py（session_id / 轮次 / 上下文 / 过期） | 🟥 | W6–8 | ✅ | 测试⑧ | 2 轮上限在服务端，30 分钟过期 |
| T3 | matcher.py + match.py（唯一裁决口） | 🟥 | W6–8 | ✅ | 品牌组 + 0成本计数 | 0 成本命中 → API 调用计数 = 0 |
| T4 | openai_client.py + anthropic_client.py（非流式） | 🟥 | W6–8 | ✅ | 测试① | 两头已用真实 key 实测（百炼网关） |
| T4a | router.py（注入 providers / 10s 超时不重试 / failover） | 🟥 | W6–8 | ✅ | 测试②③⑤ | 线程硬超时，超时只 failover |
| T4b | schema.py（模版 + options/source_hint 校验 + 降级） | 🟥 | W6–8 | ✅ | 测试①④⑥⑦ | 非法 hint→null；越界 option→固定清单 |
| T4c | pipeline.py 编排（0成本→AI→裁决→输出） | 🟥 | W6–8 | ✅ | run_tests | 四态 + `mode` 字段标明路径 |
| T4d | identify.py（合并解释的 prompt） | 🟥 | W6–8 | ✅ | 品牌组 | 1 次调用出解释，失败回退规则原文 |
| T5 | fallback.py 固定选项反问 + 2 轮上限 | 🟥 | W6–8 | ✅ | 低置信组 | 选项唯一来源 features.json（12 项，含 cup / cup-paper、box / box-foam 的材质区分） |
| T6 | refusal.py（本地 hazards + 未匹配 + 全挂） | 🟥 | W6–8 | ✅ | 不支持组 | 四类拒答话术集中一处 |
| T7 | server.py + web/index.html（英文 UI，非流式） | 🟥 | W6–8 | ✅ | smoke_test | 9/9 冒烟通过；`/identify/stream` 501 |
| T8 | README（英文：setup / key / samples / supported） | 🟥 | W6–8 | ✅ | — | 含 16 条规则来源表与日期口径 |
| T9 | 正式评测（10+5+5）并修错 | 🟥 | W9–10 | ✅ | run_tests | 生产路径 19/20、AI-only 19–20/20、mock 20/20 |
| T10 | Demo + 文档定稿 | 🟥 | W11–12 | ⬜ | run_tests | 对齐 W12 验收 |
| T11 | 🟦 图片上传 + 多模态识别 | 🟦 | should | ⬜ | 新增图例用例 | 接口已在 T4d 预留 |
| T12 | 🟦 fuzzy.py difflib 模糊识别 | 🟦 | should | ⬜ | 相似词用例 | 未实现返回 None |
| T13 | 🟦 SSE 流式 + 异步双路（threading） | 🟦 | should | ⬜ | 流式/进度事件 | `/identify/stream` 501→200 |
| T14 | 🟦 memory.py 本地记忆落盘 | 🟦 | should | ⬜ | 落盘/关闭开关 | 与 proposal 红线同步 |
| T15 | 🟦 AI 搜索 + AI 动态反问文案 | 🟦 | should | ⬜ | 搜索标记用例 | `search_used` |

### 8.1 本轮实现记录（与计划的偏差，需团队确认）

| 项 | 计划 | 实际 | 原因 |
|---|---|---|---|
| 规则条数 | 10–15 | **16** | HKU photo guide 是组件级（杯身/杯盖/杯套分开），10 条压不住；超出 1 条 |
| 规则 `effective_date` | 官方生效日 | **核对日 2026-10-21** | 官网未标注生效日；已写入 `meta.note`，等 Estates Office 文件号确认后替换 |
| 新增 `parts` / `materials` / `instruction` 字段 | 无 | 有 | `match.py` 需要部件+材质做裁决；`instruction` 是解释的兜底文本 |
| 结果新增 `mode` 字段 | 无 | 有 | 前端/评测要区分 exact / ai / clarify / refuse，才能验证「0 成本命中不发 API」 |
| mock provider | 无 | `--mock` 与 `providers:["mock"]` | 无 key 的电脑要能跑冒烟与 demo；它只替代模型，pipeline/schema/裁决全部照跑 |
| 低置信阈值 | 未量化 | 组件最高置信度 < 0.5 → CLARIFY | 给「宁可反问」一条可执行判据 |
| 部分命中 | 未规定 | 至少 1 个部件命中 → OK，其余部件标 `UNCOVERED` | 整包拒答会丢有效信息；未命中部件仍显示固定拒答话术 |
| 超时口径 | 10s 首 token | **15s 整请求** | 核心走非流式，拿不到首 token 时刻；百炼 flash 实测 3–7s，10s 太紧 |
| 模型 | gpt-4o-mini | **qwen3.8-flash**（百炼网关） | 课程发的 key 是百炼；可用模型清单里无 OpenAI 原生模型 |
| thinking | 未考虑 | 两路都关闭（`enable_thinking:false` / `thinking:{type:disabled}`） | 同一 prompt：开 12–67s，关 2–5s；JSON 抽取不需要思维链 |
| `extra_body` | 无 | 有 | 网关私有开关不能写进通用客户端，否则真 OpenAI/Anthropic 会 400 |
| 测试开关 | 无 | `pipeline.use_fast_path` + `run_tests --no-fast-path` | 不关掉关键词层，品牌组永远走不到模型，等于没测 AI |
| 反问选项粒度 | 部件级（cup / box …） | 部分部件加材质变体（`cup` vs `cup-paper`、`box` vs `box-foam`） | 模型只会问「塑料还是纸」，但只有部件级选项时它就只给 1 个答案；schema 现要求 ≥2 个选项，否则回落完整清单 |

**验证状态**：`run_tests.py` mock 20/20 · `run_tests.py --live` 19/20 · `run_tests.py --live --no-fast-path` 19–20/20 · `test_ai_plugin.py` 8/8 · `smoke_test.py` 9/9。唯一不稳定用例是 `l03 "some snack wrapper"`：模型有时直接判 plastic packaging，有时反问——属真实判断题，非链路 bug。

## 9. 里程碑（对齐 proposal timeline）

- **Week 5**：T0–T1 完成；学校 key 注入；双通道方案定稿
- **Weeks 6–8**：T2–T8 核心链路跑通（10 covered / 5 low-conf / 5 unsupported 均能走通）
- **Weeks 9–10**：T9 实测修错；**核心全绿后再考虑 T11–T15**
- **Weeks 11–12**：T10 定稿；选作能进多少进多少，不进不阻塞验收
