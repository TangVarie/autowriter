# vendor/

从 **truth-vault** 原样复制过来的产物，**不要手改**。

| 文件 | 上游 | 说明 |
|---|---|---|
| `controlled_vocab_v0_2.json` | `truth-vault:schemas/controlled_vocab_v0_2.json` | 受控词表的机器可读权威导出。人类可读权威源是 TV 的 `docs/05-controlled-vocab.md`；TV 的 CI 保证那两者一致 |
| `controlled_vocab_v0_2.json.sha256` | 本地生成 | 上面那份文件的校验和。CI 校验副本没被手改 |

## 为什么 vendor 而不是手抄

TV 的 `onboarder/vocab.py` 已经是 `docs/05` 的一份手抄副本。再加一份跨仓手抄
必然漂移，而词表漂移的表现是**发牌抽到不存在的值**——不会立刻报错，会安静地
产出脏数据。所以这里存字节级副本 + 校验和，漂移变成"你的副本落后了"而不是
"两边悄悄不一样"。

## 怎么更新

TV 那边改了词表（`docs/05` → CI 保证同步到 JSON）之后：

```bash
curl -sS -o deskcore/vendor/controlled_vocab_v0_2.json \
  https://raw.githubusercontent.com/TangVarie/truth-vault/main/schemas/controlled_vocab_v0_2.json
python -c "import hashlib;raw=open('deskcore/vendor/controlled_vocab_v0_2.json','rb').read();\
open('deskcore/vendor/controlled_vocab_v0_2.json.sha256','w').write(hashlib.sha256(raw).hexdigest()+'  controlled_vocab_v0_2.json\n')"
```

然后跑 `python -m deskcore.cli selftest` 确认闭集没有意外变化，再提交。
决策见 TV 的 `DECISIONS.md` D-041 / `docs/10-sister-repo-followups.md` R-034。
