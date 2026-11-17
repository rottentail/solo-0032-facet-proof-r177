# Facet Proof 离线图版资料库（facetarchive）

把渲染服务数据根里的图版，按**观察轮次**固化成可离线浏览、可独立核对的资料库。
本包只用 Python 标准库，**不导入 `facetproof` 服务的任何代码**；接收者即使没有
原服务，也能复核每一张图版的收录依据。

## 解决的三个核心约束

1. **中断可续做，且不产生假成功。** 一轮采集先固定一份 SQLite 快照（只读
   `immutable` 打开），再逐项抓取内容寻址字节，每完成一项就落 `journal.json`。
   中断或失败后轮次保持 `staging`，不会出现在 `rounds/` 或账本里；再次运行只
   打开同一快照续做，绝不重抓另一个 SQL 时点，也不能更换来源数据根。
2. **一页内的图片、来源、审阅事实来自同一观察轮次。** 浏览页只在轮次**封存
   之后**由该轮不可变清单生成，页脚固定轮次号与 `manifest_sha256`。来源库之后
   新增或隔离图版只会产生**新一轮 + 差异记录**，旧轮只读、永不被改写或跨轮拼接。
3. **身份是 URN，不是图片摘要。** 目录以图版/曝光 URN 为身份。同画面的独立
   曝光即使六个通道字节逐字节相同，也各自成条，并在差异里标成
   `added_independent_exposure`；相同字节只在内容寻址库里物理存一份，绝不按
   摘要把独立曝光合并掉。

## 目录结构

```
<archive-root>/
├── index.html                 # 跨轮入口，链接到各自不可变轮次
├── ledger.json                # 只增、带哈希链的账本（封存完成的唯一标记）
├── diffs/                     # 相邻轮次差异（JSON + HTML，内容寻址、只增）
├── staging/<round_id>/        # 可续做工作区（快照、journal、已抓字节）
└── rounds/<round_id>/         # 封存后不可变
    ├── manifest.json          # 不可变收录清单（含来源、审阅、通道摘要、分组）
    ├── blobs/<xx>/<sha256>    # 内容寻址闭包，共享字节只存一份
    └── browse/                # 自包含静态站（相对路径，断网可看）
        ├── index.html         # 本轮总目录
        ├── parts/             # 按部件（装配）
        ├── lenses/            # 按镜头
        ├── plates/            # 单图版：三通道 + 来源 + 六通道摘要 + 审阅
        └── media/             # 轮次内图片实体（硬链接或复制，无符号链接）
```

## 命令

```sh
# 一轮可续做观察并封存（中断后重复同一命令即续做）
python3 -m facetarchive harvest --archive ./archive --source ./facet-data
# 查看封存轮次与 staging 续做进度
python3 -m facetarchive status  --archive ./archive
# 离线独立核对：账本哈希链、清单哈希、每个字节 sha256、PNG 解码、身份约束
python3 -m facetarchive verify  --archive ./archive
# 查看差异记录
python3 -m facetarchive diffs   --archive ./archive
```

`harvest` 的 `--stop-after N` 与 `--fail-checksum <sha>` 仅用于演示/测试中断恢复。

## 核对依据（接收者视角）

- `ledger.json`：每条目含 `prev_head`，重算 `entry_hash` 可验证整链；`head`
  必须等于链尾。
- 每轮 `manifest.json`：剔除 `manifest_hash` 后重算 sha256，须与字段一致，并与
  账本锚点一致。
- 每个通道引用的字节在 `blobs/` 内重算 sha256 与长度；PNG 由独立解码器
  （`pngcheck.py`）重新解码并核对尺寸。
- 身份约束：一个曝光 URN 至多一张图版；同画面独立曝光 URN 互不相同；闭包集合
  与实际引用完全一致，共享字节物理唯一。
- 差异记录剔除 `diff_hash` 后重算 sha256，两端 `manifest_hash` 须对得上。

任一项不符，`verify` 以退出码 2 和稳定错误码（如 `verify_blob`、
`verify_ledger`、`verify_manifest`）拒绝，绝不伪造通过。

## 端到端演示与测试

```sh
python3 tools/run_demo.py --workspace ./demo-workspace   # 含中断、失败恢复、两轮、篡改检测
python3 -m unittest tests.test_facetarchive              # 18 项自动化测试
```

素材生成（`tools/generate_materials.py`、`tools/mutate_source.py`）负责用真实
渲染服务制造来源；收录逻辑全部在本包内，与来源生成严格分离。
