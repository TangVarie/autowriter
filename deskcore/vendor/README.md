# vendor/

从 **truth-vault** 原样复制过来的产物，**不要手改**。

| 文件 | 上游 | 说明 |
|---|---|---|
| `controlled_vocab_v0_2.json` | `truth-vault:schemas/controlled_vocab_v0_2.json` | 受控词表的机器可读权威导出。人类可读权威源是 TV 的 `docs/05-controlled-vocab.md`；TV 的 CI 保证那两者一致 |
| `controlled_vocab_v0_2.json.sha256` | 本地生成 | 上面那份文件的校验和。CI（`python -m deskcore.cli selftest`）与线上都校验副本没被手改：对不上时 `/health` 的 `config.vendored_vocab.ok` 和顶层 `ok` 都是 false（`/health` 本身仍返 200），`/ready` 返 **503**。所以 JSON 与 `.sha256` 必须在**同一个提交里**一起改 |
| `angle_priors_snapshot.json` | 本仓 `scripts/build_angle_priors.py` 从 TV 只读生成 | 发牌 v2 的先验快照（angle cards v2 §4）：每个 TV 项目的干净标签计数（内容形式 × 情绪杠杆 / × 问句与否）、发帖配比、品类、构建时间。**只有计数**，没有标题、正文、笔记 id（写盘前 `priors.snapshot_problems` 按白名单查）。TV 读不到时 `priors.get_profile` 用它（`ps=snap`）。每次部署重建；不是上游产物，没有 sha 校验，坏了就退到均匀分布 |

## 为什么 vendor 而不是手抄

TV 的 `onboarder/vocab.py` 已经是 `docs/05` 的一份手抄副本。再加一份跨仓手抄
必然漂移，而词表漂移的表现是**发牌抽到不存在的值**——不会立刻报错，会安静地
产出脏数据。所以这里存字节级副本 + 校验和，漂移变成"你的副本落后了"而不是
"两边悄悄不一样"。

## 怎么更新

TV 那边改了词表（`docs/05` → CI 保证同步到 JSON）之后，以下命令都在**仓库根目录**下跑
（路径与 `python -m deskcore.cli` 都是相对仓库根的）：

```bash
curl -sS -o deskcore/vendor/controlled_vocab_v0_2.json \
  https://raw.githubusercontent.com/TangVarie/truth-vault/main/schemas/controlled_vocab_v0_2.json
python -c "import hashlib;raw=open('deskcore/vendor/controlled_vocab_v0_2.json','rb').read();\
open('deskcore/vendor/controlled_vocab_v0_2.json.sha256','w').write(hashlib.sha256(raw).hexdigest()+'  controlled_vocab_v0_2.json\n')"
```

然后**看一眼上游到底改了什么**：

```bash
git diff deskcore/vendor/controlled_vocab_v0_2.json
```

`vocab.py` 是**硬取键**的：`sets` 下那八个集合（`emotional_lever` / `emotional_valence` /
`emotional_intensity` / `human_truth_archetype` / `trend_dependencies` / `content_format` /
`target_audience` / `intent`）、`trend_dependencies.exclusive_value`、
`derivations.lever_to_valence`、`boundary_rules.rules`——少任意一个，`import deskcore.vocab`
当场 KeyError，服务、CLI、测试全起不来。所以上游动了结构就得同步改 `deskcore/vocab.py`
（`surface_decay` 是 `.get` 取的，缺了不炸）。唯一一次真跑过这个流程（`cb2d340`）正是如此：
上游把逐值 `halflife_tier` 换成规则编码，`vocab.py` 跟着改了 8 行，那次提交是**三个**文件而不是两个。

最后跑 selftest，再提交：

```bash
python -m deskcore.cli selftest
```

⚠️ 它**替不了内容审查**。校验和是拿上一步刚生成的 `.sha256` 自己比自己，必然 OK。
它真正能挡的是三件事：

1. **新词表还能不能被 `vocab.py` 用**——上面那些硬取的键少一个就 KeyError、selftest 退 1；
2. `emotional_lever` 与 `derivations.lever_to_valence` 对不上；
3. 「通用」排他规则失效。

**其余闭集的增删值 selftest 一律 PASS**——某个 lever 被上游删掉、某个 archetype 改了名，
它不会说话。那部分只能靠上面那条 `git diff` 自己看。

决策见 TV 的 `DECISIONS.md` D-041 / `docs/10-sister-repo-followups.md` R-034。
