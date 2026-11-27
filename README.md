# ontology-builder

**把「宽表 + 业务术语 + 上一版本体」变成「下一版本体 + 变更清单」。**

它不是生成器，是合并器。名字里的 builder 指「帮人把本体建对」，不指「自动生成」。

零依赖，只用 Python 标准库（`sqlite3` / `json`）。

## 为什么不能全自动

本体要写的东西按「数据能不能验」分四档：

| 档 | 例子 | 能不能自动 |
|---|---|---|
| DDL 写死的 | 表、列、类型、主键、声明过的外键 | **能**，跑一遍只是复核（能不能免批另算，见下面那张白名单） |
| 要从数据里认的 | 外键、grain、基数 | **只能给候选**。数据能验的是「值都对得上」，验不了「这就是外键」——见下面 TPC-DS 的实测 |
| basic 指标（单列聚合） | `gmv = SUM(pay_amount)` | 半自动，扫列 + 人确认名字和别名 |
| derived 指标与约束 | 客单价 = `gmv / order_cnt`；`do_not`：含退款、未扣优惠券 | **不能，只能人写** |

最后一档是根本证据：「分母含退款单，不要用 `gmv` / `order_cnt` 手工相除来复算」——建表语句里没有，数据字典里没有，从数据里也推不出来。所以这个工具的上限是**产候选**，签发永远是人。

**「能不能免批」这一条，我改过三次，每次都因为一条判据解释不了摆在眼前的事。**

第一版写的是「数据能自己确认的（外键、基数、grain）免批」，错在把**声明**和**推断**当成一回事：DDL 写了 `foreign key (x) references t(y)`，那就是外键；probe 算出「x 的值全都落在 t.y 里」，那是一条统计事实。下面那张表里，只靠这条统计事实，1796 个候选里只有 102 个是对的。

第二版改成「声明式证据免批、推断式一律签」。它能解释那张表，但解释不了另一件事：**给已有对象补一个列、给 `gmv` 补一个别名，也是从数据里推断的，可它不该要人签。** 而且它漏了一整类：覆盖一个已经签过值的改动，证据再硬也不该免。

第三版是三问的合取：位置空 ∧ 影响局部 ∧ 证据声明式。逻辑上没毛病，问题在形态——**它是推理**。三条凑齐了结论未必成立，而且你没法证明「凑齐了」。更实际的问题是第二问：它靠一个 `impact` 字段，而那个字段由产出候选的模块自己填，它填 `local` 就是 `local`。等于自己给自己发通行证。

所以现在是白名单，不是三问：

| # | 候选 | 证据形态 | 凭什么可以免 |
|---|---|---|---|
| 1 | `object` | `ddl_table` | DDL 里有这张表，本体只是给它一个 id |
| 2 | `grain` | `ddl_pk` | DDL 写了 `PRIMARY KEY`，抄一遍 |
| 3 | `foreign_key` | `ddl_fk` | DDL 写了 `FOREIGN KEY`，抄一遍 |

**列在表里就免，没列就签。** 三条的共性不是「声明式」，是**零新语义**——删掉这条候选，本体里没有任何已确认的结论会变意思。换成 `data_containment`，证据再硬也不免：包含率 100% 这件事，两个互不相干的代理键也做得到。

白名单之外只剩一道检查：**那个位置本来是空的吗**。被覆盖的已签值从库里消失，没地方找回来，所以这条压过一切。

**生产者只能报形态，不能报结论。** 原来 `Candidate` 带一个 `declared: bool`，由 `probe` 自己填 `True`；现在它只能报「我的证据是 `ddl_fk`」这种枚举值，能不能免由表查。形态有据可查（DDL 里真有那句 `foreign key`），结论没有。

**白名单会自我强化，所以每条都得带依据。** 第一次有人签了某情形，第二次就免了；半年后没人说得清能不能删，这张表就退化成台账。这也是「这张表将来要搬进 `gate`、改它要人签」的原因。

**basic 和 derived 必须分开，因为它们的错不是一种错。** basic 写错了，跑一遍数据就露馅；derived 写错了（比如同店 GMV 的「同店」范围没写），**所有测试仍然是绿的**——它无声退化成 `gmv`，名字还叫「同店」。所以 `metric` 有一个必填的 `kind`，见 [SPEC §2.1](SPEC.md)。

所以有四条硬规矩：

1. **挖掘永远写不进本体。** 候选进 `candidate` 表，改本体只能走 `commit`。这不是流程约定，是表结构。
2. **免批只认白名单**：列在表里的才免，没列的一律进草稿；另外那个位置必须本来是空的。
3. **确认是字段级的，不是整条的。** 人往往只确认了 `do_not` 那一句，没确认 `bindings`。整条打标会把机器猜的东西洗成人签的。
4. **重跑不许冲掉人签过的。** 所以「上一版已确认本体」是必选输入，不是可选项。

