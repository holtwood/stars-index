#!/usr/bin/env python3
"""生成 StarsDB 索引仓库内容（从 data/ 下的 GitHub API 数据）

数据管线:
  scripts/fetch_stars.sh  → data/starred_full.json   (JSONL,每行一个 repo,含 starred_at)
  scripts/fetch_lists.py  → data/list_items.json     (list_id → [repo名])
  scripts/translate.py    → data/desc_zh.json        (full_name → 中文描述)
  scripts/gen_index.py    → README.md + docs/*.md + data/stars.json + _sidebar.md

用法:
  python3 scripts/gen_index.py
  python3 scripts/gen_index.py --check   # 用已提交的 data/stars.json 离线重建并逐字节校验

`--check` 不访问网络：它把 data/stars.json 当作输入快照，反推出生成器需要的
repo/lists 字段，在临时目录重建全部产物，再与仓库内已提交文件比较。正常生成
仍优先使用 fetch_stars.sh / fetch_lists.py 拉取的完整数据。
"""
import argparse
import difflib
import filecmp
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone

from config import CATEGORIES, ALL_INDEX, LANG_INDEX, OWNER, REPO, STAR_LISTS_URL, today

DATA_DIR = 'data'
DOCS_DIR = 'docs'
STARRED_FULL = f'{DATA_DIR}/starred_full.json'
LIST_ITEMS = f'{DATA_DIR}/list_items.json'
STARS_JSON = f'{DATA_DIR}/stars.json'
LISTS_META = f'{DATA_DIR}/lists_meta.json'

# --check 强制只读 data/stars.json，保证校验不受本机残留的抓取结果影响。
OFFLINE_ONLY = False

# ---------- 加载数据 ----------
def load_jsonl(path):
    items = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def load_repos():
    if not OFFLINE_ONLY and os.path.exists(STARRED_FULL):
        return load_jsonl(STARRED_FULL)

    # 离线回退：从已提交的 stars.json 还原生成器用到的字段。stars.json 是
    # 生成产物，但包含 full_name/html_url/描述/stars/lists/starred_at 等，
    # 足以重建 README 与 docs/；id、node_id 等生成器未使用的字段不还原。
    if not os.path.exists(STARS_JSON):
        sys.exit("缺少 data/starred_full.json 与 data/stars.json；"
                 "请先运行 scripts/fetch_stars.sh 拉取数据")
    with open(STARS_JSON, encoding='utf-8') as f:
        payload = json.load(f)
    repos = []
    for item in payload['repos']:
        repo = dict(item)
        repo['stargazers_count'] = repo['stars']
        repos.append(repo)
    return repos


