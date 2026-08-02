# Critic v1.0：C0514 真实审查验证

日期：2026-08-02  
输入：Prediction v1.5.0 C0514 最终 handoff  
候选：C0514，`TGTGETLEEFQE`

## 1. 结论

Critic v1.0 已在服务器真实 Prediction 产物上跑通：

- record 路径和 SHA-256 校验通过；
- Prediction run/version/candidate/status 身份校验通过；
- verdict：`iterate`；
- 没有误报 `prediction_evidence_incomplete` 或 `invalid_prediction_artifact`；
- 相同输入重复运行生成相同 report ID/SHA-256；
- State iteration history 和 evidence log 各只登记一次 Critic 事件；
- 正式 State/CSV 未修改。

## 2. C0514 issue

```text
l2_interface_confidence_low
l3_interface_physics_low
threshold_calibration_pending
cohort_too_small
```

解释：

- C0514 的 Boltz、AlphaFold2、PRODIGY、Rosetta 和 post-relax 证据已经完整；
- L2/L3 是完整证据下的真实数值失败，应该反馈 Design 改进界面；
- 暂定阈值仍应单独标记，不能通过自动放宽阈值解决；
- 当前只有一个真实完整候选，不能据此判断 route 分布或候选池多样性。

Critic 没有把 ipTM 当成 L2 主判据。L2 evidence 使用 ipSAE：

- MDM2：0.2987596854；
- MDMX：0.2867012253。

## 3. Planner 建议

| 优先级 | action | owner | 说明 |
|---|---|---|---|
| P1 | `iterate_interface_design` | Design | 针对 L2 ipSAE 失败追加界面设计 |
| P1 | `iterate_interface_physics` | Design | 针对 L3 dG/SC 失败改善界面物理 |
| P2 | `calibrate_thresholds` | Research | 需要人工批准；当前可继续 deferred |
| P2 | `generate_review_cohort` | Design | 增加真实完整候选后再做分布判断 |

报告明确包含 `reuse_complete_prediction_evidence`，Planner 不应为 C0514 重跑已经
完整的 Boltz/Rosetta/post-relax 来“补证据”。

## 4. 幂等与产物

```text
report ID:
critic_af6225e262d5

report path:
/root/damodel-tmp/novapeptide/prediction_v150_c0514_final_20260802/runs/
prediction_v150_c0514_final_20260802/critic/critic_af6225e262d5/critic_report.json

report SHA-256:
ed9b475a04e7d84456ac2d8637c82deded87d04dbb6bf87fb0ff335bcf8bd55e
```

连续运行两次后：

- Critic iteration history：1；
- `critic_review` evidence event：1；
- 隔离 State phase：`critic`。

## 5. 正式数据保护

```text
data/state.json:
10c6fdf79b030e9693664cb53e1512522aaad6e1546d37664a9e1ad0825a457f

data/candidate_index.csv:
4e4b0a0e8be7a5e959262a3cc76db5e28f983076a7c3ce462b605eeab2e89c84
```

Critic 只修改 C0514 的隔离 State/CSV 副本，并将报告写入隔离 Prediction run。