## 现在能跑什么

`probe` 和 `seal` 的写入口（`commit` / `export`）已经落地，`mine` / `gate` / `seal.propose` 还没写。

```bash
python -m ontology_builder init      onto.db
python -m ontology_builder import    onto.db fixtures/ontology.v2.json
python -m ontology_builder show      onto.db
python -m ontology_builder export    onto.db out.json            # --version 1 可取历史版本
python -m ontology_builder candidate onto.db --kind object --evidence-form ddl_table \
    --payload '{"type":"table_object","name":"x"}'

# probe：schema + 数据 → 候选（只写 candidate 表）
# schema 先按下一节补进来
python -m ontology_builder probe onto.db \
    --schema fixtures/schema/tpcds.sql --data /tmp/tpcds.db --report /tmp/cands.json
```

跑测试：

```bash
python -m pytest tests/ -q      # 66 passed, 3 skipped
```

跳过的 3 个是 TPC-DS 实测，需要两份**不属于本仓库**的 DDL——TPC 对那份规范保留版权，
本仓库是 MIT，放一起是两种许可打架，注明来源也解决不了。补上再跑：

```bash
python -m bench.fetch_tpcds --from /path/to/tpcds-kit/tools   # 本地已有 kit
python -m bench.fetch_tpcds                                   # 没有的话，它告诉你从哪拿

python -m pytest tests/ -q      # 69 passed
python -m bench.measure         # 造一次迷你库约 5 秒
```

`show` 的样子：

```
版本：
  * v1  2026-11-27T09:32:23  import ontology.v2.json

当前 v1：16 个对象 · 16 条边
  business_object  store                        2 字段，其中 1 个已确认
  dimension        same_store_set               4 字段，其中 0 个已确认
  metric           gmv                          5 字段，其中 2 个已确认
  metric           refund_rate                  6 字段，其中 0 个已确认
  metric           same_store_gmv               6 字段，其中 0 个已确认
  ...
边：
  metric:same_store_gmv -[restricted_to]-> dimension:same_store_set
  metric:same_store_gmv -[inputs]-> metric:gmv
  metric:gmv -[computed_from]-> table_object:dwd_order_di
  event:single_store_refund_spike -[evidenced_by]-> metric:refund_rate
```

派生链在图上能走通：`same_store_gmv → gmv → dwd_order_di`。

## probe 的判据在 TPC-DS 上是什么样

**答案来自仓库外面。** `fixtures/schema/tpcds.sql` 是 TPC 官方的 25 张表建表语句，`tpcds_ri.sql` 是官方的 102 条参照完整性约束。**这两份不随仓库分发**（见上一节），`tpcds_ri.sql` 只当答案，不喂给 probe——喂进去 recall 立刻变 100%，那个数是假的。

数据是我照着这 102 条约束造的迷你实例（`bench/tpcds.py`，store_sales 2 万行）。造数据的人知道答案，所以下面**只有 recall 能信，precision 只能看方向**（SPEC §6.1）。

| 判据组合 | 候选 | 命中 | 漏 | recall | precision |
|---|---|---|---|---|---|
| A 只数据判据（包含率 ∧ 目标唯一） | 1796 | 102 | 0 | 1.000 | 0.057 |
| B ＋名字同源 | 80 | 69 | 33 | 0.676 | 0.863 |
| C ＋名字＋缩写表（3 条） | 135 | 102 | 0 | 1.000 | 0.756 |

三行各说明一件事：

**A：数据判据一条都不漏，但也没法用。** 1796 个候选里 1694 个是错的——因为代理键都是 1..N，「`ss_item_sk` 的值全落在 `c_customer_sk` 里」在数据上完全成立。这不是阈值调得不严，是**包含率本身分不出「语义指向」和「值域撞车」**。

**B：名字救回来，代价是漏三分之一。** 33 条漏报全是同一类：`addr` 对 `address`、`cdemo` 对 `demo`、`hdemo` 对 `demo`。

**C：一张三条的缩写表把 recall 补满，precision 掉回 0.756。** 因为 `cdemo` 和 `hdemo` 都被规范成 `demo`，两个人口统计维表互相串了 24 条——**缩写表救人，也让人分不清**。

顺带一个对照（不是测量，是演示）：**假如这个库自己声明过外键**，那 102 条是 `ddl_fk`，全走 `exempt`，probe 只需要人看剩下的 11 条。免批这条规矩的意义全在这里——**DDL 里声明越多，要签字的人越少**；一个什么都没声明的库，就只能靠人把 1796 条过一遍，而人是过不完 1796 条的。

