# 变更记录：仓库更名为 stars-index

- 日期：2026-11-19
- 变更类型：命名与索引同步

## 内容

- GitHub 仓库 `holtwood/stars-db` 更名为 `holtwood/stars-index`，品牌名同步由
  `StarsDB` 改为 `StarsIndex`；`scripts/config.py`、`index.html` 与生成器标题已更新。
- 旧 Pages 地址 `https://holtwood.github.io/stars-db/` 不再由本仓提供；旧仓名保留为
  跳转页，指向 `https://holtwood.github.io/stars-index/`。
- 数据与分类口径未变：`data/stars.json` 仍为离线快照，`gen_index.py --check`
  继续逐字节校验 24 个生成文件。