def load_desc():
    """中文描述缓存:full_name → 中文描述（translate.py 生成）"""
    try:
        with open(f'{DATA_DIR}/desc_zh.json', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def load_lists():
    with open(LISTS_META, encoding='utf-8') as f:
        lists_meta = json.load(f)

    if not OFFLINE_ONLY and os.path.exists(LIST_ITEMS):
        id2name = {l['id']: l['name'] for l in lists_meta}
        list_items = json.load(open(LIST_ITEMS, encoding='utf-8'))
        # repo -> [list名] 与 反向映射 list名 -> [repo名]
        repo_lists = {}
        list_repos = {}
        for lid, items in list_items.items():
            name = id2name[lid]
            for item in items:
                repo_lists.setdefault(item, []).append(name)
                list_repos.setdefault(name, []).append(item)
        return repo_lists, list_repos

    # 离线回退：stars.json 每个 repo 自带 lists 字段；反向映射按字母序
    # 重建，与 fetch_lists.py 写出的 sorted(list_items) 保持一致。
    with open(STARS_JSON, encoding='utf-8') as f:
        payload = json.load(f)
    repo_lists = {}
    list_repos = {l['name']: [] for l in lists_meta}
    for repo in payload['repos']:
        names = repo.get('lists') or []
        repo_lists[repo['full_name']] = list(names)
        for name in names:
            list_repos.setdefault(name, []).append(repo['full_name'])
    return repo_lists, list_repos


# ---------- 格式化工具 ----------
def star(n):
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def esc(text):
    if not text:
        return ''
    # 清理 markdown 表格特殊字符
    return text.replace('|', '\\|').replace('\n', ' ').strip()


def status_mark(r):
    """仓库状态小标记:🟡 已归档 / 🔀 分叉 / 空(活跃)"""
    if r.get('archived'):
        return '🟡'
    if r.get('fork'):
        return '🔀'
    return ''


def zh_desc(r, desc_zh, limit=80):
    """中文描述优先;无缓存则取英文原文截断"""
    text = desc_zh.get(r['full_name']) or (r.get('description') or '').strip()
    if not text:
        return ''
    if len(text) > limit:
        text = text[:limit].rstrip() + '…'
    return text


def repo_row(r, desc_zh):
    mark = status_mark(r)
    name = f"{r['full_name']}{mark}" if mark else r['full_name']
    lang = esc(r.get('language') or '')
    return (f"| [{name}]({r['html_url']}) | ⭐ {star(r['stargazers_count'])} "
            f"| {lang} | {zh_desc(r, desc_zh)} |")


def bar(ratio, width=16):
    """ASCII 占比条"""
    filled = round(ratio * width)
    if ratio > 0 and filled == 0:
        filled = 1
    return '█' * filled + '░' * (width - filled)


def badge(label, value, color):
    """shields.io 静态徽章 URL"""
    label = label.replace('-', '--').replace(' ', '_')
    value = value.replace('-', '--').replace(' ', '_')
    return f"https://img.shields.io/badge/{label}-{value}-{color}"


# ---------- 数据一致性校验 ----------
def verify_consistency(repos, list_repos, counts):
    problems = []
    total = len(repos)
    sum_cats = sum(counts.values())

    covered = set()
    for cat in CATEGORIES:
        for lst in cat['lists']:
            covered.update(list_repos.get(lst, []))
    missing = [r['full_name'] for r in repos if r['full_name'] not in covered]
    if missing:
        problems.append(f"{len(missing)} 个 repo 未被任何分类覆盖,例如: {missing[:3]}")

    if sum_cats < total:
        problems.append(f"分类之和 {sum_cats} < 仓库总数 {total}")

    if problems:
        print('⚠️  数据一致性检查失败:')
        for p in problems:
            print('   -', p)
        sys.exit(1)

    overlap = sum_cats - total
    print(f'✅ 数据一致性校验通过: {total} 个仓库全部被覆盖')
    if overlap:
        print(f'ℹ️  有 {overlap} 个 repo 同时属于多个分类(正常现象,会重复出现在各分类页)')


# ---------- 生成分类页 ----------
TABLE_HEADER = "| 项目 | 星数 | 语言 | 描述 |\n|------|------|------|------|"


def build_category_files(repos_by_name, list_repos, counts, desc_zh):
    files = {}
    for cat in CATEGORIES:
        items = []
        seen = set()
        for lst in cat['lists']:
            for name in list_repos.get(lst, []):
                if name not in seen:
                    seen.add(name)
                    r = repos_by_name.get(name)
                    if r:
                        items.append(r)
        items.sort(key=lambda x: (-x['stargazers_count'],
                                x['full_name'].casefold(), x['full_name']))
        counts[cat['key']] = len(items)

        lines = [
            f"# {cat['emoji']} {cat['title']}",
            "",
            f"> {cat['desc']}",
            "",
            f"共 **{len(items)}** 个项目,按 ⭐ 排序",
            "",
            TABLE_HEADER,
        ]
        for r in items:
            lines.append(repo_row(r, desc_zh))
        lines += ["", "---", "[⬆ 返回顶部](#top)"]
        files[cat['key']] = '\n'.join(lines) + '\n'
    return files


# ---------- 生成全量索引 all.md ----------
def build_all_index(repos, desc_zh):
    items = sorted(repos, key=lambda x: (-x['stargazers_count'],
                                          x['full_name'].casefold(), x['full_name']))
    lines = [
        "# 📦 全量索引",
        "",
        f"> 全部 **{len(items)}** 个 Star 项目,按 ⭐ 排序",
        "",
        TABLE_HEADER,
    ]
    for r in items:
        lines.append(repo_row(r, desc_zh))
    lines += ["", "---", "[⬆ 返回顶部](#top)"]
    return '\n'.join(lines) + '\n'


# ---------- 生成按语言索引 by-language.md ----------
def build_lang_index(repos):
    by_lang = Counter(r.get('language') or '未标注' for r in repos)
    lines = [
        "# 🗣️ 按语言浏览",
        "",
        f"> 全部 **{len(repos)}** 个项目按语言分组,共 {len(by_lang)} 种语言",
        "",
        "| 语言 | 项目数 | 占比 |",
        "|------|--------|------|",
    ]
    languages = sorted(by_lang.items(), key=lambda kv: (-kv[1], kv[0].casefold(), kv[0]))
    for lang, cnt in languages:
        lines.append(f"| {esc(lang)} | {cnt} | {bar(cnt / len(repos))} |")
    lines += ["", "---", "[⬆ 返回顶部](#top)"]
    return '\n'.join(lines) + '\n'


# ---------- 生成最近收藏 ----------
def build_recent(repos, desc_zh, n=15):
    """按 starred_at 倒序取最近收藏"""
    starred = [r for r in repos if r.get('starred_at')]
    # 先按名字升序，再按 star 时间稳定倒序：同一时刻收藏的 repo 顺序可复现。
    starred.sort(key=lambda x: (x['full_name'].casefold(), x['full_name']))
    starred.sort(key=lambda x: x['starred_at'], reverse=True)
    lines = ["## 🔥 最近收藏", ""]
    for r in starred[:n]:
        date = r['starred_at'][:10]
        lines.append(f"- ⭐ [{r['full_name']}]({r['html_url']}) — {zh_desc(r, desc_zh, 60)} `{date}`")
    lines.append("")
    return '\n'.join(lines)


# ---------- 生成 README ----------
def build_readme(total, counts, list_count, repos, desc_zh, today_str):
    readme = []
    readme.append("# ⭐ StarsDB")
    readme.append("")
    readme.append(f"> 我的 GitHub Star 分类导航 · 由脚本自动生成 · 与 [Star Lists]({STAR_LISTS_URL}) 同步")
    readme.append("")
    readme.append(f"![项目数]({badge('Star 项目', str(total), '8A2BE2')}) "
                  f"![最后同步]({badge('最后同步', today_str, '2ea44f')}) "
                  f"![自动更新]({badge('自动更新', 'GitHub Actions', '007ec6')})")
    readme.append("")
    readme.append(f"> 📈 **{total}** 个项目 · **{len(CATEGORIES)}** 个分类 · **{list_count}** 个 Star Lists · 每日自动同步")
    readme.append("")
    readme.append(build_recent(repos, desc_zh))
    readme.append("## 📑 分类导航")
    readme.append("")
    readme.append("| 分类 | 项目数 | 占比 | 文档 |")
    readme.append("|------|--------|------|------|")
    for cat in CATEGORIES:
        cnt = counts[cat['key']]
        ratio = cnt / total if total else 0
        readme.append(f"| {cat['emoji']} {cat['title']} | {cnt} | `{bar(ratio)}` "
                      f"({ratio*100:.0f}%) | [docs/{cat['key']}.md](docs/{cat['key']}.md) |")
    readme.append("")
    readme.append("## 📊 快速入口")
    readme.append("")
    readme.append(f"- 📦 [全量索引({total})](docs/{ALL_INDEX}) — 所有项目按 ⭐ 排序")
    readme.append(f"- 🗣️ [按语言浏览](docs/{LANG_INDEX}) — 语言分布一览")
    readme.append("")
    readme.append("## 🔄 自动同步")
    readme.append("")
    readme.append("本仓库由 [GitHub Actions](.github/workflows/sync.yml) 每日自动拉取 Star 数据并重新生成,"
                  "也可在 Actions 页面手动触发 `workflow_dispatch`。")
    readme.append("")
    readme.append("### 本地手动更新")
    readme.append("")
    readme.append("```bash")
    readme.append("# 1. 拉取 stars 数据(需要 GitHub 认证)")
    readme.append("bash scripts/fetch_stars.sh")
    readme.append("# 2. 拉取 Star Lists 归属(需要认证)")
    readme.append("python3 scripts/fetch_lists.py")
    readme.append("# 3. 增量翻译新项目的中文描述(需要 NEWAPI_BASE_URL/NEWAPI_API_KEY 环境变量)")
    readme.append("python3 scripts/translate.py")
    readme.append("# 4. 重新生成 README 与 docs/")
    readme.append("python3 scripts/gen_index.py")
    readme.append("# 干净检出/无网络时：用已提交的 data/stars.json 离线重建并逐字节校验")
    readme.append("python3 scripts/gen_index.py --check")
    readme.append("```")
    readme.append("")
    readme.append("> 💡 翻译结果缓存在 `data/desc_zh.json` 随仓库提交,新 star 项目翻译前以英文原文显示。")
    readme.append("")
    return '\n'.join(readme) + '\n'


# ---------- 生成 data/stars.json(供前端/二次开发) ----------
def build_stars_json(repos, repo_lists, counts, desc_zh, generated_at):
    categories = {}
    for cat in CATEGORIES:
        categories[cat['key']] = {
            'title': f"{cat['emoji']} {cat['title']}",
            'desc': cat['desc'],
            'count': counts[cat['key']],
            'lists': cat['lists'],
        }
    payload = {
        'generated_at': generated_at,
        'total': len(repos),
        'categories': categories,
        'repos': [
            {
                'full_name': r['full_name'],
                'html_url': r['html_url'],
                'stars': r['stargazers_count'],
                'language': r.get('language'),
                'topics': r.get('topics', []),
                'description': r.get('description'),
                'description_zh': desc_zh.get(r['full_name']),
                'archived': r.get('archived', False),
                'fork': r.get('fork', False),
                'starred_at': r.get('starred_at'),
                'lists': repo_lists.get(r['full_name'], []),
            }
            for r in repos
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=1) + '\n'


# ---------- 生成 docsify 侧边栏 ----------
def build_sidebar():
    lines = ["- [🏠 首页](README.md)", "", "**分类**"]
    for cat in CATEGORIES:
        lines.append(f"  - [{cat['emoji']} {cat['title']}](docs/{cat['key']}.md)")
    lines += ["", "**索引**",
              f"  - [📦 全量索引](docs/{ALL_INDEX})",
              f"  - [🗣️ 按语言浏览](docs/{LANG_INDEX})"]
    return '\n'.join(lines) + '\n'


# ---------- 主流程 ----------
def write_outputs(out_dir, today_str, generated_at):
    repos = load_repos()
    repo_lists, list_repos = load_lists()
    desc_zh = load_desc()
    repos_by_name = {r['full_name']: r for r in repos}
    total = len(repos)
    list_count = len(list_repos)

    # 未分类 repo 自动归入「其他与杂项」(仅展示层,不写回 GitHub Lists)
    covered = set()
    for cat in CATEGORIES:
        for lst in cat['lists']:
            covered.update(list_repos.get(lst, []))
    uncovered = [r['full_name'] for r in repos if r['full_name'] not in covered]
    if uncovered:
        print(f"ℹ️  {len(uncovered)} 个未分类 repo 自动归入「其他与杂项」: {uncovered}")
        list_repos.setdefault('其他与杂项', []).extend(uncovered)
        repo_lists.setdefault('其他与杂项', []).extend(uncovered)
        for name in uncovered:
            repo_lists.setdefault(name, []).append('其他与杂项')

    counts = {}
    files = build_category_files(repos_by_name, list_repos, counts, desc_zh)
    files[ALL_INDEX] = build_all_index(repos, desc_zh)
    files[LANG_INDEX] = build_lang_index(repos)

    verify_consistency(repos, list_repos, counts)

    # 写分类页(分类 key 需补 .md,全量/语言索引 key 已带扩展名)
    outputs = []
    os.makedirs(os.path.join(out_dir, DOCS_DIR), exist_ok=True)
    os.makedirs(os.path.join(out_dir, DATA_DIR), exist_ok=True)
    for key, content in files.items():
        fname = key if key.endswith('.md') else f'{key}.md'
        rel = f'{DOCS_DIR}/{fname}'
        with open(os.path.join(out_dir, rel), 'w') as f:
            f.write(content)
        outputs.append(rel)

    # 写 README
    with open(os.path.join(out_dir, 'README.md'), 'w') as f:
        f.write(build_readme(total, counts, list_count, repos, desc_zh, today_str))
    outputs.append('README.md')

    # 写前端数据
    rel = f'{DATA_DIR}/stars.json'
    with open(os.path.join(out_dir, rel), 'w') as f:
        f.write(build_stars_json(repos, repo_lists, counts, desc_zh, generated_at))
    outputs.append(rel)

    # 写 docsify 侧边栏
    with open(os.path.join(out_dir, '_sidebar.md'), 'w') as f:
        f.write(build_sidebar())
    outputs.append('_sidebar.md')

    return outputs, total, counts


def _print_diff(committed, generated, rel, max_lines=12):
    with open(committed, encoding='utf-8') as f:
        expected = f.readlines()
    with open(generated, encoding='utf-8') as f:
        actual = f.readlines()
    diff = list(difflib.unified_diff(
        expected, actual, fromfile=rel, tofile=f'generated/{rel}', n=2))
    for line in diff[:max_lines]:
        print(f'     {line.rstrip()}')
    if len(diff) > max_lines:
        print('     ...')


def committed_readme_date():
    """已提交 README 的“最后同步”日期；生成日期不属于数据快照。"""
    try:
        with open('README.md', encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        return None
    match = re.search(r'最后同步-(\d{4})--(\d{2})--(\d{2})', text)
    if not match:
        return None
    return f'{match.group(1)}-{match.group(2)}-{match.group(3)}'


def check_outputs():
    """Offline rebuild from committed data/stars.json and byte-compare outputs."""
    global OFFLINE_ONLY
    if not os.path.exists(STARS_JSON):
        print('❌ --check 需要已提交的 data/stars.json 作为离线输入')
        return 1

    with open(STARS_JSON, encoding='utf-8') as f:
        snapshot = json.load(f)
    generated_at = snapshot['generated_at']
    # README 的“最后同步”是本地生成日期，可能因时区/跨午夜与 generated_at
    # 的 UTC 日期相差一天；校验时按已提交值注入，避免把日期元数据当成数据差异。
    today_str = committed_readme_date() or generated_at[:10]
    OFFLINE_ONLY = True

    with tempfile.TemporaryDirectory() as tmp:
        outputs, _, _ = write_outputs(tmp, today_str, generated_at)
        mismatches = []
        for rel in outputs:
            committed = rel
            generated = os.path.join(tmp, rel)
            if not os.path.exists(committed):
                mismatches.append((rel, '仓库内缺少该文件'))
            elif not filecmp.cmp(generated, committed, shallow=False):
                mismatches.append((rel, '内容不同'))

        if mismatches:
            print('❌ 离线重建结果与已提交产物不一致:')
            for rel, reason in mismatches:
                print(f'   - {rel}: {reason}')
                if os.path.exists(rel):
                    _print_diff(rel, os.path.join(tmp, rel), rel)
            return 1

    print(f'✅ 离线重建校验通过: {len(outputs)} 个文件与已提交产物逐字节一致')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='生成 StarsDB 索引')
    parser.add_argument('--check', action='store_true',
                        help='用已提交的 data/stars.json 离线重建并与仓库产物逐字节比较')
    args = parser.parse_args(argv)

    if args.check:
        return check_outputs()

    outputs, total, counts = write_outputs(
        '.', today(), datetime.now(timezone.utc).isoformat())
    print(f"✅ 生成完成: README.md + docs/ 下 {len(outputs) - 3} 个文件 + data/stars.json + _sidebar.md")
    print(f"   总项目: {total}")
    for cat in CATEGORIES:
        print(f"   {cat['key']}: {counts[cat['key']]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