### 同一份 schema，库里多一版，免批就少两条

同一份 schema、同一份数据、同一次 probe，只因为库里多了一版本体：

| | exempt | sign | 差别在哪 |
|---|---|---|---|
| 空库 | 151 | 11 | — |
| 已有一版（`customer` 的 grain 已签过） | 149 | **13** | `customer` 的 object 和 grain 各一条翻成 sign |

那 11 条是官方约束里没声明、但数据上成立的（`c_last_review_date_sk → d_date_sk` 这类），一直要签。多的那 2 条不是新发现，是**位置已经被占**：`table_object:customer` 上一版就在库里了，`customer` 的 grain 有人签过，所以这一轮提的不是「新增」而是「改一个签过的东西」。

**这就是为什么 `route` 不能在 `probe` 里算。** 判「位置空不空」要查库，而 `probe` 只看得见 schema 和一份数据——它不知道这一版本体里哪个位置已经有人签过。所以候选只报 `evidence_form`，`route` 在写库那一刻算（`store.add_candidates`）。

自己验一遍，三种结果：

```bash
# 白名单第 1 条 + 位置空 → exempt
python -m ontology_builder candidate onto.db --kind object \
    --evidence-form ddl_table --payload '{"type":"table_object","name":"new_table"}'

# 还在白名单里，但那个位置上一版就在库里 → sign
python -m ontology_builder candidate onto.db --kind object \
    --evidence-form ddl_table --payload '{"type":"table_object","name":"customer"}'

# 不在白名单（从数据里算出来的证据）→ sign，证据多硬都不行
python -m ontology_builder candidate onto.db --kind foreign_key \
    --evidence-form data_containment \
    --payload '{"table":"a","column":"k","ref_table":"b","ref_column":"k","predicate":"joins_on"}'
```

还有两件从这张表里长出来的事：

- **同一对列会出两条相反方向的候选**（`a.k ⊆ b.k` 和 `b.k ⊆ a.k` 同时成立）。判据分不出谁指向谁，这个方向也只能人签。我没有加去重规则把它藏起来。
- **官方答案本身不完整。** 剩下的 9 条误报里有几条看着像真外键（`cr_order_number → cs_order_number`、`sr_ticket_number → ss_ticket_number`、`wp_customer_sk → c_customer_sk`），官方约束里没有。我没有动答案去认它们——这正好是 `route=sign` 的意思：这条要人看，不是脚本说了算。

所以每条候选落库时都带 `route`：在白名单里、位置又空，才是 `exempt`，其余一律 `sign`。这不是保守，是查表查出来的结论。

## 本体长什么样

**十二个类节点 + 一类边 + 两条公理。**

```
3E 业务里有什么
  ├─ BO  business_object      业务对象：门店、商品、订单、活动
  ├─ SO  semantic_dataset     ← 层名，不是类；下面两个类合起来才是 SO
  │    ├─ metric              指标
  │    └─ dimension           维度
  └─ TO  table_object         表对象（含列）
7S 业务怎么做
  └─ term / perspective / event / decision / method / action / playbook
Link 一条边：src → predicate → dst
```

**十二个类、三个层，别混着数。** 阿里说「十一类」是按 SO 一个类算；我们把它展开成 `metric` + `dimension` 两个类，差的就是这一个。

**公理不是第 13 类节点，也不挂在节点上——它挂在谓词上。** 公理回答的是「这个谓词什么时候允许建边」，所以它的定义位置在词表（八个词，每个两三条，十几条封顶），生效位置在每一条边上。挂在边上的话，它会随边数一起长到上万条（真实图谱里关系数约是节点数的 2.9 倍），那就不叫公理了，叫备注。

同一句话里还有两类东西**不叫公理**：对象不变量（分子分母必须同口径，约束的是对象内部，每次算数都要判）和框架公理（自然语言只穿过边界一次、输出必须枚举完、未知输入等于错误、一个方法只能有一个权威——这四条不进库，进了库就会冒出「有人改了框架公理」这种版本事件）。

**v0.3 只把位置定下来，公理表和 `gate` 还没写。** 前置条件是先把谓词拆细。

三个刻意的选择：

- **十二个类是数据，不是代码。** 加一类不改表结构，只插一行 `type`。
- **谓词必须来自受控词表，而且两端类型写死。** 自由字符串等于没约束；两端不限等于没有词。v0.2 的 `derived_from` 一个词装了三种关系（指标→表、指标→指标、事件→指标），最后一种连词表自己的定义都不符合，但因为「两端不限」一路走通。现在拆成 `computed_from` / `inputs` / `evidenced_by`，导入时按 `src_types` / `dst_types` 校验。
- **`metric` 必填 `kind`（`basic` / `derived`），`derived` 必填 `inputs`。** 导入时就校验：引用不存在拒、成环拒、`basic` 带 `inputs` 也拒。

