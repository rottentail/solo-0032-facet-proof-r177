"""把已封存清单渲染成自包含的静态浏览目录。

所有页面只描述同一个轮次清单，并在页脚固定轮次号与 manifest 哈希；图片以
相对路径引用轮次内 blobs，整目录拷走、断网也能浏览。绝不从活动来源或其他
轮次取数，因此一页内的图片、来源与审阅事实天然属于同一观察轮次。
"""
from html import escape
from pathlib import Path
import os
import shutil

from .canonical import canonical

STYLE = """
:root{--ink:#1b2430;--muted:#5b6776;--line:#d7dde5;--accent:#1d5f8a;--bg:#f6f8fa;}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
 color:var(--ink);background:var(--bg);line-height:1.55}
header{background:#0f2740;color:#fff;padding:18px 28px}
header h1{margin:0;font-size:20px}
header .meta{color:#b9cde0;font-size:13px;margin-top:6px}
main{max-width:1180px;margin:24px auto;padding:0 24px}
.crumbs{font-size:13px;color:var(--muted);margin-bottom:14px}
.crumbs a{color:var(--accent);text-decoration:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px}
.card h3{margin:0 0 8px;font-size:15px}
.card img{width:100%;border:1px solid var(--line);border-radius:6px;background:#fff}
.thumbs{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.thumbs figure{margin:0}
.thumbs img{width:100%;border:1px solid var(--line);border-radius:6px}
figcaption{font-size:12px;color:var(--muted);text-align:center;margin-top:4px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}
th{background:#eef2f6}
code,.hash{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.hash{word-break:break-all;color:#33475b}
.section{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:18px}
.section h2{margin:0 0 12px;font-size:16px}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;
 background:#e6eef6;color:#17456b;border:1px solid #c6d8ea}
.badge.warn{background:#fbeee6;color:#8a4b1d;border-color:#eccbb2}
.badge.ok{background:#e8f4ec;color:#1f6b3a;border-color:#c2e0cd}
footer{color:var(--muted);font-size:12px;padding:18px 28px;border-top:1px solid var(--line);margin-top:30px}
a.platelink{color:var(--accent);text-decoration:none}
ul.tight{margin:6px 0;padding-left:18px}
ul.tight li{margin:3px 0}
"""


def _e(value):
    return escape("" if value is None else str(value))


def _page(title, inner, manifest, rel_prefix, crumbs=""):
    footer = (
        f"<p>观察轮次 <code>{_e(manifest['round_id'])}</code> ｜ "
        f"收录清单 <code>manifest.json</code> ｜ "
        f"manifest_sha256 <span class='hash'>{_e(manifest['manifest_hash'])}</span></p>"
        f"<p>本页全部图片、来源与审阅事实均取自上述同一观察轮次的固定快照，"
        f"不与其他轮次或活动来源混合。</p>")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title><style>{STYLE}</style></head>
