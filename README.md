# Perovskite-filtered RSS from Inoreader OPML

这个文件包用于把 Inoreader 导出的 OPML 订阅源重新筛选成一个新的 RSS：只保留标题、摘要或内容中包含钙钛矿相关关键词的文章。

## 已包含文件

- `Inoreader Feeds 20260502.opml`：你上传的 OPML 文件，当前包含 56 个 RSS 源。
- `filter_perovskite_rss.py`：核心筛选脚本。
- `include_keywords.txt`：保留关键词。
- `exclude_keywords.txt`：弱排除关键词，用于减少地质、矿物、陶瓷类误报。
- `requirements.txt`：Python 依赖。
- `.github/workflows/update_rss.yml`：GitHub Actions 自动更新脚本。

## 本地运行

```bash
pip install -r requirements.txt
python filter_perovskite_rss.py --opml "Inoreader Feeds 20260502.opml" --output filtered_perovskite.xml
```

运行后会生成：

```text
filtered_perovskite.xml
```

这个文件就是筛选后的 RSS。

## 发布成 Zotero 可订阅链接

1. 在 GitHub 新建一个公开仓库，例如：`perovskite-rss-filter`。
2. 上传本文件包中的所有文件。
3. 进入仓库的 `Settings` → `Pages`。
4. `Build and deployment` 选择 `Deploy from a branch`。
5. Branch 选择 `main`，目录选择 `/root`。
6. 进入 `Actions`，手动运行一次 `Update filtered perovskite RSS`。
7. 之后你的 RSS 链接通常是：

```text
https://你的GitHub用户名.github.io/perovskite-rss-filter/filtered_perovskite.xml
```

把这个链接添加到 Zotero 的 RSS 订阅中即可。

## 修改关键词

想放宽或收紧筛选，只需要编辑：

```text
include_keywords.txt
exclude_keywords.txt
```

例如，如果只想要太阳能电池方向，可以在 `include_keywords.txt` 中保留：

```text
perovskite solar cell
perovskite solar cells
perovskite/silicon
perovskite tandem
wide-bandgap perovskite
metal halide perovskite
```

如果想尽量不漏文献，就保留宽泛关键词：

```text
perovskite
perovskites
钙钛矿
```