## 存储：五张表

```sql
predicate (name, layer, src_types, dst_types, note)   -- 词表；两端类型是定义的一部分
version   (id, domain, published_at, note, content_hash, is_current)
object    (version_id, id, type, name)
field     (version_id, object_id, name, value_json,
           confirmed_by, confirmed_at, source_json)
edge      (version_id, src, predicate, dst)     -- 三要素一起进主键
candidate (id, kind, route, payload_json, evidence_json, state)   -- route 写库时算
```

三条设计决定：

**每次签发是一份完整快照，不是增量。** 万级数据，全量复制比 bitemporal 便宜，而且「跑两遍输出一样」天然成立。

**候选是 json，本体是行。** 候选不需要被确认，不必结构化；被采纳那一刻才拆成行。

**`ontology.json` 是导出物，不是源头。** 下游继续读 json，不用装这个库——每个仓库单独可用这条不破。

## 跟谁配合

| 仓库 | 关系 |
|---|---|
| [`mql-compiler`](../mql-compiler) | 读导出的本体，把 MQL 编成 SQL |
| [`ontology-slicer`](../ontology-slicer) | 读导出的本体，按问题切子图 |
| [`mql-author`](../mql-author) | 读导出的本体，做指标召回 |
| [`stat-probe`](../stat-probe) | 无关。归因用的，不在本体的链路上 |

**版本不一致是已知状态**：本仓库用本体 v2（十二类），上面三个仓库还停在 v1.1（不认识 7S）。这是显式记录的已知不一致，不靠兼容层掩盖——兼容层会掩盖两边看到的本体不一样这件事。

## 状态

- v0.1.0：存储层 + 版本 + 候选隔离。`probe` / `mine` / `gate` 未实现
- v0.1.1：补 SO。`metric` / `dimension` 分 `basic` / `derived`，`derived` 的 `inputs` 做引用完整性与成环校验；fixtures 补了三个派生指标（客单价 / 退款率 / 同店 GMV），正好覆盖三类口径分歧（除数 / 分母 / 范围）
- v0.2.0：`probe` 落地。DDL 解析、列画像（只读打开）、三条判据、`route` 分免批与待签；TPC-DS 实测三组数字写在上面；`candidate` 表加 `route` 列（老库自动补列）
- v0.3.0：谓词拆细。`derived_from` 一拆三（`computed_from` / `inputs` / `evidenced_by`），`describes` 并进 `belongs_to`，`same_as` 收窄到跨体系；`predicate` 表加 `layer` / `src_types` / `dst_types`（老库自动补列），`commit` 按两端类型拒边、拒悬空边；`init` 清理无引用的废词并把有引用的旧词报出来
- v0.4.0：免批判据从「声明式 / 推断式」升级成三问（位置空 + 影响局部 + 声明式），三问在写库时判。新增 `route.py`（判定纯函数 + 落点 + 占用查询）；`probe` 的 `Candidate` 不再带 `route`，改为 `declared` + `impact`；`add_candidates` 返回实际落的 route 列表；修 `--help` 里 `%` 没转义导致 argparse 崩的问题
- v0.4.1：TPC-DS 的两份 DDL 移出仓库（版权在 TPC，本仓库是 MIT），加 `bench/fetch_tpcds.py` 从本地 kit 补。当时测试变成 `67 passed, 3 skipped`，补上文件后是 70
- v0.5.0：免批判据从**三问**改成**白名单**。`impact` 和 `declared` 删掉，换成 `evidence_form` 枚举（`ddl_table` / `ddl_pk` / `ddl_fk` / `data_unique` / `data_containment`）；`EXEMPT_WHITELIST` 三条，每条带一句依据；不在表里一律签；未知形态直接报错，不静默判 sign。`candidate` 表加 `evidence_form` 列（老库自动补列）；CLI 的 `candidate` 子命令改为必填 `--evidence-form`；`target_of` 不再给谓词缺省值。测试 69
- 形状与判据全部写在 [`SPEC.md`](SPEC.md)，改代码前先改那里
- 验收不定准确率：**可审**（没做过的人一小时内判完变更清单）+ **可重算**（`propose` 两遍一样、`commit` 重试只出一个版本）+ **口径不漏**（遮掉名字只看字段，能看出它和 `gmv` 不是一回事）

## License

MIT © Aiops-seal