<body>
<header><h1>{_e(title)}</h1>
<div class="meta">Facet Proof 离线图版资料库 · 部件 / 镜头 / 图版</div></header>
<main>
<div class="crumbs">{crumbs}</div>
{inner}
</main>
<footer>{footer}</footer></body></html>"""


def _blob_rel(round_browse_dir, blob_tree, checksum, name):
    """把图片实体落到 browse 内（硬链接优先），返回相对 HTML 路径。"""
    src = blob_tree / checksum[:2] / checksum
    media_dir = round_browse_dir / "media" / checksum[:2]
    media_dir.mkdir(parents=True, exist_ok=True)
    dst = media_dir / f"{checksum}-{name}"
    if not dst.exists():
        try:
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)
        os.chmod(dst, 0o444)
    return os.path.relpath(dst, round_browse_dir)


def _hash_rows(plate):
    rows = []
    for name in sorted(plate["passes"]):
        desc = plate["passes"][name]
        rows.append(f"<tr><td><code>{_e(name)}</code></td>"
                    f"<td class='hash'>{_e(desc['sha256'])}</td>"
                    f"<td>{_e(desc['size'])}</td></tr>")
    return "".join(rows)


def render_site(browse_dir, manifest, blob_tree):
    browse_dir = Path(browse_dir)
    browse_dir.mkdir(parents=True, exist_ok=True)
    plates = {p["urn"]: p for p in manifest["plates"]}

    _render_index(browse_dir, manifest)
    for part in manifest["groups"]["parts"]:
        _render_part(browse_dir, manifest, part, plates, blob_tree)
    for lens in manifest["groups"]["lenses"]:
        _render_lens(browse_dir, manifest, lens, plates, blob_tree)
    for plate in manifest["plates"]:
        _render_plate(browse_dir, manifest, plate, blob_tree)


def _render_index(browse_dir, manifest):
    parts = "".join(
        f"<li><a href='parts/{_e(g['part_slug'])}.html'>{_e(g['name'] or g['part_slug'])}"
        f" <span class='badge'>{len(g['plate_urns'])} 张图版</span></a></li>"
        for g in manifest["groups"]["parts"])
    lenses = "".join(
        f"<li><a href='lenses/{_e(g['lens_slug'])}.html'>"
        f"{_e(g['projection'])} 镜头 <code>{_e(g['lens_slug'])}</code> "
        f"<span class='badge'>{len(g['plate_urns'])} 张图版</span></a></li>"
        for g in manifest["groups"]["lenses"])
    inner = f"""
