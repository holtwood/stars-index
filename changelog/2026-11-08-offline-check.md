# 变更记录：索引生成链支持离线重建与逐字节校验

- 日期：2026-11-08
- 变更类型：可复现性修复

## 内容

- `scripts/gen_index.py`
  - 新增 `--check`：强制以已提交的 `data/stars.json` 为输入快照，在临时
    目录重建全部产物，再与仓库内 `README.md`、`docs/*.md`、`_sidebar.md`、
    `data/stars.json` 逐字节比较；不一致时输出统一 diff 并以 1 退出。
    README 的“最后同步”日期按已提交值注入，避免 `generated_at` 的 UTC
    日期与本地生成日期在跨午夜/时区场景相差一天而被误报为内容差异。
  - 当 `data/starred_full.json` / `data/list_items.json` 缺失时，从
    `data/stars.json` 的 `repos[].stars` 与 `repos[].lists` 还原生成器
    所需字段；正常流程仍优先使用抓取到的完整数据。
  - 分类页、全量索引、语言索引和“最近收藏”增加确定性的并列排序键
    （星数 → 名称 casefold → 名称），使相同星数的项目顺序不再依赖
    抓取输入顺序。
  - README 的本地更新说明补充 `--check` 用法。
- 新增 `.github/workflows/check.yml`：在 push / PR 上无网络、无 PAT 地
  执行 `python3 scripts/gen_index.py --check`。
- 按新的确定性顺序重新生成 `docs/ai.md`、`docs/all.md`、
  `docs/by-language.md`、`docs/lang.md`、`docs/learning.md`、
  `docs/misc.md`；`README.md` 与 `data/stars.json` 除 README 增加
  `--check` 说明外无内容变化。

## 影响

- 干净检出不再需要先执行 `fetch_stars.sh` / `fetch_lists.py` 才能重建或
  校验已提交的索引。
- 正常每日同步仍按原流程抓取最新数据并重新生成；`:--check` 只读已提交
  快照，不会访问网络。
- 并列星数的展示顺序被规范化（按项目名排序），这是一次性的文档顺序变化，
  不改变项目集合、分类或描述。

## 验证

- `python3 scripts/gen_index.py --check`：24 个产物与已提交文件逐字节一致，
  退出码 0。
- 在仓库副本中向 `docs/misc.md` 追加内容后再次 `--check`：报告
  `docs/misc.md: 内容不同` 并退出 1。
- 在仓库副本中删除抓取输入后直接运行 `python3 scripts/gen_index.py`：
  走离线回退，生成成功。
- 未运行：GitHub Actions 工作流本体（本机无法执行 Actions）。
