# 标题翻译与简写链路改进方案

> 状态：已实施（commit 1/2/3 均已落地）
> 日期：2026-07-13
>
> 实施补充说明：
> - `_fetch_untranslated_articles`（`[auto-translate]` 回路）未加翻译退避——该
>   路径 `_translate_article_background` 不做校验、不产生拒绝，永远不会写入
>   `title_translation_error`，退避在此为死代码，故仅在做校验的 `[auto-title]`
>   路径（`_fetch_title_process_articles`）落地。
> - 顺带修复一个既有时区 bug：`title_summary_error_at`/`title_translation_error_at`
>   由 SQLite `datetime('now')`（UTC）写入，原代码用 `time.mktime`（本地时区）
>   解析，在 UTC+8 部署环境下退避窗口偏移 8h、几乎从不生效；改用
>   `calendar.timegm` 按 UTC 解析。见 `_within_error_backoff`。
> 背景：v5.0.1 上线后，自动标题翻译/简写链路陆续暴露出多个问题（跨语言校验误杀、
> 括号切分截断、失败无限重试等）。前几轮已完成校验规则修正（软/硬警告分级、
> `_repair_title_summary` 括号配对、字数上限放宽为软目标）。本方案在此基础上做
> 五项结构性加固，目标：**翻译后及简写后的标题尽量一次生成到位、合理完整，
> 失败时可控退避，不再产生残缺或反复无效重试。**

---

## 全景：现有链路

```
_auto_title_process_loop (每 10s)
  └─ _fetch_title_process_articles        # 扫当天文章，判断 translate/summary 需求
       └─ _process_article_title
            ├─ [需要翻译] translate_title()  → _validate_title_translation → 落库
            └─ [仍超长]   summarize_title()  → _parse_title_summary_result
                                             → _validate_ai_title_summary_result → 落库
```

痛点对应关系：

| # | 改进项 | 解决的问题 |
|---|--------|-----------|
| 1 | 翻译+简写合并调用 | 长英文标题两次 AI 调用、标题在用户眼前变两次 |
| 2 | summarize_title 改纯文本输出 | JSON 解析脆弱（内嵌引号破坏解析、退化成把 JSON 当标题） |
| 3 | 失败反馈重试 + 翻译退避 | 低温同 prompt 盲目重试必然同败，翻译失败无退避、无限烧 token |
| 4 | translate_title max_tokens 200→500 | 生成端截断（如「林赛·格雷厄姆 残句）的直接诱因之一 |
| 5 | 简写触发阈值与目标拉开 | 31 字标题也触发一次 AI 调用去"精简 1 个字" |

---

## 1. 翻译+简写合并为一次调用

**文件**：`ai_service.py`、`web_server.py`

- `ai_service.py` 新增方法：

  ```python
  def translate_and_condense_title(self, title, target_lang="zh-CN",
                                   max_chars=30, min_chars=18) -> str
  ```

  Prompt 要点（融合两个现有 prompt）：
  - 角色：资深新闻编辑 + 翻译；
  - 任务：将标题翻译为中文，若直译结果明显冗长，则按"三要素"（主体/动作/关键数字）
    在忠实前提下精简；
  - 字数**尽量** `min_chars`-`max_chars` 字，为保留关键信息/标点完整可以超出（与
    现行 summarize_title 的软约束口径一致）；
  - 成对标点必须完整；只输出一行纯文本标题（见改进 2，不用 JSON）。

- `web_server.py` `_process_article_title()` 分支调整：

  ```
  if translate_needed and _needs_title_summary(title) and summarize_enabled:
      → 走合并调用（一次 AI、一次校验、一次落库，title_source="title_summary"）
      → 同时 _save_ai_result(title_summary=最终标题)，让后续扫描命中缓存不再处理
  elif translate_needed:
      → 现行 translate_title 路径不变
  之后的独立简写分支逻辑保持（覆盖"原生中文长标题"场景）
  ```

- 注意：`_needs_title_summary()` 对英文原文判定依赖 `_title_total_chars > 40`
  这一条（英文无 CJK 字符），现行逻辑已覆盖，无需额外改动，但需在单测中固定
  这一行为，防回归。

**验收**：一条长英文标题从入库到显示只发生一次标题变更；ai_results 中
`title_summary` 有缓存；token 消耗减半（该场景）。

## 2. summarize_title / 合并调用改纯文本输出

**文件**：`ai_service.py`、`web_server.py`

- `summarize_title()` prompt 删除 "只输出 JSON" 与输出格式示例，改为：
  "只输出一行最终标题，不要任何解释、标签或代码块"。
- `_parse_title_summary_result()` **保留不动**：它对非 JSON 文本本来就有
  fallback（`legacy plain title summary`），改纯文本后自动走该分支；同时兼容
  旧缓存里可能存过的 JSON 串。