<div class="section"><h2>按部件（装配）浏览</h2><ul class="tight">{parts}</ul></div>
<div class="section"><h2>按镜头浏览</h2><ul class="tight">{lenses}</ul></div>
<div class="section"><h2>本轮概览</h2>
<table><tr><th>图版数</th><td>{len(manifest['plates'])}</td></tr>
<tr><th>内容块（去重后）</th><td>{len(manifest['blob_closure'])}</td></tr>
<tr><th>来源快照事件序号</th><td>{_e(manifest['event_sequence'])}</td></tr>
<tr><th>封存时间(UTC)</th><td>{_e(manifest['sealed'])}</td></tr></table></div>"""
    html = _page(f"轮次 {manifest['round_id']} 总目录", inner, manifest, "",
                 "<a href='index.html'>总目录</a>")
    (browse_dir / "index.html").write_text(html, encoding="utf-8")


def _thumb(browse_dir, manifest, plate, name, blob_tree):
    checksum = plate["passes"][name]["sha256"]
    rel = _blob_rel(browse_dir, blob_tree, checksum, name)
    plate_rel = f"../plates/{plate['plate_id']}.html"
    return (f"<figure><a href='{plate_rel}'><img src='../{rel}' alt='{_e(name)}'></a>"
            f"<figcaption>{_e(name)}</figcaption></figure>")


def _render_part(browse_dir, manifest, group, plates, blob_tree):
    out = browse_dir / "parts"
    out.mkdir(parents=True, exist_ok=True)
    cards = []
    for urn in group["plate_urns"]:
        plate = plates[urn]
        thumbs = "".join(_thumb(browse_dir, manifest, plate, n, blob_tree)
                         for n in ("color.png", "depth.png", "object.png"))
        cards.append(
            f"<div class='card'><h3><a class='platelink' href='../plates/{_e(plate['plate_id'])}.html'>"
            f"{_e(plate['plate_id'])}</a> <span class='badge {('ok' if plate['state']=='ready' else 'warn')}'>"
            f"{_e(plate['state'])}</span></h3>"
            f"<div class='thumbs'>{thumbs}</div>"
            f"<p style='font-size:12px;color:#5b6776;margin:8px 0 0'>"
            f"镜头 <code>{_e(plate['seal']['lens_slug'])}</code> ｜ "
            f"{_e(plate['width'])}×{_e(plate['height'])} ｜ "
            f"曝光 <code>{_e(plate['exposure']['dedup_key'])}</code></p></div>")
    inner = (f"<div class='section'><h2>部件：{_e(group['name'] or group['part_slug'])} "
             f"<code>{_e(group['part_slug'])}</code></h2>"
             f"<p>装配 URN <code>urn:facet:assembly:{_e(group['assembly_id'])}</code></p>"
             f"<div class='grid'>{''.join(cards)}</div></div>")
    crumbs = "<a href='../index.html'>总目录</a> / 部件"
    html = _page(f"部件 {group['name'] or group['part_slug']}", inner, manifest, "../", crumbs)
    (out / f"{group['part_slug']}.html").write_text(html, encoding="utf-8")


def _render_lens(browse_dir, manifest, group, plates, blob_tree):
    out = browse_dir / "lenses"
    out.mkdir(parents=True, exist_ok=True)
    cards = []
    for urn in group["plate_urns"]:
        plate = plates[urn]
        thumbs = "".join(_thumb(browse_dir, manifest, plate, n, blob_tree)
                         for n in ("color.png", "depth.png", "object.png"))
        cards.append(
            f"<div class='card'><h3><a class='platelink' href='../plates/{_e(plate['plate_id'])}.html'>"
            f"{_e(plate['plate_id'])}</a></h3><div class='thumbs'>{thumbs}</div>"
            f"<p style='font-size:12px;color:#5b6776;margin:8px 0 0'>"
            f"部件 <code>{_e(plate['seal']['part_slug'])}</code> ｜ "
            f"{_e(plate['width'])}×{_e(plate['height'])}</p></div>")
    inner = (f"<div class='section'><h2>镜头：{_e(group['projection'])} "
             f"<code>{_e(group['lens_slug'])}</code></h2>"
             f"<p>lens_hash <span class='hash'>{_e(group['lens_hash'])}</span><br>"
             f"eye <code>{_e(group['eye'])}</code> ｜ target <code>{_e(group['target'])}</code></p>"
             f"<div class='grid'>{''.join(cards)}</div></div>")
    crumbs = "<a href='../index.html'>总目录</a> / 镜头"
    html = _page(f"镜头 {group['lens_slug']}", inner, manifest, "../", crumbs)
    (out / f"{group['lens_slug']}.html").write_text(html, encoding="utf-8")


def _review_block(plate):
    if not plate["review_facts"]:
        return "<p>本观察轮次内该图版暂无审阅决定事实。</p>"
    rows = []
    for fact in plate["review_facts"]:
        payload = fact["payload"]
        rows.append(
            f"<tr><td><code>{_e(fact['decision_id'])}</code></td>"
            f"<td><span class='badge {('ok' if fact['state']=='accepted' else 'warn')}'>"
            f"{_e(fact['state'])}</span></td>"
            f"<td>{_e(payload.get('note',''))}</td>"
            f"<td>{_e(payload.get('decision_note',''))}</td>"
            f"<td class='hash'>{_e(payload.get('visual_hash',''))}</td></tr>")
    return ("<table><tr><th>审阅 URN</th><th>决定</th><th>开立案注</th>"
            f"<th>决定注记</th><th>开立时 visual_hash</th></tr>" + "".join(rows) + "</table>")


def _source_block(plate):
    placements = "".join(
        f"<li><code>{_e(p['key'])}</code>：{_e(p['mesh_name'])} "
        f"(<code>{_e(p['mesh_id'])}</code>, rev {_e(p['mesh_revision'])}) ＋ "
        f"{_e(p['pigment_name'])} (<code>{_e(p['pigment_id'])}</code>, "
        f"rev {_e(p['pigment_revision'])})</li>" for p in plate["seal"]["placements"])
    lens = plate["seal"]["lens"]
    return f"""
