# 1.6.0 合并 validation 草案：100 条

原 [1.5.2 的 60 条](../draft_1_5_2/README.md)加上 [40 条中高难案例](../challenge_1_6/README.md)。由 `uv run python scripts/build_challenge_dataset.py` 生成；原案例的标签、申请 ID、证据要求、权限及预算均保留，合并副本只更新 `dataset_version` 并记录 `source_dataset_version`。原目录和冻结 test 不变。

本目录提供合并的申请、轨迹、检索样本、附件切片、附件证据和文件清单；PDF 不重复存放，路径仍指向各自来源目录。新案例的结构化快照使用 `challenge_1_6/structured_snapshots.json`，入库方法见该目录 README。

运行 100 条时，将工作流评测命令的 `--dataset-dir`、`--attachment-chunks`、`--attachment-evidence` 改成本目录对应路径，并使用新报告文件名。仍须 `--active-catalog`，使新旧制度同时处于单公司授权目录中。

当前是**未全量实测的合成 validation 草案**，不代表原有通过率可以直接延用到 100 条。新 40 条的中/高难分组与原 60 条应分别统计；不得把不同版本、模型或检索配置的最佳结果拼成一份对照。

新增 40 条已完成 [独立对照运行](../../reports/single_vs_multi_dense_challenge_1_6_deepseek_20261002.md)，本轮没有重新运行原 60 条，也没有声称完成一次统一的 100 条对照。标签仍为待独立审阅的草案。