- AI 自评 `valid/reason` 字段随之取消——现状只有 `valid is False` 一个分支在用，
  实际拦截效果可忽略，服务端校验（`_validate_ai_title_summary_result`）才是
  真正的防线。

**验收**：日志中不再出现 `unparsed JSON payload` / JSON 解析退化类失败。

## 3. 失败反馈重试 + 翻译错误退避

**文件**：`ai_service.py`、`web_server.py`

### 3a. 带反馈的即时重试（翻译与简写通用）

- `translate_title()` / `summarize_title()` / 新合并方法均增加可选参数
  `feedback: str = ""`；非空时在 user message 末尾追加：
  `"注意：上一次输出存在问题（{feedback}），请重新输出完整、标点配对的标题。"`
- `_process_article_title()` 中校验失败后：
  1. 立即用失败 reason 作为 feedback 重试**一次**（temperature 可从 0.2 提到 0.5，
     避免低温复现相同输出）；
  2. 重试仍失败 → 进入退避（3b），本轮放弃。

### 3b. 翻译失败退避（对齐简写已有的 6 小时机制）

- `_init_ai_results_table()` 增加两列（沿用现有 ALTER 迁移模式）：
  - `title_translation_error TEXT`
  - `title_translation_error_at TEXT`
- 失败时 `_save_ai_result(article_id, title_translation_error=..., ..._error_at=now)`；
  成功后清空两列。
- `_fetch_title_process_articles()` 与 `_fetch_untranslated_articles()` 的
  `translate_needed` 判定增加：`title_translation_error_at` 距今 < 6h 则跳过
  （复用现有 `title_summary_error_at` 的时间解析代码，抽成小函数
  `_within_error_backoff(ts, hours=6)` 避免复制粘贴）。

**验收**：构造一条必然校验失败的标题，日志显示"1 次原始调用 + 1 次反馈重试"
后 6 小时内不再出现该文章的翻译尝试。

## 4. translate_title max_tokens 200 → 500

**文件**：`ai_service.py`（`translate_title` 的 `self.chat(..., max_tokens=200)`）

- 改为 500，与 `summarize_title` 一致；推理型模型（reasoning tokens 计入输出）
  或模型先吐引导语的场景不再把标题截断在半截。
- 成本影响可忽略（标题场景实际输出远小于上限）。

## 5. 简写触发阈值与目标拉开距离

**文件**：`web_server.py`

- 新增配置：

  ```python
  TITLE_SUMMARY_TRIGGER_RATIO = float(os.environ.get("TITLE_SUMMARY_TRIGGER_RATIO", "1.3"))
  ```

- `_needs_title_summary()` 改为：

  ```python
  cjk > int(TITLE_SUMMARY_MAX_CHARS * TITLE_SUMMARY_TRIGGER_RATIO)        # 默认 >39
  or total > int(TITLE_SUMMARY_MAX_TOTAL_CHARS * TITLE_SUMMARY_TRIGGER_RATIO)  # 默认 >52
  ```

- 效果：30–39 字的中文标题直接保留（CSS 两行截断兜底展示），只有明显超长的
  才动用 AI 简写；prompt 里的目标字数仍传 30，简写结果回到 30 字附近。
- 注意连锁：该函数同时被改进 1 的合并分支和 `_fetch_title_process_articles`
  使用，阈值改动自动全链路一致，无需分别调整。

---

## 实施顺序与提交划分

1. **commit 1**（低风险，先行）：改进 4 + 5（两处常量/阈值级改动）。
2. **commit 2**：改进 2（纯文本输出）+ 改进 3（反馈重试、退避列与判定）。
3. **commit 3**：改进 1（合并调用，依赖 2/3 的纯文本与重试基建）。

每步独立可部署、可回滚；DB 迁移只在 commit 2（加两列，幂等 ALTER，向前兼容）。

## 测试清单

- [ ] 单测：`_needs_title_summary` 新阈值边界（39/40 字、52/53 字符）。
- [ ] 单测：`_parse_title_summary_result` 对纯文本 / 旧 JSON 缓存 / 带前后缀
      废话文本三类输入的解析。
- [ ] 单测：`_within_error_backoff` 时间边界。
- [ ] 回放测试：用日志中三个真实案例（Hormuz 无数字、10,000/1万、
      「林赛·格雷厄姆 截断）跑校验与 repair，确认全部按预期通过/拦截。
- [ ] 部署后观察 `[auto-title]` 日志 24h：无 `unparsed JSON`、无同一
      article_id 的高频重试、软警告率明显下降。

## 风险与回滚

- 合并调用的 prompt 质量需实测调优（不同 provider 差异），若合并结果不佳，
  `_process_article_title` 保留分支开关即可退回两步式（不需要回滚代码，其余
  改进不受影响）。
- 退避可能延迟个别文章的翻译恢复（上游供应商抖动恢复后最长等 6h）；可通过
  环境变量把退避时长调短。