<table>
<tr><th>图版 URN</th><td class='hash'>{_e(plate['urn'])}</td></tr>
<tr><th>曝光 URN</th><td class='hash'>{_e(plate['exposure_urn'])}</td></tr>
<tr><th>幂等键</th><td><code>{_e(plate['exposure']['dedup_key'])}</code> ｜ 尝试 {_e(plate['exposure']['attempt'])} 次 ｜ 状态 {_e(plate['exposure']['state'])}</td></tr>
<tr><th>冻结快照</th><td><code>urn:facet:seal:{_e(plate['seal']['seal_id'])}</code><br>
seal_hash <span class='hash'>{_e(plate['seal_hash'])}</span></td></tr>
<tr><th>部件</th><td>{_e(plate['seal']['assembly_name'])} <code>{_e(plate['seal']['part_slug'])}</code></td></tr>
<tr><th>镜头</th><td>{_e(lens['projection'])} <code>{_e(plate['seal']['lens_slug'])}</code> ·
 lens_hash <span class='hash'>{_e(plate['seal']['lens_hash'])}</span></td></tr>
<tr><th>请求</th><td><code>{_e(canonical(plate['request']))}</code></td></tr>
<tr><th>画面 visual_hash</th><td class='hash'>{_e(plate['visual_hash'])}</td></tr>
</table>
<div class="section" style="margin-top:12px"><h2>冻结闭包内的部件与材质</h2><ul class="tight">{placements}</ul></div>"""


def _render_plate(browse_dir, manifest, plate, blob_tree):
    out = browse_dir / "plates"
    out.mkdir(parents=True, exist_ok=True)
    figures = []
    for name in ("color.png", "depth.png", "object.png"):
        checksum = plate["passes"][name]["sha256"]
        rel = _blob_rel(browse_dir, blob_tree, checksum, name)
        figures.append(
            f"<figure><img src='../{rel}' alt='{_e(name)}'><figcaption>{_e(name)}</figcaption></figure>")
    inner = f"""
<div class="section"><h2>三通道预览 <span class="badge {('ok' if plate['state']=='ready' else 'warn')}">{_e(plate['state'])}</span></h2>
<div class='thumbs'>{''.join(figures)}</div></div>
<div class="section"><h2>来源事实（同一冻结快照）</h2>{_source_block(plate)}</div>
<div class="section"><h2>六个通道的内容寻址依据</h2>
<table><tr><th>通道</th><th>sha256</th><th>字节数</th></tr>{_hash_rows(plate)}</table></div>
<div class="section"><h2>审阅事实（轮次快照内）</h2>{_review_block(plate)}</div>"""
    crumbs = ("<a href='../index.html'>总目录</a> / "
              f"<a href='../parts/{_e(plate['seal']['part_slug'])}.html'>部件</a> / 图版")
    html = _page(f"图版 {plate['plate_id']}", inner, manifest, "../", crumbs)
    (out / f"{plate['plate_id']}.html").write_text(html, encoding="utf-8")


def render_archive_root(archive_root, ledger, round_entries):
    """在资料库根生成跨轮入口：每个链接都落到各自不可变轮次，不混合内容。"""
    rows = []
    for entry in reversed(round_entries):
        rows.append(
            f"<li><a href='rounds/{_e(entry['round_id'])}/browse/index.html'>"
            f"{_e(entry['round_id'])}</a> ｜ 图版 {_e(entry['plate_count'])} ｜ "
            f"内容块 {_e(entry['blob_count'])} ｜ 封存 {_e(entry['sealed'])}<br>"
            f"<span class='hash'>manifest {_e(entry['manifest_hash'])}</span></li>")
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Facet Proof 离线图版资料库</title><style>{STYLE}</style></head>
<body><header><h1>Facet Proof 离线图版资料库</h1>
<div class="meta">按观察轮次封存；每轮独立不可变，互不混合</div></header><main>
<div class="section"><h2>观察轮次（最新在上）</h2><ul class="tight">{''.join(rows) or '<li>尚无封存轮次</li>'}</ul></div>
<div class="section"><h2>核对入口</h2><ul class="tight">
<li>只增哈希链账本：<code>ledger.json</code></li>
<li>各轮收录清单：<code>rounds/&lt;round_id&gt;/manifest.json</code></li>
<li>差异记录：<code>diffs/</code></li>
<li>离线核对命令：<code>python3 -m facetarchive verify ...</code></li>
</ul></div></main></body></html>"""
    path = Path(archive_root) / "index.html"
    path.write_text(html, encoding="utf-8")
    return path
