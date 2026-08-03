"""
Design Agent v5.2.0 — 于嘉乐
职责：RFpeptides 生成环肽骨架 → LigandMPNN 序列设计 → AfCycDesign refold 验证
入口：design_rfpeptides(target_spec, design_config) → list[dict]
      design_motif_guided(target_spec, design_config) → list[dict]
      design_atsp_derived(target_spec, design_config) → list[dict]
      threshold_filter(candidates, thresholds) → list[dict]
      pareto_front(candidates) → list[dict]
依赖：from data_layer import EvidenceLogger, CandidateIndex, State, file_hash
工具：RFdiffusion (rfdiff_env) / LigandMPNN (rfdiff_env) / AfCycDesign (cycpep)

Agent 职责边界：
  Design 阶段只做基础验证（能折叠 + 环闭合）。
  pLDDT > 0.8 的最终过滤由 Prediction Agent (Phase 3 L1) 负责。
"""

import math, os, sys, json, time, subprocess, tempfile, threading, hashlib, copy, shutil, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from data_layer import EvidenceLogger, CandidateIndex, State, file_hash
from project_config import (
    load_project_config,
    required_target_ids,
    target_slug,
    target_value,
    threshold_for_target,
)
from structure_resolution import assert_target_structure_ready
from target_bootstrap import assert_project_approved


# ============================================================
# 环境路径
# ============================================================
#
# 警告：以下所有路径常量在 import 时解析。必须在 import design 之前
# 设置 CYCPEP_CONDA / RFDIFF_DIR / CYCPEP_DESIGN_ROOT 等环境变量；
# import 之后再修改这些变量不会生效。测试代码请参阅 test_design.py
# 的 stub 注入模式。

ACTIVE_PROJECT_CONFIG = load_project_config()

# 新服务器路径可全部通过环境变量覆盖；默认值对应 damodel 部署。
# os.environ.get(…) or default 确保空字符串不会静默损坏路径（P0-2）。
CYCPEP_CONDA = os.environ.get("CYCPEP_CONDA") or "/root/damodel-tmp/envs/cycpep-prediction"
CYCPEP_PYTHON = os.environ.get("CYCPEP_PYTHON") or f"{CYCPEP_CONDA}/bin/python"
RFDIFF_CONDA = os.environ.get("RFDIFF_CONDA") or "/root/damodel-tmp/envs/rfdiffusion-design"
RFDIFF_PYTHON = os.environ.get("RFDIFF_PYTHON") or f"{RFDIFF_CONDA}/bin/python"
RFDIFF_DIR = os.environ.get("RFDIFF_DIR") or "/root/workspace/NovaPeptide/tools/RFdiffusion"
LIGANDMPNN_DIR = os.environ.get("LIGANDMPNN_DIR") or "/root/workspace/NovaPeptide/tools/LigandMPNN"
COLABDESIGN_DIR = os.environ.get("COLABDESIGN_DIR") or "/root/workspace/NovaPeptide/tools/ColabDesign"
COLABDESIGN_PARAMS = os.environ.get("COLABDESIGN_PARAMS") or f"{COLABDESIGN_DIR}/params"
COLABDESIGN_COMMIT = "094e2cb3603dee7d99846e0977736bd943c830c2"
SE3_ROOT = os.environ.get("SE3_ROOT") or f"{RFDIFF_DIR}/env/SE3Transformer"
CUDA_DATA_DIR = os.environ.get("CUDA_DATA_DIR") or f"{CYCPEP_CONDA}/lib/python3.10/site-packages/nvidia/cuda_nvcc"
DAMODEL_DATA_ROOT = Path("/root/damodel-tmp/novapeptide")


def _resolve_output_dir(environ=None, damodel_data_root=None):
    """Resolve a writable design root without assuming /root is accessible."""
    env = os.environ if environ is None else environ
    explicit_root = env.get("CYCPEP_DESIGN_ROOT")
    if explicit_root:
        return Path(explicit_root)

    np_data_root = env.get("NP_DATA")
    if np_data_root:
        return Path(np_data_root) / "designs"

    damodel_root = DAMODEL_DATA_ROOT if damodel_data_root is None else damodel_data_root
    try:
        if damodel_root.is_dir():
            return damodel_root / "designs"
    except OSError:
        # GitHub runners and other non-root users cannot stat paths below
        # /root.  Fall through to next candidate without logging — this
        # function runs at import time and must not produce side effects (P2).
        pass

    runner_temp = env.get("RUNNER_TEMP")
    if runner_temp:
        return Path(runner_temp) / "novapeptide" / "designs"
    return ROOT / "data" / "designs"


DEFAULT_OUTPUT_DIR = _resolve_output_dir()
OUTPUT_DIR = str(DEFAULT_OUTPUT_DIR)
_raw_ts = os.environ.get("RFDIFF_TIMESTEPS") or "50"
try:
    RFDIFF_TIMESTEPS = max(1, int(_raw_ts))
except (ValueError, TypeError):
    RFDIFF_TIMESTEPS = 50
    # Defer log until _run_rfdiff first consumes the value (P1: no
    # EvidenceLogger side-effects at import time).
    _RFDIFF_TIMESTEPS_INVALID = os.environ.get("RFDIFF_TIMESTEPS")
else:
    _RFDIFF_TIMESTEPS_INVALID = None
LIGANDMPNN_MODEL_TYPE = os.environ.get("LIGANDMPNN_MODEL_TYPE") or "protein_mpnn"
LIGANDMPNN_CHECKPOINT = os.environ.get("LIGANDMPNN_CHECKPOINT") or f"{LIGANDMPNN_DIR}/model_params/proteinmpnn_v_48_020.pt"
DESIGN_PIPELINE_VERSION = "5.2.0"

# Module-level state for _verify_colabdesign_runtime() (P3-3).
# Only cache *success* — a transient failure (GPU OOM, env hiccup) must
# not permanently disable the check for the lifetime of the process (P1).
# Cached signature binds to the concrete ColabDesign environment so that
# switching CYCPEP_PYTHON / COLABDESIGN_DIR / COLABDESIGN_PARAMS
# mid-process triggers a re-verification (P1 reviewer feedback).
_VERIFIED_RUNTIME_SIGNATURE = None
_SKIP_EVIDENCE_LOGGED = False


def _verify_colabdesign_runtime():
    """Functional smoke test: verify ColabDesign can load, forward, and produce
    non-zero residue_index offsets (P2: renamed from _check_colabdesign_loads).

    Runs in a subprocess (ColabDesign needs ``CYCPEP_PYTHON``, not the main
    process interpreter).  On success the module-level signature is set so
    every subsequent refold targeting the *same* environment skips the
    functional gate (P1-3).

    Set ``CYCPEP_SKIP_COLABDESIGN_VERIFY=1`` to bypass the check entirely
    (orchestrator-managed GPU allocation; P1 reviewer feedback).
    """
    global _VERIFIED_RUNTIME_SIGNATURE, _SKIP_EVIDENCE_LOGGED
    if os.environ.get("CYCPEP_SKIP_COLABDESIGN_VERIFY") == "1":
        if not _SKIP_EVIDENCE_LOGGED:
            EvidenceLogger.log("design", "colabdesign_verify_skipped",
                {"reason": "CYCPEP_SKIP_COLABDESIGN_VERIFY=1 — "
                 "GPU allocation managed by orchestrator; "
                 "no pre-flight ColabDesign check will run"})
            _SKIP_EVIDENCE_LOGGED = True
        return
    sig = (CYCPEP_PYTHON, COLABDESIGN_DIR, COLABDESIGN_PARAMS)
    if _VERIFIED_RUNTIME_SIGNATURE == sig:
        return
    # Double-checked locking: only one thread may run the GPU subprocess.
    # NOTE: this only serialises within the *same* Python process.  When the
    # orchestrator launches multiple worker processes, use
    # CYCPEP_SKIP_COLABDESIGN_VERIFY=1 with a single pre-flight check instead.
    with _LOCK:
        if _VERIFIED_RUNTIME_SIGNATURE == sig:
            return
        script = f"""
import sys, numpy as np
sys.path.insert(0, {COLABDESIGN_DIR!r})
from colabdesign import mk_af_model, clear_mem
model = None
model = mk_af_model(protocol='hallucination', data_dir={COLABDESIGN_PARAMS!r})
model.prep_inputs(length=8)
model.restart(seed=0, seq='AAAAAAAA')
try:
    # Minimal forward pass — proves AF model can actually compute, not just
    # import and initialise (P1 smoke-test enhancement).
    aux = model.predict(
        seq='AAAAAAAA', seed=0, models=[0], num_models=1, num_recycles=1,
        sample_models=False, dropout=False, hard=True, soft=False,
        verbose=False, return_aux=True,
    )
    plddt = np.array(aux['plddt'])
    if not np.isfinite(plddt).all():
        raise RuntimeError(
            f'ColabDesign pLDDT contains non-finite values: '
            f'nan={{np.isnan(plddt).sum()}} inf={{np.isinf(plddt).sum()}}'
        )
    _ = float(np.mean(plddt))
    idx = np.array(model._inputs['residue_index'])
    off = np.array(idx[:, None] - idx[None, :])
    if not np.any(off):
        raise RuntimeError('ColabDesign residue_index offset matrix is all-zero')
finally:
    if model is not None:
        del model
    clear_mem()
print('COLABDESIGN_OFFSET_OK')
"""
        spath = os.path.join(
            tempfile.gettempdir(),
            f"_cd_offset_check_{os.getpid()}.py",
        )
        with open(spath, "w") as f:
            f.write(script)
        try:
            r = subprocess.run([CYCPEP_PYTHON, spath], capture_output=True, text=True,
                timeout=120,
                env={**os.environ,
                     "XLA_FLAGS": f"--xla_gpu_cuda_data_dir={CUDA_DATA_DIR}",
                     "XLA_PYTHON_CLIENT_PREALLOCATE": "false",
                     "XLA_PYTHON_CLIENT_MEM_FRACTION": "0.20"})
            if r.returncode != 0:
                EvidenceLogger.error("design", "colabdesign_offset_check_failed",
                    f"exit={r.returncode} stderr={getattr(r, 'stderr', '')[-300:]}")
                return
            if "COLABDESIGN_OFFSET_OK" not in (getattr(r, 'stdout', '') or ""):
                EvidenceLogger.error("design", "colabdesign_offset_check_failed",
                    "functional test did not emit success marker")
                return
            _VERIFIED_RUNTIME_SIGNATURE = sig
        except (subprocess.SubprocessError, OSError) as exc:
            EvidenceLogger.error("design", "colabdesign_offset_check_error", str(exc))
        finally:
            try:
                os.unlink(spath)
            except OSError:
                pass

# Geometry gates are deliberately labelled as compatibility checks.  A model
# whose terminal atoms are close enough for a covalent bond is suitable for
# downstream relaxation/validation; coordinates alone do not prove that the
# bond has been chemically formed.
CLOSURE_GEOMETRY = {
    "head-to-tail_amide": {
        "atom_1": "last:C",
        "atom_2": "first:N",
        # The wwPDB validation range for a peptide C-N bond is 1.30-1.45 Å.
        # Design uses a wider pre-relax screen and records ideal-range status.
        "screen_range_angstrom": (1.15, 2.00),
        "ideal_range_angstrom": (1.30, 1.45),
    },
    "Cys-Cys_disulfide": {
        "atom_1": "first:SG",
        "atom_2": "last:SG",
        # Typical protein disulfides are close to 2.03 Å.  The wider screen
        # tolerates an unrelaxed predictor output without accepting CA proxies.
        "screen_range_angstrom": (1.80, 2.30),
        "ideal_range_angstrom": (1.90, 2.15),
    },
}


# ============================================================
# 设计常量（Research 产出可覆盖）
# ============================================================

# 所有设计常量从 Research State 读取（_load_target_spec）。
_LOCK = threading.Lock()
CYCLIZATION_PAIRS = [("C", "C"), ("", "")]
LINKER_MATRIX = ["GGGGS", "GGGS", "GGS", "GS", ""]
SCAFFOLD_MUTABLE_AA = "ACDEFGHIKLMNPQRSTVWY"

# 便宜预筛参数
#
# Route A 已改为全局两阶段收集→排序→取 top K 条（Pass1: 收集所有 backbone
# 序列，Pass2: 全局 cheap filter）以避免 backbone 顺序偏差。Route B 同理。
# CHEAP_FILTER_MAX_KEEP 控制每个 backbone 内部预筛保留的序列数上限，不再约束
# 最终候选数。
try:
    CHEAP_FILTER_MAX_KEEP = max(1, int(
        os.environ.get("CHEAP_FILTER_MAX_KEEP")
        or os.environ.get("CHEAP_FILTER_TOP_K")
        or "4"))
except (ValueError, TypeError):
    CHEAP_FILTER_MAX_KEEP = 4
HYDROPHOBIC = set("AILMFWV")
POS_CHARGED = set("KR")
NEG_CHARGED = set("DE")


def _require_mdm_reference_route(route_name):
    target_ids = set(required_target_ids(ACTIVE_PROJECT_CONFIG))
    if target_ids != {"MDM2", "MDMX"}:
        raise RuntimeError(
            f"{route_name} contains MDM-specific motif knowledge and is disabled for "
            f"project {ACTIVE_PROJECT_CONFIG['project_id']}; provide project-specific motifs instead"
        )


def _load_existing_sequences():
    """Return sequences already registered in CandidateIndex for cross-batch dedup.

    By default, an unreadable index raises ``RuntimeError`` — RFdiffusion +
    LigandMPNN + AfCycDesign refold is too expensive to risk duplicates (P1).
    Set ``CYCPEP_ALLOW_WITHOUT_DEDUP=1`` to downgrade to a warning and
    proceed without dedup (testing / one-off exploration).
    """
    try:
        rows = CandidateIndex.load()
    except (OSError, UnicodeError, ValueError) as exc:
        if os.environ.get("CYCPEP_ALLOW_WITHOUT_DEDUP") == "1":
            EvidenceLogger.error("design", "candidate_index_unavailable",
                str(exc),
                recovery="cross-batch dedup disabled; candidates may duplicate")
            return None
        raise RuntimeError(
            "CandidateIndex is unavailable — cross-batch dedup cannot be "
            "guaranteed, which risks duplicating expensive RFdiffusion / "
            "LigandMPNN / AfCycDesign work.  Set CYCPEP_ALLOW_WITHOUT_DEDUP=1 "
            "to proceed without dedup, or fix the index and retry."
        ) from exc
    return {
        str(row.get("sequence") or "").upper()
        for row in rows
        if isinstance(row, dict) and row.get("sequence")
    }


def _cheap_filter_sequences(seqs, seen_seqs=None, top_k=CHEAP_FILTER_MAX_KEEP):
    """
    便宜预筛（无 GPU）：合成可行性 + 基本理化性质。
    返回 top_k 条最优序列，格式 [(seq, score), ...]。
    选中的序列会回写到 ``seen_seqs`` 供跨 backbone 去重。
    """
    if seen_seqs is None:
        seen_seqs = set()
    seqs = list(seqs or [])
    scored = []
    violation_counts = {}
    for seq in seqs:
        if not isinstance(seq, str) or not seq:
            continue
        if seq.upper() in seen_seqs:
            continue
        violations = _synthesizability_violations(seq)
        if violations:
            for v_reason in violations:
                # Normalize dynamic keys like "stray_cys_at_[1,3]" to "stray_cys"
                base_reason = v_reason.split("_at_")[0]
                violation_counts[base_reason] = violation_counts.get(base_reason, 0) + 1
            continue  # 硬淘汰
        score = _sequence_quality_score(seq)
        scored.append((seq.upper(), score))
    scored.sort(key=lambda x: (x[1], x[0]), reverse=True)
    result = scored[:top_k]
    if not result and seqs:
        EvidenceLogger.log("design", "cheap_filter_empty", {
            "total": len(seqs),
            "already_seen": sum(1 for s in seqs if isinstance(s, str) and s.upper() in seen_seqs),
            "top_k": top_k,
            "violation_distribution": violation_counts,
        })
    for seq, _score in result:
        seen_seqs.add(seq)
    return result


def _synthesizability_violations(seq):
    """
    检查 Kickoff 定义的可合成性规则。返回违规列表，空列表 = 通过。
    - 聚集：连续 >4 个疏水氨基酸
    - 游离 Cys：不在 N/C 端的 Cys
    - 氧化：Met / Trp（软警告，不硬淘汰）
    - 脱酰胺：Asn-Gly
    - Asp-Pro 断裂
    """
    if not seq:
        return ["empty_sequence"]
    seq = seq.upper()  # P1-2: normalise case for all downstream checks
    v = []
    # 连续疏水（线性扫描）
    run = 0
    for aa in seq:
        if aa in HYDROPHOBIC:
            run += 1
        else:
            run = 0
        if run > 4:
            v.append("aggregation")
            break
    # 环化交界面：N端和C端环化后相邻，检查跨边界的连续疏水
    if "aggregation" not in v:
        tail_run = 0
        for aa in reversed(seq):
            if aa in HYDROPHOBIC:
                tail_run += 1
            else:
                break
        head_run = 0
        for aa in seq:
            if aa in HYDROPHOBIC:
                head_run += 1
            else:
                break
        if tail_run + head_run > 4:
            v.append("aggregation")
    # 游离 Cys（不在首尾）— 收集全部位置
    stray_positions = [
        i for i, aa in enumerate(seq)
        if aa == "C" and i not in (0, len(seq) - 1)
    ]
    if stray_positions:
        v.append(f"stray_cys_at_{stray_positions}")
    # Asn-Gly 脱酰胺
    for i in range(len(seq) - 1):
        if seq[i:i+2] == "NG":
            v.append("deamidation_NG")
            break
    # Asp-Pro 断裂（环肽中 N→C 也检查）
    for i in range(len(seq) - 1):
        if seq[i:i+2] == "DP":
            v.append("dp_cleavage")
            break
    # 环化连接 bond：C-term(seq[-1]) → N-term(seq[0])
    junction = seq[-1] + seq[0]
    if junction == "NG":
        v.append("deamidation_NG_cyclic")
    if junction == "DP":
        v.append("dp_cleavage_cyclic")
    return v


def _sequence_quality_score(seq):
    """
    序列质量评分（越高越好），基于：
    - 疏水/亲水平衡（0.3-0.7 区间最优）
    - 净电荷适中（-1 到 +1 最优）
    - 氨基酸多样性
    """
    L = len(seq)
    seq = seq.upper()  # normalise case for all downstream checks
    h = sum(1 for aa in seq if aa in HYDROPHOBIC) / max(L, 1)
    pos = sum(1 for aa in seq if aa in POS_CHARGED)
    neg = sum(1 for aa in seq if aa in NEG_CHARGED)
    net = (pos - neg) / max(L, 1)
    diversity = len(set(seq)) / max(L, 1)

    # 疏水平衡分：离 0.5 越近越好
    h_score = 1.0 - abs(h - 0.5) * 2
    # 电荷分：离 0 越近越好
    c_score = 1.0 - min(abs(net) * 5, 1.0)
    # 多样性分：越高越好（但 >0.4 就很好）
    d_score = min(diversity / 0.5, 1.0)

    total = h_score * 0.4 + c_score * 0.3 + d_score * 0.3
    # Met/Trp 氧化风险：每残基扣 0.15，上限 0.30（软惩罚，不硬淘汰）
    mw_count = sum(1 for aa in seq if aa in "MW")
    total -= min(mw_count, 2) * 0.15
    return max(total, 0.0)


# ============================================================
# Route A: RFpeptides 自由生成
# ============================================================

def design_rfpeptides(target_spec=None, design_config=None):
    """RFpeptides → LigandMPNN → AfCycDesign refold"""
    config = _merge_config(target_spec, design_config)
    route_name = f"route_A_{target_slug(config['target_id'])}"
    batch_id = f"batch_rfpep_{config['target_name']}_s{config['seed']}"
    batch_dir = os.path.join(OUTPUT_DIR, "route_A", batch_id)
    os.makedirs(batch_dir, exist_ok=True)

    with open(os.path.join(batch_dir, "design_config.json"), "w") as f:
        json.dump(config, f, indent=2, default=str)

    candidates = []
    total_gen, total_valid = 0, 0
    t_batch = time.time()
    _hotspots = _parse_hotspot_residues(config.get("hotspots", ""))
    target_range = _pdb_residue_range(config["target_pdb"], config["chain"],
                                      hotspot_residues=_hotspots)
    seen_seqs = _load_existing_sequences() or set()  # cross-batch dedup (None → set())

    # Pass 1: RFdiffusion + LigandMPNN → collect all raw sequences across
    # every backbone so global scoring is not biased by backbone order (P1-2).
    backbone_entries = []  # (bb_path, binder_chain, raw_seqs)

    for L in config["lengths"]:
        n_designs = max(1, config["n"] // len(config["lengths"]))
        backbone_dir = os.path.join(batch_dir, f"backbones_len{L}")
        os.makedirs(backbone_dir, exist_ok=True)
        rfdiff_ok = _run_rfdiff(
            target_pdb=config["target_pdb"], binder_len=L,
            n_designs=n_designs, output_prefix=f"{backbone_dir}/bb",
            contig=_binder_first_contig(
                config["chain"], target_range[0], target_range[1], L
            ),
            seed=config["seed"],
            hotspots=config.get("hotspots"),
            chain=config["chain"])
        if not rfdiff_ok:
            print(f"[Route A] RFdiff 失败 len={L}，跳过")
            continue

        def _bb_sort_key(p):
            try:
                return int(p.stem.split('_')[-1])
            except (ValueError, IndexError):
                return 0  # P1-1: non-standard filename, sort to front
        bb_files = sorted(Path(backbone_dir).glob("bb_*.pdb"), key=_bb_sort_key)
        print(f"[Route A] RFdiff 完成, 找到 {len(bb_files)} 个骨架PDB")
        for bb_path in bb_files[:n_designs]:
            total_gen += 1
            try:
                binder_chain = _infer_binder_chain(str(bb_path), L, receptor_chain=config["chain"])
            except (OSError, UnicodeError, ValueError) as exc:
                EvidenceLogger.error(
                    "design", "rfdiff_binder_chain_invalid",
                    f"{bb_path}: {exc}", recovery="skip ambiguous backbone",
                )
                continue
            mpnn_dir = os.path.join(batch_dir, f"mpnn_{bb_path.stem}")
            os.makedirs(mpnn_dir, exist_ok=True)
            mpnn_seed = (config["seed"] + total_gen) % 2**31
            seqs = _run_ligandmpnn(
                str(bb_path), mpnn_dir, n_seq=8, binder_chain=binder_chain,
                seed=mpnn_seed,
            )
            if not seqs:
                print(f"[Route A] LigandMPNN 返回 0 条序列: {bb_path.name}")
                continue
            backbone_entries.append((bb_path, binder_chain, seqs))

    # Pass 2: global cheap filter — score ALL sequences together so early
    # backbones cannot starve later ones (P1-2).
    all_raw_seqs = []
    bb_lookup = {}  # seq.upper() → [(bb_path, binder_chain), ...]  (P2-1)
    for bb_path, binder_chain, seqs in backbone_entries:
        for s in seqs:
            key = s.upper() if isinstance(s, str) else ""
            if key:
                bb_lookup.setdefault(key, []).append((bb_path, binder_chain))
        all_raw_seqs.extend(seqs)

    filtered = _cheap_filter_sequences(all_raw_seqs, seen_seqs=seen_seqs, top_k=config["n"])
    print(f"[Route A] global cheap filter: {len(all_raw_seqs)}→{len(filtered)} sequences")

    for seq, quality_score in filtered:
        bb_list = bb_lookup.get(seq)
        if not bb_list:
            continue
        # Use the first backbone that produced this sequence; if multiple
        # backbones produced the same sequence, note it in the manifest.
        bb_path, binder_chain = bb_list[0]
        bb_alternatives = [str(bp) for bp, _ in bb_list[1:]] if len(bb_list) > 1 else []
        cid = _next_candidate_id()
        refold_dir = os.path.join(batch_dir, "candidates", cid)
        os.makedirs(refold_dir, exist_ok=True)
        refold_pdb = os.path.join(refold_dir, "refold.pdb")
        plddt = _run_refold(seq, refold_pdb)
        cyclization = _infer_cyclization_type(seq)
        try:
            rc = (
                _ring_closure_check(refold_pdb, cyclization, sequence=seq)
                if os.path.exists(refold_pdb)
                else {"pass": False, "reason": "refold_pdb_missing"}
            )
        except (ValueError, OSError) as exc:
            rc = {"pass": False, "reason": f"closure_check_error: {exc}"}

        if plddt is not None and rc.get("pass"):
            total_valid += 1
            try:
                manifest = _write_manifest(
                    cid, seq, route_name, batch_id, refold_pdb, config,
                    backbone_pdb=str(bb_path), cyclization=cyclization,
                    ring_closure=rc, bb_alternatives=bb_alternatives,
                )
            except ValueError as exc:
                EvidenceLogger.error("design", "manifest_cyclization_mismatch",
                    str(exc), recovery="skip mismatched candidate (P1-7)")
                continue
            candidate = _candidate_from_manifest(
                manifest, plddt,
                notes={"quality_score": round(quality_score, 3)},
            )
            CandidateIndex.add(candidate)
            EvidenceLogger.log("design", "candidate_registered",
                {"candidate": candidate},
                targets=[config["target_id"]], phase="design")
            candidates.append(candidate)
        else:
            print(f"[Route A] refold失败: {cid} pLDDT={plddt} ring_closed={rc.get('pass')}")

    EvidenceLogger.design_batch(route=route_name, n_generated=total_gen,
        n_valid=total_valid, tool_name="rfpeptides_pipeline",
        tool_version=DESIGN_PIPELINE_VERSION,
        duration_sec=round(time.time()-t_batch, 1))
    return candidates


# ============================================================
# Route B: motif 引导生成
# ============================================================

def design_motif_guided(target_spec=None, design_config=None):
    """RFpeptides motif 引导 + LigandMPNN L26 偏置 + refold"""
    config = _merge_config(target_spec, design_config)
    _require_mdm_reference_route("route_B_motif")
    route_name = f"route_B_motif_{target_slug(config['target_id'])}"
    batch_id = f"batch_motif_s{config['seed']}"
    spec = _load_target_spec()
    binders = spec.get("known_dual_binders", [])
    if not binders:
        EvidenceLogger.error("design", "no_binders",
            "known_dual_binders empty in state.json — Research 尚未产出或格式错误",
            recovery="先跑 Research Agent 产出设计规则再跑 Route B")
        return []

    batch_dir = os.path.join(OUTPUT_DIR, "route_B", batch_id)
    os.makedirs(batch_dir, exist_ok=True)
    with open(os.path.join(batch_dir, "design_config.json"), "w") as f:
        json.dump(config, f, indent=2, default=str)

    templates = [(b.get("sequence") or b.get("seq", ""), b.get("name","tmpl"))
                 for b in binders if b.get("sequence") or b.get("seq")]

    candidates = []
    total_gen, total_valid = 0, 0
    t_batch = time.time()
    n_per = max(1, config.get("n", 100) // max(1, len(templates)))
    _hotspots = _parse_hotspot_residues(config.get("hotspots", ""))
    target_range = _pdb_residue_range(config["target_pdb"], config["chain"],
                                      hotspot_residues=_hotspots)
    seen_seqs = _load_existing_sequences() or set()  # cross-batch dedup (None → set())

    # Pass 1: RFdiffusion + LigandMPNN → collect all raw sequences across
    # every template and backbone (P2-3: same two-pass pattern as Route A).
    backbone_entries = []  # (bb_path, binder_chain, raw_seqs)

    for tmpl_seq, tmpl_name in templates:
        if len(tmpl_seq) < 8 or len(tmpl_seq) > 20:
            continue
        L = len(tmpl_seq)
        tmpl_hotspots = _hotspot_positions(tmpl_seq)
        safe_name = "".join(c if c.isascii() and (c.isalnum() or c=="_") else "_" for c in tmpl_name)
        backbone_dir = os.path.join(batch_dir, f"backbones_{safe_name}")
        os.makedirs(backbone_dir, exist_ok=True)
        rfdiff_ok = _run_rfdiff(target_pdb=config["target_pdb"], binder_len=L,
            n_designs=n_per, output_prefix=f"{backbone_dir}/bb",
            contig=_binder_first_contig(
                config["chain"], target_range[0], target_range[1], L
            ),
            seed=config["seed"],
            hotspots=config.get("hotspots"),
            chain=config["chain"])
        if not rfdiff_ok:
            print(f"[Route B] RFdiff 失败 {tmpl_name}，跳过")
            continue

        def _bb_sort_key(p):
            try:
                return int(p.stem.split('_')[-1])
            except (ValueError, IndexError):
                return 0
        bb_files = sorted(Path(backbone_dir).glob("bb_*.pdb"), key=_bb_sort_key)
        print(f"[Route B] {tmpl_name}: RFdiff 完成, 找到 {len(bb_files)} 个骨架PDB")
        for bb_path in bb_files[:n_per]:
            total_gen += 1
            try:
                binder_chain = _infer_binder_chain(str(bb_path), L, receptor_chain=config["chain"])
                binder_res = _parse_binder_residues(str(bb_path), binder_chain)
            except (OSError, UnicodeError, ValueError) as exc:
                EvidenceLogger.error(
                    "design", "rfdiff_binder_chain_invalid",
                    f"{bb_path}: {exc}", recovery="skip ambiguous backbone",
                )
                continue
            fixed_res = _hotspot_fixed_residues(tmpl_hotspots, binder_res) if binder_res else ""
            if tmpl_hotspots and not fixed_res:
                EvidenceLogger.error("design", "hotspot_anchors_all_out_of_range",
                    f"template {tmpl_name!r} has {len(tmpl_hotspots)} hotspot(s) "
                    f"but none mapped to binder residues (binder_len={len(binder_res) if binder_res else 0}); "
                    f"Route B proceeds without fixed residues — motif guidance is DEACTIVATED",
                    recovery="verify template-to-backbone alignment or adjust hotspot positions")
            mpnn_dir = os.path.join(batch_dir, f"mpnn_{bb_path.stem}")
            os.makedirs(mpnn_dir, exist_ok=True)
            mpnn_seed = (config["seed"] + total_gen) % 2**31
            seqs = _run_ligandmpnn(str(bb_path), mpnn_dir, n_seq=8,
                binder_chain=binder_chain, fixed_residues=fixed_res or None,
                seed=mpnn_seed)
            if not seqs:
                print(f"[Route B] LigandMPNN 返回 0 条序列: {bb_path.name}")
                continue
            backbone_entries.append((bb_path, binder_chain, seqs))

    # Pass 2: global cheap filter — score ALL sequences together (P2-3).
    all_raw_seqs = []
    bb_lookup = {}
    for bb_path, binder_chain, seqs in backbone_entries:
        for s in seqs:
            key = s.upper() if isinstance(s, str) else ""
            if key:
                bb_lookup.setdefault(key, []).append((bb_path, binder_chain))
        all_raw_seqs.extend(seqs)

    filtered = _cheap_filter_sequences(all_raw_seqs, seen_seqs=seen_seqs, top_k=config["n"])
    print(f"[Route B] global cheap filter: {len(all_raw_seqs)}→{len(filtered)} sequences")

    for seq, quality_score in filtered:
        bb_list = bb_lookup.get(seq)
        if not bb_list:
            continue
        bb_path, binder_chain = bb_list[0]
        bb_alternatives = [str(bp) for bp, _ in bb_list[1:]] if len(bb_list) > 1 else []
        cid = _next_candidate_id()
        refold_dir = os.path.join(batch_dir, "candidates", cid)
        os.makedirs(refold_dir, exist_ok=True)
        refold_pdb = os.path.join(refold_dir, "refold.pdb")
        plddt = _run_refold(seq, refold_pdb)
        cyclization = _infer_cyclization_type(seq)
        try:
            rc = (
                _ring_closure_check(refold_pdb, cyclization, sequence=seq)
                if os.path.exists(refold_pdb)
                else {"pass": False, "reason": "refold_pdb_missing"}
            )
        except (ValueError, OSError) as exc:
            rc = {"pass": False, "reason": f"closure_check_error: {exc}"}

        if plddt is not None and rc.get("pass"):
            total_valid += 1
            try:
                manifest = _write_manifest(
                    cid, seq, route_name, batch_id, refold_pdb, config,
                    backbone_pdb=str(bb_path), cyclization=cyclization,
                    ring_closure=rc, bb_alternatives=bb_alternatives,
                )
            except ValueError as exc:
                EvidenceLogger.error("design", "manifest_cyclization_mismatch",
                    str(exc), recovery="skip mismatched candidate (P1-7)")
                continue
            candidate = _candidate_from_manifest(
                manifest, plddt,
                notes={"quality_score": round(quality_score, 3)},
            )
            CandidateIndex.add(candidate)
            EvidenceLogger.log("design", "candidate_registered",
                {"candidate": candidate},
                targets=[config["target_id"]], phase="design")
            candidates.append(candidate)
        else:
            print(f"[Route B] refold失败: {cid} pLDDT={plddt} ring_closed={rc.get('pass')}")

    if total_gen == 0 and templates:
        EvidenceLogger.error("design", "route_b_all_templates_filtered",
            f"{len(templates)} template(s) provided but none passed the "
            f"length gate (8–20 residues); check known_dual_binders sequences",
            recovery="verify Research output contains valid-length binders")
    EvidenceLogger.design_batch(route=route_name, n_generated=total_gen,
        n_valid=total_valid, tool_name="rfpeptides_motif",
        tool_version=DESIGN_PIPELINE_VERSION,
        duration_sec=round(time.time()-t_batch, 1))
    return candidates


# ============================================================
# Route C: binder 模板环化改造（适配 Research 任意产出）
# ============================================================

def _route_c_design_references(config, batch_dir, sequences):
    """Generate one independent target-bound RFdiffusion backbone per sequence.

    Route C is sequence/scaffold driven, but Prediction L7 still needs a
    structural hypothesis that was produced independently of the fixed-sequence
    refold.  The returned mapping is keyed by the sequence's stable position in
    *sequences*.  Missing or ambiguous RFdiffusion outputs are omitted so the
    caller can fail closed before registering a candidate.
    """
    indexed_by_length = {}
    for index, (sequence, _description) in enumerate(sequences):
        indexed_by_length.setdefault(len(sequence), []).append(index)

    hotspots = _parse_hotspot_residues(config.get("hotspots", ""))
    target_start, target_end = _pdb_residue_range(
        config["target_pdb"], config["chain"], hotspot_residues=hotspots
    )
    references = {}
    for length, indexes in sorted(indexed_by_length.items()):
        backbone_dir = Path(batch_dir) / f"design_references_len{length}"
        backbone_dir.mkdir(parents=True, exist_ok=True)
        output_prefix = str(backbone_dir / "bb")
        completed = _run_rfdiff(
            target_pdb=config["target_pdb"],
            binder_len=length,
            n_designs=len(indexes),
            output_prefix=output_prefix,
            contig=_binder_first_contig(
                config["chain"], target_start, target_end, length
            ),
            seed=config["seed"],
            hotspots=config.get("hotspots"),
            chain=config["chain"],
        )
        if not completed:
            EvidenceLogger.error(
                "design",
                "route_c_design_reference_generation_failed",
                f"RFdiffusion failed for Route C length {length}",
                recovery="regenerate this Route C length before Prediction",
            )
            continue

        def sort_key(path):
            try:
                return int(path.stem.rsplit("_", 1)[-1])
            except (ValueError, IndexError):
                return -1

        valid_backbones = []
        for backbone_path in sorted(backbone_dir.glob("bb_*.pdb"), key=sort_key):
            try:
                _infer_binder_chain(
                    str(backbone_path), length, receptor_chain=config["chain"]
                )
            except (OSError, UnicodeError, ValueError) as exc:
                EvidenceLogger.error(
                    "design",
                    "route_c_design_reference_invalid",
                    f"{backbone_path}: {exc}",
                    recovery="skip ambiguous Route C reference backbone",
                )
                continue
            valid_backbones.append(backbone_path)

        for index, backbone_path in zip(indexes, valid_backbones):
            references[index] = str(backbone_path)
        if len(valid_backbones) < len(indexes):
            EvidenceLogger.error(
                "design",
                "route_c_design_reference_incomplete",
                {
                    "length": length,
                    "required": len(indexes),
                    "valid": len(valid_backbones),
                },
                recovery="register only candidates with an independent reference",
            )
    return references

def design_atsp_derived(target_spec=None, design_config=None):
    """模板环化：linker × 环化矩阵 + 随机突变扩展 + refold 验证
    ── 适配 Research 产出的任意 binder，不再死绑 ATSP-7041。"""
    config = _merge_config(target_spec, design_config)
    _require_mdm_reference_route("route_C_atsp")
    n = config.get("n", 200)
    seed = config["seed"]  # _merge_config already resolves None → timestamp
    import random
    rng = random.Random(seed)

    route_name = f"route_C_atsp_{target_slug(config['target_id'])}"
    batch_id = f"batch_atsp_{int(time.time())}_s{seed}_{os.urandom(4).hex()}"
    batch_dir = os.path.join(OUTPUT_DIR, "route_C", batch_id)
    os.makedirs(batch_dir, exist_ok=True)

    with open(os.path.join(batch_dir, "design_config.json"), "w") as f:
        json.dump(config, f, indent=2)

    # 从 Research 已知 binder 中选模板：优先 ATSP-7041，否则取第一个有序列的
    spec = _load_target_spec()
    binders = spec.get("known_dual_binders", [])
    template_seq = None
    template_name = None
    fallback = None
    for b in binders:
        seq_candidate = b.get("sequence") or b.get("seq", "")
        if not seq_candidate or not _validate_sequence(seq_candidate):
            continue
        if fallback is None:
            fallback = (seq_candidate, b.get("name", "unknown"))
        if "ATSP" in b.get("name", "").upper():
            template_seq, template_name = seq_candidate, b["name"]
            break
    if template_seq is None and fallback is not None:
        template_seq, template_name = fallback
        EvidenceLogger.log("design", "route_c_fallback_binder", {
            "template": template_name,
            "sequence": template_seq,
            "note": "ATSP-7041 not found in Research output; using first available "
                    "binder as cyclization template",
        })
    if not template_seq:
        EvidenceLogger.error("design", "no_binder_for_route_c",
            "known_dual_binders 中无可用的 binder 序列 — 先跑 Research Agent",
            recovery="确保 Research 产出的 known_dual_binders 包含带 sequence 的条目")
        return []
    # 标准化模板序列：去除小写/修饰符，与 _validate_sequence 的内部归一化一致，
    # 否则 AfCycDesign refold 收到非标准氨基酸会静默失败（P0-3）。
    template_seq = template_seq.upper().replace("-", "").replace("*", "")
    # Route C 序列设计: linker × 环化 全矩阵
    base_combos = []
    for linker in LINKER_MATRIX:
        for cn, cc in CYCLIZATION_PAIRS:
            seq = f"{cn}{template_seq}{linker}{cc}"
            if not _validate_sequence(seq):
                continue
            violations = _synthesizability_violations(seq)
            # head-to-tail 环化允许内部 Cys；只有二硫键环化才要求只有末端 Cys
            if cn == "" and cc == "":
                violations = [v for v in violations if "stray_cys" not in v]
            if not violations:
                base_combos.append((seq, _describe_cyclize(cn, cc, linker)))

    if not base_combos:
        EvidenceLogger.error("design", "route_c_empty",
            f"All {len(LINKER_MATRIX) * len(CYCLIZATION_PAIRS)} cyclization "
            "combos for template {template_name!r} failed the synthesizability "
            "gate — no viable sequences.",
            recovery="review template sequence and cyclization pairs for "
                     "synthesizability conflicts (NG deamidation, DP cleavage, "
                     "stray Cys, aggregation)")
        return []

    # 第2级：不够 n 则随机突变扩展；基础组合需先按已有序列去重
    # F/W/L 药效团位点保护在 L730 通过 if seq[ix] in "FWL": continue 实现
    seen_seqs = _load_existing_sequences() or set()  # cross-batch dedup (None → set())
    expanded = []
    for s, d in base_combos:
        if s not in seen_seqs:
            seen_seqs.add(s)
            expanded.append((s, d))
    attempts = 0
    while len(expanded) < n and attempts < n * 10:
        attempts += 1
        seq, desc = rng.choice(base_combos)
        aa = rng.choice(SCAFFOLD_MUTABLE_AA)
        off = 1 if seq and seq[0] == "C" else 0
        tail_guard = 1 if seq and seq[-1] == "C" else 0
        max_pos = len(seq) - off - tail_guard  # mutable core length
        if max_pos < 2:
            continue  # nowhere to mutate without breaking a terminal Cys
        pos = rng.randint(1, max_pos)
        ix = off + pos - 1
        # 保护 F/W/L 药效团位点（ATSP-7041 核心锚点）
        if seq[ix] in "FWL":
            continue
        # 同义突变不改变序列，浪费 attempts budget（P1-4）
        if aa == seq[ix]:
            continue
        mutated = seq[:ix] + aa + seq[ix+1:]
        if _validate_sequence(mutated) and mutated not in seen_seqs:
            violations = _synthesizability_violations(mutated)
            # head-to-tail 父本允许内部 Cys（只有 Cys-Cys 环化才严格要求末端 Cys）
            if seq[0] != "C" and seq[-1] != "C":
                violations = [v for v in violations if "stray_cys" not in v]
            if not violations:
                seen_seqs.add(mutated)
                expanded.append((mutated, f"{desc},mut:{ix+1}={aa}"))

    if len(expanded) < n:
        EvidenceLogger.log("design", "route_c_under_target", {
            "target": n,
            "achieved": len(expanded),
            "base_combos": len(base_combos),
            "attempts": attempts,
            "reason": "mutation space exhausted — consider increasing n*10 "
                      "budget or relaxing synthesizability gates",
        })

    selected_sequences = expanded[:n]
    design_references = _route_c_design_references(
        config, batch_dir, selected_sequences
    )

    candidates = []
    total_gen, total_valid = 0, 0
    t_batch = time.time()

    for sequence_index, (seq, desc) in enumerate(selected_sequences):
        backbone_pdb = design_references.get(sequence_index)
        if not backbone_pdb:
            EvidenceLogger.error(
                "design",
                "route_c_design_reference_unavailable",
                {
                    "sequence_index": sequence_index,
                    "sequence_length": len(seq),
                    "sequence_sha256": hashlib.sha256(seq.encode()).hexdigest(),
                },
                recovery="do not register or predict this candidate; regenerate Design",
            )
            continue
        total_gen += 1
        cid = _next_candidate_id()
        refold_dir = os.path.join(batch_dir, "candidates", cid)
        os.makedirs(refold_dir, exist_ok=True)
        refold_pdb = os.path.join(refold_dir, "refold.pdb")
        plddt = _run_refold(seq, refold_pdb)
        cyclization_type = _infer_cyclization_type(seq)
        try:
            rc = (
                _ring_closure_check(refold_pdb, cyclization_type, sequence=seq)
                if os.path.exists(refold_pdb)
                else {"pass": False, "reason": "refold_pdb_missing"}
            )
        except (ValueError, OSError) as exc:
            rc = {"pass": False, "reason": f"closure_check_error: {exc}"}

        if plddt is not None and rc.get("pass"):
            total_valid += 1
            try:
                manifest = _write_manifest(
                    cid, seq, route_name, batch_id, refold_pdb, config,
                    backbone_pdb=backbone_pdb,
                    cyclization=cyclization_type, ring_closure=rc,
                )
            except ValueError as exc:
                EvidenceLogger.error("design", "manifest_cyclization_mismatch",
                    str(exc), recovery="skip mismatched candidate (P1-7)")
                continue
            candidate = _candidate_from_manifest(manifest, plddt, notes={"design": desc})
            CandidateIndex.add(candidate)
            EvidenceLogger.log("design", "candidate_registered",
                {"candidate": candidate},
                targets=[config["target_id"]], phase="design")
            candidates.append(candidate)
        else:
            EvidenceLogger.error("design", "refold_failed",
                f"{cid}: pLDDT={plddt}", recovery="skip")

    EvidenceLogger.design_batch(route=route_name, n_generated=total_gen,
        n_valid=total_valid, tool_name="atsp_derived",
        tool_version=DESIGN_PIPELINE_VERSION,
        duration_sec=round(time.time()-t_batch, 1))
    return candidates


# ============================================================
# 评分 — 阈值过滤 + Pareto 前沿
# ============================================================

def threshold_filter(candidates, thresholds, project_config=None):
    """Apply independent per-target ipSAE and hotspot-coverage gates."""
    project = project_config or ACTIVE_PROJECT_CONFIG
    target_ids = required_target_ids(project)

    def _safe_float(value):
        """Return float(value); return None for missing, empty, or non-finite values."""
        if value in (None, "") or isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            number = float(value)
        except (ValueError, TypeError):
            return None
        return number if math.isfinite(number) else None

    def _resolve_threshold(*candidates):
        """Return the first candidate that passes _safe_float."""
        for value in candidates:
            resolved = _safe_float(value)
            if resolved is not None:
                return resolved
        return None

    # MDM2/MDMX provisional legacy defaults — only for the bundled example projects.
    _pid = project.get("project_id", "").casefold()
    _MDM_PROJECT_IDS = frozenset({
        "mdm2_mdmx_reference", "design_v5_mdm2_mdmx", "design_v5_mdm2_mdmx_test",
    })
    _is_mdm_project = _pid in _MDM_PROJECT_IDS
    if _is_mdm_project:
        EvidenceLogger.log("design", "mdm_legacy_defaults_active", {
            "project_id": _pid,
            "note": "uncalibrated provisional fallback thresholds (ipsae=0.6/0.5, "
                    "hotspot_cov=0.67) may be used when per-target configuration "
                    "is absent; calibrate against positive/negative controls",
        })
    # Pre-resolve per-target thresholds once (P3-1).  None of these depend on
    # individual candidates, so hoisting them out of the inner loop avoids
    # repeated dict lookups and legacy-fallback checks.
    _target_thresholds = []  # (target_id, slug, ipsae_threshold, hotspot_threshold)
    for target_id in target_ids:
        slug = target_slug(target_id)
        ipsae_rule = threshold_for_target(thresholds, "L2_ipsae", target_id)
        hotspot_rule = threshold_for_target(thresholds, "L5_hotspot_coverage", target_id)
        _ipsae_candidates = [
            thresholds.get(f"ipsae_{slug}"),
            thresholds.get("ipsae"),
            ipsae_rule.get("value"),
        ]
        ipsae_threshold = _resolve_threshold(*_ipsae_candidates)
        ipsae_from_legacy = False
        if _is_mdm_project and ipsae_threshold is None:
            _LEGACY_MDM_THRESHOLDS = {"mdm2": 0.6, "mdmx": 0.5}
            ipsae_threshold = _LEGACY_MDM_THRESHOLDS.get(slug)
            if ipsae_threshold is not None:
                ipsae_from_legacy = True
        if ipsae_from_legacy:
            if os.environ.get("CYCPEP_ALLOW_UNVALIDATED_MDM_THRESHOLDS") != "1":
                EvidenceLogger.error("design", "mdm_threshold_rejected", {
                    "threshold": "ipsae", "value": ipsae_threshold, "target": slug,
                    "remediation": "calibrate per-target ipsae threshold in "
                                    "project_config, or set "
                                    "CYCPEP_ALLOW_UNVALIDATED_MDM_THRESHOLDS=1",
                })
                return []
            EvidenceLogger.error("design", "mdm_uncalibrated_threshold_used", {
                "threshold": "ipsae", "value": ipsae_threshold, "target": slug,
                "remediation": "calibrate per-target ipsae threshold against "
                                "positive/negative controls in project_config",
            })
        _hotspot_candidates = [
            thresholds.get(f"hotspot_cov_{slug}"),
            thresholds.get("hotspot_cov"),
            hotspot_rule.get("value"),
        ]
        hotspot_threshold = _resolve_threshold(*_hotspot_candidates)
        hotspot_from_legacy = False
        if _is_mdm_project and hotspot_threshold is None:
            hotspot_threshold = 0.67
            hotspot_from_legacy = True
        if hotspot_from_legacy:
            if os.environ.get("CYCPEP_ALLOW_UNVALIDATED_MDM_THRESHOLDS") != "1":
                EvidenceLogger.error("design", "mdm_threshold_rejected", {
                    "threshold": "hotspot_cov", "value": 0.67, "target": slug,
                    "remediation": "calibrate per-target hotspot_cov threshold "
                                    "in project_config, or set "
                                    "CYCPEP_ALLOW_UNVALIDATED_MDM_THRESHOLDS=1",
                })
                return []
            EvidenceLogger.error("design", "mdm_uncalibrated_threshold_used", {
                "threshold": "hotspot_cov", "value": 0.67, "target": slug,
                "remediation": "calibrate per-target hotspot_cov threshold "
                                "against positive/negative controls",
            })
        _target_thresholds.append((target_id, slug, ipsae_threshold, hotspot_threshold))

    passed = []
    for candidate in candidates:
        accepted = True
        for target_id, slug, ipsae_threshold, hotspot_threshold in _target_thresholds:
            ipsae_val = _safe_float(target_value(candidate, target_id, "ipsae"))
            hotspot_val = _safe_float(target_value(candidate, target_id, "hotspot_cov"))
            if ipsae_threshold is None:
                rejected_by = f"missing_ipsae_threshold_{slug}"
            elif hotspot_threshold is None:
                rejected_by = f"missing_hotspot_threshold_{slug}"
            elif ipsae_val is None:
                rejected_by = f"ipsae_nil_{slug}"
            elif hotspot_val is None:
                rejected_by = f"hotspot_nil_{slug}"
            elif ipsae_val < ipsae_threshold:
                rejected_by = f"ipsae_below_{slug}"
            elif hotspot_val < hotspot_threshold:
                rejected_by = f"hotspot_below_{slug}"
            else:
                rejected_by = None
            if rejected_by:
                accepted = False
                break
        if accepted:
            passed.append(candidate)
    return passed


def pareto_front(candidates, obj_x=None, obj_y=None, project_config=None):
    """Thin wrapper around data_layer.compute_pareto_front().

    The data-layer implementation handles missing objectives (exclude),
    mixed direction (maximize / minimize), per-target metrics, and
    candidate-ID validity — do NOT maintain a duplicate algorithm here.
    """
    project = project_config or ACTIVE_PROJECT_CONFIG
    if obj_x is None:
        target_ids = required_target_ids(project)
        # data_layer expects {"target": ..., "metric": ..., "direction": ...}
        objectives = tuple(
            {"target": tid, "metric": "ipsae", "direction": "maximize"}
            for tid in target_ids[:2]
        )
    else:
        objectives = (obj_x,) if obj_y is None else (obj_x, obj_y)

    from data_layer import compute_pareto_front
    front_ids = set(compute_pareto_front(candidates, objectives))
    return [c for c in candidates if c.get("candidate_id") in front_ids]


# ============================================================
# candidate_manifest.json
# ============================================================

def _write_manifest(
        cid, seq, route, batch_id, refold_pdb, config, backbone_pdb=None,
        cyclization=None, ring_closure=None, bb_alternatives=None):
    """Write one versioned candidate manifest with audited closure geometry."""
    refold_dir = os.path.dirname(refold_pdb)
    manifest_path = os.path.join(refold_dir, "manifest.json")
    if cyclization is None:
        cyclization = _infer_cyclization_type(seq)
    cyclization_description = str(cyclization)
    canonical_cyclization = _canonical_cyclization_type(
        cyclization_description, sequence=seq
    )
    rc = copy.deepcopy(ring_closure) if ring_closure is not None else None
    if rc is None:
        rc = (
            _ring_closure_check(
                refold_pdb, canonical_cyclization, sequence=seq
            )
            if os.path.exists(refold_pdb)
            else {"pass": False, "reason": "refold_pdb_missing"}
        )
    observed_type = rc.get("cyclization_type")
    if observed_type and observed_type != canonical_cyclization:
        raise ValueError(
            f"[{cid}] ring-closure result cyclization does not match manifest: "
            f"{observed_type!r} != {canonical_cyclization!r}"
        )
    design_reference = ""
    design_reference_hash = ""
    design_reference_role = ""
    if backbone_pdb:
        reference_path = os.path.realpath(str(backbone_pdb))
        refold_path = os.path.realpath(str(refold_pdb))
        if not os.path.isfile(reference_path):
            raise ValueError(f"[{cid}] Design reference does not exist: {reference_path}")
        if reference_path == refold_path:
            raise ValueError(
                f"[{cid}] fixed-sequence refold cannot be its own L7 Design reference"
            )
        design_reference = reference_path
        design_reference_hash = file_hash(reference_path)
        refold_hash = file_hash(refold_path) if os.path.exists(refold_path) else ""
        if refold_hash and design_reference_hash == refold_hash:
            raise ValueError(
                f"[{cid}] L7 Design reference is byte-identical to fixed-sequence refold"
            )
        design_reference_role = "rfdiffusion_target_bound_backbone"

    manifest = {
        "design_pipeline_version": DESIGN_PIPELINE_VERSION,
        "candidate_id": cid, "sequence": seq, "length": len(seq),
        "source_route": route, "source_batch": batch_id,
        "cyclization_type": canonical_cyclization,
        "cyclization_description": cyclization_description,
        "refold_pdb": refold_pdb,
        "refold_pdb_hash": file_hash(refold_pdb) if os.path.exists(refold_pdb) else "",
        # Explicit v5.2 contract.  backbone_* remains a compatibility alias for
        # older Prediction readers and historical manifests.
        "design_reference_pdb": design_reference,
        "design_reference_pdb_hash": design_reference_hash,
        "design_reference_role": design_reference_role,
        "backbone_pdb": design_reference,
        "backbone_pdb_hash": design_reference_hash,
        "backbone_alternatives": bb_alternatives or [],
        "ring_closure": rc,
        "design_config_summary": {
            "project_id": config.get("project_id"),
            "target": config.get("target_id"),
            "target_pdb": config.get("target_pdb"),
            "target_pdb_sha256": config.get("target_pdb_sha256"),
            "seed": config.get("seed"),
        }
    }
    manifest["manifest_path"] = manifest_path
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def _manifest_summary(manifest):
    return {
        key: manifest[key]
        for key in [
            "design_pipeline_version", "candidate_id", "sequence",
            "refold_pdb_hash", "manifest_path",
        ]
        if key in manifest
    }


def _candidate_from_manifest(manifest, plddt, notes=None):
    """Convert a v5 manifest into the stable dev candidate handoff contract."""
    length = manifest["length"]
    cyclization = manifest["cyclization_type"]
    if "head-to-tail_amide" in cyclization:
        bonds = [{
            "atom_1": f"residue_{length}:C",
            "atom_2": "residue_1:N",
            "bond_type": "amide",
        }]
    elif "Cys-Cys_disulfide" in cyclization:
        bonds = [{
            "atom_1": "residue_1:SG",
            "atom_2": f"residue_{length}:SG",
            "bond_type": "disulfide",
        }]
    else:
        EvidenceLogger.error("design", "unknown_cyclization_bonds", {
            "cyclization_type": cyclization,
            "candidate_id": manifest["candidate_id"],
            "remediation": "add bond geometry to _candidate_from_manifest",
        })
        raise ValueError(
            f"unsupported cyclization type {cyclization!r} — cannot determine "
            f"cyclization bonds for candidate {manifest['candidate_id']}"
        )
    note_payload = {**_manifest_summary(manifest), **(notes or {})}
    return {
        "candidate_id": manifest["candidate_id"],
        "sequence": manifest["sequence"],
        "length": length,
        "source_route": manifest["source_route"],
        "source_batch": manifest["source_batch"],
        "cyclization_type": cyclization,
        "cyclization_bonds": bonds,
        "design_pdb_path": manifest["refold_pdb"],
        "design_pdb_hash": manifest["refold_pdb_hash"],
        "manifest_path": manifest["manifest_path"],
        "monomer_plddt": round(float(plddt), 3),
        "notes": json.dumps(note_payload, ensure_ascii=False),
    }


# ============================================================
# 工具调用封装
# ============================================================

def _binder_first_contig(target_chain, target_start, target_end, binder_len):
    """Build the RFdiffusion macrocyclic-binder contig in official chain order.

    RFdiffusion assigns the first contig segment to internal chain ``a``.
    Because ``inference.cyc_chains=a`` is used below, the generated binder must
    be the first segment and the fixed receptor must follow it.
    """
    chain = str(target_chain or "").strip()
    if len(chain) != 1 or not chain.isalpha() or not chain.isupper():
        raise ValueError(
            f"target chain must be a single uppercase PDB chain ID, got {target_chain!r}"
        )
    start, end, length = int(target_start), int(target_end), int(binder_len)
    if start > end:
        raise ValueError(f"target residue range is reversed: {start}-{end}")
    if not 8 <= length <= 20:
        raise ValueError(f"binder length must be 8-20, got {length}")
    return f"{length}-{length} {chain}{start}-{end}/0"


def _run_rfdiff(target_pdb, binder_len, n_designs, output_prefix, contig,
                seed=None, hotspots=None, chain="A"):
    """RFdiffusion 子进程。hotspots: 逗号分隔的残基号如 '54,93,96'

    .. note::

        ``seed`` is **intentionally ignored** for RFdiffusion backbone generation
        because the GPU path is non-deterministic at the hardware level.  The seed
        parameter is accepted for API consistency with the rest of the pipeline and
        is only consumed by LigandMPNN and Route C expansion.
    """
    # Deferred log for invalid RFDIFF_TIMESTEPS (P1: no EvidenceLogger at import).
    global _RFDIFF_TIMESTEPS_INVALID
    if _RFDIFF_TIMESTEPS_INVALID is not None:
        EvidenceLogger.log("design", "invalid_RFDIFF_TIMESTEPS",
            {"value": _RFDIFF_TIMESTEPS_INVALID, "fallback": 50})
        _RFDIFF_TIMESTEPS_INVALID = None

    if seed is not None:
        import warnings
        warnings.warn(
            f"RFdiffusion seed={seed} is ignored — GPU non-deterministic backbone",
            stacklevel=2,
        )
    cmd = [
        RFDIFF_PYTHON, f"{RFDIFF_DIR}/scripts/run_inference.py",
        f"inference.input_pdb={target_pdb}",
        "inference.cyclic=True",
        "inference.cyc_chains=a",
        f"inference.num_designs={n_designs}",
        f"inference.output_prefix={output_prefix}",
        f"contigmap.contigs=['{contig}']",
        f"diffuser.T={RFDIFF_TIMESTEPS}",
    ]
    if hotspots:
        # 补链名前缀: "54,93,96" → "'A54','A93','A96'"（Hydra 要求每个残基加引号）
        formatted = ",".join(f"'{chain}{r.strip()}'" for r in hotspots.split(",") if r.strip())
        if formatted:
            cmd.append(f"ppi.hotspot_res=[{formatted}]")
    try:
        _rfdiff_timeout = int(os.environ.get("RFDIFF_TIMEOUT") or "3600")
    except (ValueError, TypeError):
        _rfdiff_timeout = 3600
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_rfdiff_timeout,
            cwd=RFDIFF_DIR,
            env=_rfdiff_subprocess_env())
        if r.returncode != 0:
            print(f"[RFdiff 失败] exit={r.returncode}")
            print(f"  stderr: {r.stderr[-500:]}")
            EvidenceLogger.error("design", "rfdiff_failed",
                f"exit={r.returncode} stderr={r.stderr[-300:]}")
            _cleanup_partial_rfdiff_output(output_prefix)
            return False
        return True
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        print(f"[RFdiff 异常] {e}")
        EvidenceLogger.error("design", "rfdiff_exception", str(e))
        _cleanup_partial_rfdiff_output(output_prefix)
        return False


def _cleanup_partial_rfdiff_output(output_prefix):
    """Remove incomplete PDB files left by a failed/timed-out RFdiffusion run."""
    prefix_dir = os.path.dirname(output_prefix)
    prefix_name = os.path.basename(output_prefix)
    try:
        for pdb in Path(prefix_dir).glob(f"{prefix_name}_*.pdb"):
            pdb.unlink()
    except OSError:
        pass


def _run_ligandmpnn(backbone_pdb, output_dir, n_seq=8, binder_chain=None,
                    fixed_residues=None, seed=42):
    """LigandMPNN subprocess with an explicitly validated binder chain.

    The RFdiffusion output chain labels are discovered from the emitted PDB,
    rather than inferred from the input receptor's chain label.
    fixed_residues: 空格分隔的 chain+resi 列表，如 'B25 B26 B27'，这些残基在 LigandMPNN 中固定不变。"""
    if LIGANDMPNN_MODEL_TYPE != "protein_mpnn":
        EvidenceLogger.error(
            "design", "unsupported_inverse_folding_model",
            f"LIGANDMPNN_MODEL_TYPE={LIGANDMPNN_MODEL_TYPE!r}; "
            "the validated protein-target workflow requires 'protein_mpnn'",
            recovery="use protein_mpnn or add a separately tested adapter",
        )
        return []
    try:
        layout = _pdb_chain_residue_layout(backbone_pdb)
        input_sequences = _pdb_chain_sequences(backbone_pdb)
    except (OSError, UnicodeError, ValueError) as exc:
        EvidenceLogger.error(
            "design", "ligandmpnn_backbone_invalid", str(exc), recovery="skip"
        )
        return []
    binder_chain = str(binder_chain or "").strip()
    if binder_chain not in layout:
        EvidenceLogger.error(
            "design", "ligandmpnn_binder_chain_missing",
            f"{backbone_pdb}: binder chain {binder_chain!r} is absent",
            recovery="skip",
        )
        return []
    try:
        configured_max = int(os.environ.get("LIGANDMPNN_MAX_BATCH_SIZE", "4"))
    except (ValueError, TypeError):
        configured_max = 4
    configured_max = max(1, min(configured_max, 32))
    batch_size = min(max(1, int(n_seq)), configured_max)
    number_of_batches = max(1, (int(n_seq) + batch_size - 1) // batch_size)
    cmd = [
        RFDIFF_PYTHON, f"{LIGANDMPNN_DIR}/run.py",
        "--model_type", LIGANDMPNN_MODEL_TYPE,
        f"--checkpoint_protein_mpnn={LIGANDMPNN_CHECKPOINT}",
        f"--pdb_path={backbone_pdb}",
        f"--out_folder={output_dir}",
        f"--batch_size={batch_size}",
        f"--number_of_batches={number_of_batches}",
        "--temperature=0.1", f"--seed={seed}",
        "--fasta_seq_separation=:",
        f"--chains_to_design={binder_chain}",
    ]
    if fixed_residues:
        cmd.append(f"--fixed_residues={fixed_residues}")
    # Wipe the entire output directory so no orphaned file from a previous
    # LigandMPNN run (FASTA, PDB, log, etc.) can be mistaken for new output.
    # LigandMPNN expects a clean or non-existent directory (P1-6).
    # ignore_errors=True already suppresses all OSError; no need for try/except.
    shutil.rmtree(output_dir, ignore_errors=True)
    os.makedirs(output_dir, exist_ok=True)
    try:
        _ligandmpnn_timeout = int(os.environ.get("LIGANDMPNN_TIMEOUT") or "600")
    except (ValueError, TypeError):
        _ligandmpnn_timeout = 600
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_ligandmpnn_timeout,
            cwd=LIGANDMPNN_DIR,
            env=_rfdiff_subprocess_env())
        if r.returncode != 0:
            print(f"[LigandMPNN 失败] exit={r.returncode} stderr={r.stderr[-300:]}")
            return []
        seqs = []
        fa_files = sorted(Path(output_dir).glob("**/*.fa"))
        if len(fa_files) > 1:
            EvidenceLogger.log("design", "ligandmpnn_multiple_fasta", {
                "backbone_pdb": str(backbone_pdb),
                "fasta_count": len(fa_files),
                "fa_files": [str(p) for p in fa_files],
            })
        for fa in fa_files:
            with open(fa) as fh:
                raw_lines = fh.readlines()
            # Detect FASTA header convention: LigandMPNN uses ", id=" markers.
            # If no header contains this marker (e.g. after an upstream format
            # change), fall back to positional heuristics: first record is the
            # reference complex, every subsequent record is a generated design.
            headers = [ln.strip() for ln in raw_lines if ln.strip().startswith(">")]
            uses_id_marker = any(", id=" in h for h in headers)
            if not uses_id_marker and len(headers) > 1:
                EvidenceLogger.log("design", "ligandmpnn_fasta_no_id_marker", {
                    "backbone_pdb": str(backbone_pdb),
                    "fasta_file": str(fa),
                    "header_count": len(headers),
                    "fallback": "positional — first record treated as reference, "
                                "subsequent records as generated",
                })
            header_index = 0
            is_generated_record = False
            ref_binder_seq = None  # captured for positional-fallback validation
            seq_buffer = ""
            # Iterate once more at the end to flush the final accumulated sequence.
            lines_iter = iter(raw_lines)
            exhausted = object()
            while True:
                line = next(lines_iter, exhausted)
                if line is exhausted or line.strip().startswith(">"):
                    # Flush the accumulated sequence for the previous record.
                    if seq_buffer and is_generated_record:
                        try:
                            seq = _extract_ligandmpnn_binder_sequence(
                                seq_buffer, binder_chain, layout, input_sequences
                            )
                        except (OSError, UnicodeError, ValueError) as exc:
                            EvidenceLogger.error(
                                "design", "ligandmpnn_fasta_invalid",
                                f"{fa}: {exc}", recovery="skip malformed output",
                            )
                            seq = None
                        if seq is not None:
                            # 跳过纯 homopolymer（LigandMPNN baseline artifact）
                            if len(set(seq)) > 1 and seq not in seqs:
                                # Positional-fallback guard: if the generated
                                # sequence is nearly identical to the reference
                                # complex, the record order may have changed.
                                if ref_binder_seq is not None and len(ref_binder_seq) == len(seq):
                                    identical = sum(a == b for a, b in zip(ref_binder_seq, seq))
                                    similarity = identical / len(seq) if len(seq) > 0 else 0
                                    if similarity > 0.8:
                                        EvidenceLogger.error(
                                            "design", "ligandmpnn_fallback_suspicious",
                                            f"{fa}: generated record #{header_index} is "
                                            f"{similarity:.0%} identical to reference — "
                                            f"positional fallback may have mis-identified "
                                            f"the reference complex as a design; "
                                            f"sequence SKIPPED to avoid contaminating "
                                            f"the candidate pool with a known native sequence",
                                            recovery="verify LigandMPNN FASTA header format",
                                        )
                                        continue  # P0-1: 拒绝参考序列混入候选池
                                seqs.append(seq)
                    elif seq_buffer and not uses_id_marker and ref_binder_seq is None:
                        # Positional fallback: capture reference binder sequence
                        # from the first (non-generated) record for later
                        # similarity validation.
                        try:
                            ref_binder_seq = _extract_ligandmpnn_binder_sequence(
                                seq_buffer, binder_chain, layout, input_sequences
                            )
                        except (OSError, UnicodeError, ValueError):
                            ref_binder_seq = None
                    if line is exhausted:
                        break
                    # Start a new record.
                    seq_buffer = ""
                    line = line.strip()
                    header_index += 1
                    if uses_id_marker:
                        # ", id=0" (or ", id= 0") → native reference complex
                        is_generated_record = (
                            ", id=" in line
                            and ",id=0" not in line.replace(" ", "")
                        )
                    else:
                        is_generated_record = header_index > 1
                    continue
                if is_generated_record:
                    seq_buffer += line.strip()
        return seqs[:n_seq]
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        EvidenceLogger.error("design", "ligandmpnn_exception", str(e))
        return []


def _rfdiff_subprocess_env():
    """Reproduce the validated rfdiffusion-design ``activate.d`` runtime."""
    env = dict(os.environ)
    python_version = os.environ.get("RFDIFF_PYTHON_VERSION", "3.10")
    site_packages = f"{RFDIFF_CONDA}/lib/python{python_version}/site-packages"
    python_paths = [SE3_ROOT, RFDIFF_DIR]
    if env.get("PYTHONPATH"):
        python_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    env["DGLBACKEND"] = "pytorch"

    library_paths = [
        f"{RFDIFF_CONDA}/lib",
        f"{site_packages}/torch/lib",
        *(
            f"{site_packages}/nvidia/{package}/lib"
            for package in [
                "cusolver", "cuda_nvrtc", "cuda_runtime", "cublas", "cusparse",
                "nvjitlink", "cuda_cupti", "cufft", "cudnn", "nccl", "curand", "nvtx",
            ]
        ),
    ]
    if env.get("LD_LIBRARY_PATH"):
        library_paths.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = os.pathsep.join(library_paths)
    return env


def _build_refold_script(sequence, output_pdb):
    """Build a fixed-sequence AfCycDesign prediction script.

    ``design_3stage`` optimizes sequence logits and therefore cannot be used
    for refolding an already designed LigandMPNN sequence.  Prediction uses
    ``predict(seq=...)`` and verifies both ColabDesign's hard sequence and the
    emitted PDB before the manifest is allowed downstream.
    """
    if not _validate_sequence(sequence):
        raise ValueError("refold sequence must contain 8-20 standard amino acids")
    L = len(sequence)
    return f"""
import sys, subprocess, numpy as np
sys.path.insert(0, {COLABDESIGN_DIR!r})
from colabdesign import mk_af_model, clear_mem
from colabdesign.af.alphafold.model import modules as af_modules

head = subprocess.run(
    ['git', '-C', {COLABDESIGN_DIR!r}, 'rev-parse', 'HEAD'],
    capture_output=True, text=True, timeout=30, check=True,
).stdout.strip()
if head != {COLABDESIGN_COMMIT!r}:
    raise RuntimeError(
        'ColabDesign commit mismatch: expected=' + {COLABDESIGN_COMMIT!r}
        + ' observed=' + head
    )
dirty = subprocess.run(
    [
        'git', '-C', {COLABDESIGN_DIR!r}, 'status', '--porcelain',
        '--untracked-files=no'
    ],
    capture_output=True, text=True, timeout=30, check=True,
).stdout.strip()
if dirty:
    raise RuntimeError('tracked ColabDesign sources are modified')
source = open(af_modules.__file__, encoding='utf-8').read()
# Guard: verify the pinned ColabDesign commit still injects cyclic offset
# into the AF2 batch.  If the source-code pattern is absent (e.g. variable
# rename after an upstream refactor), the module-level functional smoke test
# (_verify_colabdesign_runtime) serves as a fallback gate (P1-3).
if '"offset" in batch' not in source and "'offset' in batch" not in source:
    if not {_VERIFIED_RUNTIME_SIGNATURE is not None}:
        raise RuntimeError(
            'ColabDesign backend does not consume cyclic pairwise offset '
            'and module-level functional verification has not passed — '
            'cyclic geometry may be broken'
        )

model = mk_af_model(protocol='hallucination', data_dir={COLABDESIGN_PARAMS!r})
model.prep_inputs(length={L})
model.restart(seed=0, seq={sequence!r})

i = np.arange({L})
ij = np.stack([i, i+{L}], -1)
offset = i[:,None] - i[None,:]
c_offset = np.abs(ij[:,None,:,None] - ij[None,:,None,:]).min((2,3))
a = c_offset < np.abs(offset)
c_offset[a] = -c_offset[a]
c_offset = c_offset * np.sign(offset)
# Smoke test: verify cyclic offset was actually applied.
# A zero matrix means the ColabDesign cyclic-offset code path was
# not executed, which would silently produce a linear peptide.
if not np.any(c_offset):
    raise RuntimeError(
        'cyclic offset matrix is all-zero — '
        'ColabDesign cyclic geometry was not applied'
    )
idx = np.array(model._inputs['residue_index'])
off = np.array(idx[:,None] - idx[None,:])
off[:{L}, :{L}] = c_offset
model._inputs['offset'] = off

aux = model.predict(
    seq={sequence!r}, seed=0, models=[0], num_models=1, num_recycles=3,
    sample_models=False, dropout=False, hard=True, soft=False,
    verbose=False, return_aux=True,
)
observed = model.get_seq(get_best=False)
if observed != [{sequence!r}]:
    raise RuntimeError(
        'fixed-sequence refold drift: requested=' + repr([{sequence!r}])
        + ' observed=' + repr(observed)
    )
model.save_pdb({str(output_pdb)!r}, get_best=False, aux=aux)

aa3 = {{
    'ALA':'A','ARG':'R','ASN':'N','ASP':'D','CYS':'C','GLN':'Q','GLU':'E',
    'GLY':'G','HIS':'H','ILE':'I','LEU':'L','LYS':'K','MET':'M','PHE':'F',
    'PRO':'P','SER':'S','THR':'T','TRP':'W','TYR':'Y','VAL':'V'
}}
chains, seen = {{}}, set()
with open({str(output_pdb)!r}) as handle:
    for line in handle:
        if line.startswith('ENDMDL'):
            break
        if not line.startswith('ATOM') or line[12:16].strip() != 'CA':
            continue
        if len(line) < 27:
            continue
        key = (line[21].strip() or '_', line[22:27])
        if key in seen:
            continue
        seen.add(key)
        chains.setdefault(key[0], []).append(aa3.get(line[17:20].strip(), 'X'))
pdb_sequences = {{chain: ''.join(values) for chain, values in chains.items()}}
if len(pdb_sequences) != 1 or list(pdb_sequences.values()) != [{sequence!r}]:
    raise RuntimeError(
        'fixed-sequence PDB mismatch: requested=' + repr({sequence!r})
        + ' observed=' + repr(pdb_sequences)
    )
plddt = float(np.mean(aux['plddt']))
with open({f'{output_pdb}.plddt'!r}, 'w') as pf:
    pf.write(str(plddt))
clear_mem()
"""


def _run_refold(sequence, output_pdb):
    """
    AfCycDesign refold：hallucination 折叠固定序列为环肽。
    只做基础折叠验证。pLDDT > 0.8 的最终过滤由 Prediction Agent 的 L1 负责。
    """
    # Lazily verify ColabDesign cyclic-offset wiring once per process (P1-3).
    if _VERIFIED_RUNTIME_SIGNATURE is None:
        _verify_colabdesign_runtime()
    script = _build_refold_script(sequence, output_pdb)
    spath = os.path.join(
        tempfile.gettempdir(),
        f"refold_{os.getpid()}_{hashlib.sha256(sequence.encode()).hexdigest()[:16]}.py"
    )
    plddt_file = f"{output_pdb}.plddt"
    # A failed retry must never inherit a PDB or score produced by an older run.
    # If we cannot guarantee a clean slate we must refuse to proceed, otherwise
    # downstream consumers read stale data and every metric becomes fake.
    for stale_artifact in (output_pdb, plddt_file):
        try:
            os.unlink(stale_artifact)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(
                f"cannot remove stale artifact {stale_artifact!r} — "
                f"refusing to run with potentially contaminated output: {exc}"
            ) from exc
    with open(spath, "w") as f:
        f.write(script)
    try:
        _refold_timeout = int(os.environ.get("REFOLD_TIMEOUT") or "1200")
    except (ValueError, TypeError):
        _refold_timeout = 1200
    try:
        r = subprocess.run([CYCPEP_PYTHON, spath], capture_output=True, text=True,
            timeout=_refold_timeout,
            env={**os.environ,
                 "XLA_FLAGS": f"--xla_gpu_cuda_data_dir={CUDA_DATA_DIR}",
                 "XLA_PYTHON_CLIENT_PREALLOCATE": "false",
                 "XLA_PYTHON_CLIENT_MEM_FRACTION": "0.80"})
        if r.returncode != 0:
            EvidenceLogger.error("design", "refold_nonzero",
                f"exit={r.returncode} stderr={r.stderr[-200:]}")
            return None
        if not os.path.isfile(output_pdb) or not os.path.isfile(plddt_file):
            EvidenceLogger.error(
                "design", "refold_artifact_missing",
                f"fixed-sequence refold did not produce {output_pdb} and score",
            )
            return None
        _verify_fixed_sequence_pdb(output_pdb, sequence)
        with open(plddt_file) as pf:
            plddt = float(pf.read().strip())
        if not math.isfinite(plddt):
            raise ValueError(f"refold pLDDT is non-finite: {plddt!r}")
        if plddt < 0.0:
            raise ValueError(f"refold pLDDT is negative: {plddt!r}")
        # ColabDesign may return 0–1 (normalised) or 0–100 (raw AlphaFold)
        # depending on the installed version.  Normalise both to 0–1 so
        # downstream consumers always see a consistent scale (P0-2).
        if plddt > 1.0:
            if plddt > 100.0:
                raise ValueError(f"refold pLDDT out of range: {plddt!r}")
            plddt = plddt / 100.0
        return plddt
    except ValueError as e:
        # Distinguish sequence drift (scientific integrity) from subprocess failures.
        if "sequence" in str(e).lower() or "drift" in str(e).lower() or "mismatch" in str(e).lower():
            EvidenceLogger.error("design", "sequence_drift",
                f"refold PDB sequence diverged from input: {e}")
        else:
            EvidenceLogger.error("design", "refold_exception", str(e))
        return None
    except (subprocess.SubprocessError, OSError, RuntimeError) as e:
        EvidenceLogger.error("design", "refold_exception", str(e))
        return None
    finally:
        try:
            os.unlink(spath)
        except OSError:
            pass


def _canonical_cyclization_type(cyclization, sequence=None):
    """Return the stable manifest value while accepting legacy descriptions."""
    raw = str(cyclization or "").strip()
    if not raw:
        return _infer_cyclization_type(sequence or "")
    normalized = raw.lower().replace("_", "-")
    if "cys-cys-disulfide" in normalized:
        return "Cys-Cys_disulfide"
    if "head-to-tail-amide" in normalized:
        return "head-to-tail_amide"
    raise ValueError(f"unsupported cyclization type: {cyclization!r}")


def _infer_cyclization_type(sequence):
    """Infer the existing Design convention for routes without an explicit mode."""
    sequence = str(sequence or "").strip().upper()
    if not _validate_sequence(sequence):
        raise ValueError("cannot infer cyclization from an invalid sequence")
    if sequence.startswith("C") and sequence.endswith("C"):
        return "Cys-Cys_disulfide"
    return "head-to-tail_amide"


def _first_model_residues(pdb_path):
    """Parse canonical protein atoms from the first PDB model, fail closed."""
    chains, residue_lookup = {}, {}
    with open(pdb_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            record = line[0:6].strip()
            if record == "ENDMDL":
                break
            if record != "ATOM":
                continue
            if len(line) < 54:
                raise ValueError("short_atom_line")
            altloc = line[16:17].strip()
            if altloc not in {"", "A"}:
                continue
            chain = line[21:22].strip() or "_"
            residue_number = line[22:26].strip()
            insertion_code = line[26:27].strip()
            residue_name = line[17:20].strip().upper()
            atom_name = line[12:16].strip().upper()
            if not residue_number:
                raise ValueError("blank_residue_identifier")
            try:
                coordinate = tuple(
                    float(line[start:end])
                    for start, end in ((30, 38), (38, 46), (46, 54))
                )
            except (OSError, UnicodeError, ValueError) as exc:
                raise ValueError("invalid_atom_coordinate") from exc
            if not all(math.isfinite(value) for value in coordinate):
                raise ValueError("nonfinite_atom_coordinate")
            residue_id = (chain, residue_number, insertion_code)
            residue = residue_lookup.get(residue_id)
            if residue is None:
                residue = {
                    "chain": chain,
                    "number": residue_number,
                    "insertion_code": insertion_code,
                    "name": residue_name,
                    "atoms": {},
                }
                residue_lookup[residue_id] = residue
                chains.setdefault(chain, []).append(residue)
            elif residue["name"] != residue_name:
                raise ValueError("conflicting_residue_name")
            residue["atoms"].setdefault(atom_name, coordinate)
    if not chains:
        raise ValueError("no_protein_atoms")
    return chains


def _ring_closure_check(pdb_path, cyclization_type, sequence=None):
    """Check the actual prospective covalent atoms; never use terminal CA atoms.

    This is a pre-relax geometric compatibility gate.  It records the observed
    bond distance and both the screening and ideal ranges; it does not claim
    that a coordinate file contains a chemically instantiated covalent bond.
    """
    try:
        canonical = _canonical_cyclization_type(
            cyclization_type, sequence=sequence
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return {
            "pass": False,
            "reason": "unsupported_cyclization",
            "detail": str(exc),
        }
    criterion = CLOSURE_GEOMETRY.get(canonical)
    if criterion is None:  # P2-1: new cyclization type missing from geometry table
        return {
            "pass": False,
            "reason": "unsupported_cyclization",
            "detail": f"no closure geometry defined for {canonical!r}",
        }
    base = {
        "pass": False,
        "assessment": "pre_relax_geometry_compatibility",
        "cyclization_type": canonical,
        "atom_1": criterion["atom_1"],
        "atom_2": criterion["atom_2"],
        "screen_range_angstrom": list(criterion["screen_range_angstrom"]),
        "ideal_range_angstrom": list(criterion["ideal_range_angstrom"]),
    }
    try:
        chains = _first_model_residues(pdb_path)
        if len(chains) != 1:
            return {
                **base,
                "reason": "ambiguous_monomer_chain",
                "chains": sorted(chains),
            }
        chain, residues = next(iter(chains.items()))
        if len(residues) < 2:
            return {
                **base,
                "reason": "too_few_residues",
                "chain": chain,
                "n_residues": len(residues),
            }
        first, last = residues[0], residues[-1]
        if sequence is not None and len(residues) != len(sequence):
            return {
                **base,
                "reason": "sequence_length_mismatch",
                "chain": chain,
                "pdb_length": len(residues),
                "sequence_length": len(sequence),
            }
        if canonical == "head-to-tail_amide":
            atom_1_name, atom_2_name = "C", "N"
            atom_1 = last["atoms"].get(atom_1_name)
            atom_2 = first["atoms"].get(atom_2_name)
        else:
            if first["name"] != "CYS" or last["name"] != "CYS":
                return {
                    **base,
                    "reason": "terminal_residues_not_cysteine",
                    "first_residue": first["name"],
                    "last_residue": last["name"],
                }
            atom_1_name = atom_2_name = "SG"
            atom_1 = first["atoms"].get(atom_1_name)
            atom_2 = last["atoms"].get(atom_2_name)
        missing = []
        if atom_1 is None:
            missing.append(criterion["atom_1"])
        if atom_2 is None:
            missing.append(criterion["atom_2"])
        if missing:
            return {**base, "reason": "closure_atom_missing", "missing": missing}
        distance = math.dist(atom_1, atom_2)
        screen_min, screen_max = criterion["screen_range_angstrom"]
        ideal_min, ideal_max = criterion["ideal_range_angstrom"]
        passed = screen_min <= distance <= screen_max
        return {
            **base,
            "pass": passed,
            "reason": "geometry_compatible" if passed else "distance_out_of_range",
            "chain": chain,
            "distance_angstrom": round(distance, 3),
            "ideal_geometry": ideal_min <= distance <= ideal_max,
        }
    except (OSError, UnicodeError, ValueError, KeyError) as exc:
        return {
            **base,
            "reason": "pdb_parse_failed",
            "detail": str(exc),
        }


# ============================================================
# 共享工具
# ============================================================

def _parse_hotspot_residues(hotspots_str):
    """Parse a comma-separated hotspot string (e.g. ``"54,93,96"``) into a
    list of int residue numbers.  Returns ``None`` when the string is empty.
    """
    if not hotspots_str or not str(hotspots_str).strip():
        return None
    tokens = [r.strip() for r in str(hotspots_str).split(",") if r.strip()]
    for token in tokens:
        if not token.isdigit() or int(token) < 1:  # P3-2: reject 0
            raise ValueError(
                f"hotspot residue must be a positive integer (>=1), got {token!r}"
            )
    return [int(token) for token in tokens]


def _pdb_residue_range(pdb_path, chain="A", hotspot_residues=None):
    """Return (first, last) residue numbers for the chain segment that should
    be used as the receptor contig window.

    A gap > 50 residue numbers splits the chain into segments; by default the
    **longest** segment wins (this ignores crystallographic outliers such as
    ILE 500 in 3DAB).

    When *hotspot_residues* (iterable of int residue numbers) is provided, the
    function **validates that every hotspot falls inside a single contiguous
    segment** and returns that segment — even if it is shorter than another.
    If hotspots span multiple segments or lie outside all segments it raises
    ``ValueError``, preventing a contig that silently excludes approved
    binding-site residues.
    """
    # Fixed-column PDB parsing (columns 22-26 = residue number, col 22 = chain ID).
    # Assumes standard RCSB PDB format; mmCIF or non-standard files need preprocessing.
    residues = set()
    try:
        with open(pdb_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("ATOM") and len(line) >= 22 and line[21] == chain:
                    r = int(line[22:26].strip())
                    # P2-3: detect insertion codes.  MDM2/MDMX structures do
                    # not use them, but a generic pipeline should at least warn
                    # rather than silently merging residues 100 and 100A.
                    ins = line[26] if len(line) > 26 else " "
                    if ins != " ":
                        EvidenceLogger.log("design", "pdb_insertion_code_detected",
                            {"pdb": str(pdb_path), "chain": chain,
                             "residue": r, "insertion": ins,
                             "note": "insertion codes are not resolved in "
                                     "segment/hotspot logic; residues like "
                                     "100 and 100A collapse to the same number"})
                    residues.add(r)
    except (OSError, UnicodeError, ValueError) as e:
        EvidenceLogger.error("design", "pdb_parse_failed",
            f"Cannot parse approved coordinate artifact {pdb_path} chain {chain}: {e}.",
            recovery="verify target PDB path")
        raise ValueError(f"cannot parse target PDB chain {chain}: {pdb_path}") from e
    if not residues:
        EvidenceLogger.error("design", "pdb_empty_chain",
            f"No atoms found in approved coordinate artifact {pdb_path} chain {chain}.")
        raise ValueError(f"target PDB contains no atoms for chain {chain}: {pdb_path}")

    sorted_res = sorted(residues)
    # Split into contiguous segments (gap > 50 = new segment)
    segments = []
    seg_start = sorted_res[0]
    prev = seg_start
    for r in sorted_res[1:]:
        if r - prev > 50:
            segments.append((seg_start, prev))
            seg_start = r
        prev = r
    segments.append((seg_start, prev))

    if hotspot_residues:
        hotspot_set = {int(r) for r in hotspot_residues}
        present = hotspot_set & residues
        absent = sorted(hotspot_set - present)
        if absent:
            EvidenceLogger.error("design", "hotspot_absent_from_pdb",
                f"hotspots {absent} absent from chain {chain} of {pdb_path}; "
                f"present={sorted(present)}, pdb_residues={sorted(residues)}",
                recovery="verify structure_resolution approved the correct PDB")
            raise ValueError(
                f"Approved binding-site residues {absent} are absent from "
                f"the approved coordinate artifact {pdb_path} chain {chain}. "
                f"Verify that structure_resolution approved the correct PDB."
            )
        # Which segments cover at least one hotspot?
        covering = []
        for s_start, s_end in segments:
            covered = {r for r in present if s_start <= r <= s_end}
            if covered:
                covering.append((s_start, s_end, covered))
        if not covering:
            EvidenceLogger.error("design", "hotspot_no_contiguous_segment",
                f"chain {chain} segments {segments} contain none of the "
                f"hotspots {sorted(present)} in {pdb_path}",
                recovery="verify PDB chain assignment and hotspot residue numbering")
            raise ValueError(
                f"No contiguous segment of chain {chain} contains any "
                f"binding-site residue {sorted(present)}. "
                f"PDB segments: {segments}"
            )
        if len(covering) > 1:
            EvidenceLogger.error("design", "hotspot_multi_segment",
                f"hotspots span {len(covering)} segments of chain {chain}: "
                f"{[(c[0], c[1], sorted(c[2])) for c in covering]} in {pdb_path}",
                recovery="narrow hotspot range to a single contiguous segment, "
                         "or approve a PDB with co-located binding residues")
            raise ValueError(
                f"Binding-site residues span multiple segments of chain "
                f"{chain}: {[(c[0], c[1], sorted(c[2])) for c in covering]}. "
                f"Cannot build a single contig covering all hotspots."
            )
        # All hotspots in one segment — use it even if shorter than another
        best = (covering[0][0], covering[0][1])
    else:
        # No hotspot guidance → longest segment (backward compatible)
        best = max(segments, key=lambda s: s[1] - s[0])

    return best[0], best[1]


def _pdb_chain_residue_layout(pdb_path):
    """Return first-model PDB residues grouped in emitted chain order."""
    layout, seen = {}, {}
    model_seen = False
    with open(pdb_path) as handle:
        for line in handle:
            if line.startswith("MODEL "):
                if model_seen:
                    break
                model_seen = True
                continue
            if line.startswith("ENDMDL"):
                break
            if not line.startswith("ATOM"):
                continue
            altloc = line[16:17].strip()
            if altloc not in {"", "A"}:
                continue
            chain = line[21:22].strip() or "_"
            residue_number = line[22:26].strip()
            insertion_code = line[26].strip()
            if not chain or chain == "_" or not residue_number:
                raise ValueError(f"blank chain/residue identifier in {pdb_path}")
            residue_id = (residue_number, insertion_code)
            layout.setdefault(chain, [])
            seen.setdefault(chain, set())
            if residue_id not in seen[chain]:
                seen[chain].add(residue_id)
                layout[chain].append(residue_id)
    if not layout:
        raise ValueError(f"no ATOM residues found in {pdb_path}")
    return layout


def _pdb_chain_sequences(pdb_path):
    """Return first-model canonical sequences in emitted PDB chain order."""
    amino_acids = {
        "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
        "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
        "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
        "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    }
    sequences, seen = {}, {}
    model_seen = False
    with open(pdb_path) as handle:
        for line in handle:
            if line.startswith("MODEL "):
                if model_seen:
                    break
                model_seen = True
                continue
            if line.startswith("ENDMDL"):
                break
            if not line.startswith("ATOM"):
                continue
            altloc = line[16:17].strip()
            if altloc not in {"", "A"}:
                continue
            chain = line[21:22].strip() or "_"
            residue_number = line[22:26].strip()
            insertion_code = line[26].strip()
            residue_name = line[17:20].strip().upper()
            if not chain or chain == "_" or not residue_number:
                raise ValueError(f"blank chain/residue identifier in {pdb_path}")
            residue_id = (residue_number, insertion_code)
            sequences.setdefault(chain, [])
            seen.setdefault(chain, set())
            if residue_id in seen[chain]:
                continue
            seen[chain].add(residue_id)
            if residue_name not in amino_acids:
                raise ValueError(
                    f"non-canonical residue {residue_name!r} in chain {chain} "
                    f"at {residue_number}{insertion_code}"
                )
            sequences[chain].append(amino_acids[residue_name])
    if not sequences:
        raise ValueError(f"no ATOM residues found in {pdb_path}")
    return {chain: "".join(values) for chain, values in sequences.items()}


def _verify_fixed_sequence_pdb(pdb_path, requested_sequence):
    """Require one monomer chain whose saved PDB sequence is exactly requested."""
    requested_sequence = str(requested_sequence or "").strip().upper()
    if not _validate_sequence(requested_sequence):
        raise ValueError("requested fixed sequence is invalid")
    observed = _pdb_chain_sequences(pdb_path)
    if len(observed) != 1 or list(observed.values()) != [requested_sequence]:
        raise ValueError(
            f"fixed-sequence PDB mismatch: requested={requested_sequence!r} "
            f"observed={observed!r}"
        )
    return observed


def _infer_binder_chain(pdb_path, expected_length, receptor_chain=None):
    """Return the RFdiffusion-generated binder chain.

    The RFdiffusion output must contain at least two chains (receptor + binder).
    When *receptor_chain* is provided it must be present in the PDB and is
    unconditionally excluded before length matching.
    """
    layout = _pdb_chain_residue_layout(pdb_path)
    if len(layout) < 2:
        raise ValueError(
            f"RFdiffusion complex must contain at least two chains, got "
            f"{sorted(layout)}"
        )
    if receptor_chain:
        if receptor_chain not in layout:
            raise ValueError(
                f"expected receptor chain {receptor_chain!r} not found in "
                f"RFdiffusion output; chains={sorted(layout)}"
            )
        candidate_chains = set(layout) - {receptor_chain}
    else:
        candidate_chains = set(layout)
    candidates = [
        chain for chain in candidate_chains
        if len(layout[chain]) == int(expected_length)
    ]
    if len(candidates) != 1:
        counts = {chain: len(layout[chain]) for chain in layout}
        detail = f"candidates={candidates}, lengths={counts}"
        if receptor_chain:
            detail += f", receptor={receptor_chain!r}"
        raise ValueError(
            f"expected one {expected_length}-residue binder chain; {detail}"
        )
    return candidates[0]


def _parse_binder_residues(pdb_path, binder_chain):
    """Return ``[(chain, residue_id), ...]`` for one validated binder chain."""
    layout = _pdb_chain_residue_layout(pdb_path)
    if binder_chain not in layout:
        raise ValueError(f"binder chain {binder_chain!r} is absent from {pdb_path}")
    return [
        (binder_chain, f"{number}{insertion}")
        for number, insertion in layout[binder_chain]
    ]


def _extract_ligandmpnn_binder_sequence(
        encoded, binder_chain, layout, input_sequences=None):
    """Extract the binder segment and verify that every receptor chain stayed fixed."""
    # LigandMPNN's parse_PDB() builds ``mask_c`` from a sorted chain list and
    # writes FASTA segments in that order, even when the PDB records first
    # encounter the chains in a different order.
    chain_order = sorted(layout)
    if binder_chain not in layout:
        raise ValueError(f"binder chain {binder_chain!r} is absent from PDB layout")
    segments = str(encoded).strip().upper().split(":")
    if len(segments) != len(chain_order):
        raise ValueError(
            f"FASTA has {len(segments)} chain segments, PDB has "
            f"{len(chain_order)} chains"
        )
    if input_sequences is not None:
        for chain, segment in zip(chain_order, segments):
            if chain == binder_chain:
                continue
            expected = input_sequences.get(chain)
            if expected is None or segment != expected:
                raise ValueError(
                    f"fixed chain {chain} changed during inverse folding"
                )
    sequence = segments[chain_order.index(binder_chain)]
    expected_length = len(layout[binder_chain])
    if len(sequence) != expected_length:
        raise ValueError(
            f"binder FASTA length {len(sequence)} != PDB length {expected_length}"
        )
    if not sequence or any(amino_acid not in SCAFFOLD_MUTABLE_AA for amino_acid in sequence):
        raise ValueError("binder FASTA contains non-standard amino acids")
    return sequence


def _hotspot_positions(template_seq):
    """在模板序列中检测 F/W/L hotspot 位置，返回 {0-based_position: aa}"""
    hotspots = {}
    for i, aa in enumerate(template_seq):
        if aa in "FWL":
            hotspots[i] = aa
    return hotspots


def _hotspot_fixed_residues(hotspots, binder_residues):
    """将模板 hotspot 位置映射为 LigandMPNN fixed_residues 字符串。
    固定 F/W 锚点，L 位置留给 LigandMPNN 自由设计（L26 偏置）。
    hotspots: _hotspot_positions() 返回的 {pos: aa} dict
    """
    fixed = []
    for i, aa in hotspots.items():
        if aa in "FW":
            if i < len(binder_residues):
                # W23/F19 锚点：固定不变
                ch, resi = binder_residues[i]
                fixed.append(f"{ch}{resi}")
            else:
                EvidenceLogger.error("design", "hotspot_anchor_out_of_range", {
                    "position": i,
                    "residue": aa,
                    "binder_length": len(binder_residues),
                    "remediation": "template F/W anchor position exceeds generated "
                                    "binder length; verify template-to-backbone alignment",
                })
        # L 残基不固定，让 backbone 几何自然偏置小氨基酸
    return " ".join(fixed)


def _validate_sequence(seq):
    if not isinstance(seq, str):
        return False
    valid = set("ACDEFGHIKLMNPQRSTVWY")
    s = seq.upper().replace("-","").replace("*","")
    return 8 <= len(s) <= 20 and all(c in valid for c in s)


def _next_candidate_id():
    """Return the next C**** candidate ID (thread-safe within a single process).

    Assumes single-process execution.  Multi-process orchestration must use an
    external lock (fcntl.flock / Redis / DB sequence) to avoid ID collisions.
    """
    with _LOCK:
        s = State.load()
        existing_max = 0
        for row in CandidateIndex.load():
            candidate_id = str(row.get("candidate_id") or "").strip()
            if re.fullmatch(r"C\d{4,}", candidate_id):
                existing_max = max(existing_max, int(candidate_id[1:]))
        s["candidate_count"] = max(int(s.get("candidate_count", 0)), existing_max) + 1
        State.save(s)
        return f"C{s['candidate_count']:04d}"


def _describe_cyclize(n_term, c_term, linker):
    parts = []
    if n_term == "C" and c_term == "C":
        parts.append("Cys-Cys_disulfide")
    elif n_term == "" and c_term == "":
        parts.append("head-to-tail_amide")
    else:
        raise ValueError(
            f"unrecognised terminal residues for cyclization: "
            f"N-term={n_term!r} C-term={c_term!r}; expected Cys-Cys or head-to-tail"
        )
    if linker:
        parts.append(f"linker={linker}")
    return ",".join(parts)


def _load_target_spec():
    """
    从 State 读取 Research 产出的设计规则。
    若 Research 未运行则返回空结构（Route B/C 会报错退出）。
    """
    s = State.load()
    # 设计规则：Trp23 不变 / Phe19 ≤ Phe体积 / Leu26 换小脂肪族
    design_rules = s.get("design_rules", {}) or s.get("pocket_differences", {})
    return {
        "targets": s.get("targets", {}),
        "pocket_differences": s.get("pocket_differences", {}),
        "known_dual_binders": s.get("known_dual_binders", []),
        "design_rules": design_rules,
    }


def _merge_config(target_spec, design_config):
    """Merge run controls with the approved target and coordinate artifact.

    Target identity, chain, hotspots, and coordinate path are security-sensitive
    project inputs.  They come from the approved project config; callers may
    select a configured target but may not replace those fields ad hoc.
    """
    ts = target_spec or {}
    dc = design_config or {}
    project = ACTIVE_PROJECT_CONFIG
    assert_project_approved(project)

    default_target = project["targets"][0]["id"]
    target_ref = (
        dc.get("target_id") or ts.get("target_id") or ts.get("id")
        or dc.get("target_name") or ts.get("target_name")
        or default_target
    )
    target = assert_target_structure_ready(project, target_ref)
    structure = target.get("structure") or {}
    coordinate_value = structure.get("coordinate_path")
    if not coordinate_value:
        raise RuntimeError(
            f"approved target {target['id']} has no structure.coordinate_path; "
            "materialize and approve the coordinate artifact before Design"
        )
    coordinate_path = Path(coordinate_value).expanduser().resolve()
    if not coordinate_path.is_file():
        raise FileNotFoundError(
            f"approved coordinate artifact does not exist: {coordinate_path}"
        )

    requested_path = dc.get("target_pdb") or ts.get("target_pdb")
    if requested_path and Path(requested_path).expanduser().resolve() != coordinate_path:
        raise ValueError("target_pdb cannot override the approved coordinate_path")

    chain = structure.get("chain")
    if not chain:
        raise RuntimeError(f"approved target {target['id']} has no structure.chain")
    requested_chain = dc.get("chain") or ts.get("chain")
    if requested_chain and requested_chain != chain:
        raise ValueError("chain cannot override the approved target chain")

    binding_site = target.get("binding_site") or {}
    hotspots = ",".join(str(residue) for residue in binding_site.get("residues", []))
    if hotspots:
        try:
            _parse_hotspot_residues(hotspots)
        except ValueError as exc:
            raise ValueError(
                f"approved target {target['id']} has invalid hotspot residues: {exc}"
            ) from exc
    requested_hotspots = dc.get("hotspots") or ts.get("hotspots")
    if requested_hotspots and requested_hotspots != hotspots:
        raise ValueError("hotspots cannot override the approved binding site")

    lengths = dc.get("lengths") or ts.get("lengths") or (
        target.get("design") or {}
    ).get("lengths", [10, 12, 14])
    lengths = [int(length) for length in lengths]
    if not lengths or any(length < 8 or length > 20 for length in lengths):
        raise ValueError("cyclic peptide lengths must be between 8 and 20 residues")

    n = dc.get("n") if dc.get("n") is not None else ts.get("n", 100)
    n = int(n)
    if n < 1:
        raise ValueError("n must be at least 1")

    seed = dc.get("seed") if dc.get("seed") is not None else ts.get("seed")
    if seed is None:
        seed = int.from_bytes(os.urandom(4), "big") % (2**31)
        print(f"[Design] seed not specified — auto-generated seed={seed} "
              f"(controls LigandMPNN + Route C; RFdiffusion backbone "
              f"generation is GPU non-deterministic)")
    try:
        seed = int(seed)
    except (ValueError, TypeError) as exc:
        seed = int.from_bytes(os.urandom(4), "big") % (2**31)
        print(f"[Design] invalid seed value {exc}, falling back to "
              f"auto-generated seed={seed}")
    # Guard against fractional float silently truncated by int() (P1-2).
    # A fractional value (e.g. 42.9 → 42) nearly always indicates a caller
    # error where a score or progress fraction was mistakenly passed as seed.
    if isinstance(original := (dc.get("seed") if dc.get("seed") is not None
                                else ts.get("seed")), float):
        if original != int(original):
            EvidenceLogger.error("design", "fractional_seed_rejected",
                f"seed={original!r} has a fractional part; int() truncates to "
                f"{seed}. Pass an integer seed instead.",
                recovery="use an integer seed in [0, 2^31-1]")
            raise ValueError(
                f"seed must be an integer, got fractional float {original!r}"
            )
    if seed < 0 or seed > 2**31 - 1:
        raise ValueError(
            f"seed must be in [0, {2**31 - 1}] (int32 non-negative), got {seed}"
        )

    return {
        "project_id": project["project_id"],
        "target_id": target["id"],
        "target_name": target["id"],
        "target_pdb": str(coordinate_path),
        "target_pdb_sha256": structure.get("coordinate_sha256"),
        "pdb_id": structure.get("pdb_id"),
        "chain": chain,
        "hotspots": hotspots,
        "lengths": lengths,
        "n": n,
        "seed": seed,
    }


# ============================================================
# 兼容旧 API
# ============================================================

def design_afcyc(target=None, n=10, lengths=None, hotspots=None, chain=None, seed=None):
    import warnings
    warnings.warn("deprecated, use design_rfpeptides", DeprecationWarning)
    target_spec = {}
    if target is not None:
        target_spec["target_name"] = target
    if chain is not None:
        target_spec["chain"] = chain
    if hotspots is not None:
        target_spec["hotspots"] = hotspots
    return design_rfpeptides(
        target_spec=target_spec,
        design_config={"n": n, "lengths": lengths or [10], "seed": seed})


def design_motif_graft(n=400, seed=None):
    import warnings
    warnings.warn("deprecated, use design_motif_guided", DeprecationWarning)
    return design_motif_guided(design_config={"n": n, "seed": seed})


def design_atsp_cyclize(n=200, seed=None):
    import warnings
    warnings.warn("deprecated, use design_atsp_derived", DeprecationWarning)
    return design_atsp_derived(design_config={"n": n, "seed": seed})


# ============================================================
# 兼容旧 dual_target_score（保留但不推荐）
# ============================================================

def dual_target_score(iptm_mdm2, iptm_mdmx):
    """旧版加权组合打分（被 Pareto 前沿替代，保留兼容）"""
    import warnings
    warnings.warn("dual_target_score deprecated, use threshold_filter+pareto_front",
                  DeprecationWarning)
    combined = (iptm_mdm2 + iptm_mdmx) / 2
    asymmetry = abs(iptm_mdm2 - iptm_mdmx)
    return {
        "dual_score": round(combined - 0.5 * asymmetry, 4),
        "combined": round(combined, 4),
        "asymmetry": round(asymmetry, 4),
        "passed": iptm_mdm2 > 0.7 and iptm_mdmx > 0.55 and asymmetry < 0.25,
    }


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description=f"Design Agent v{DESIGN_PIPELINE_VERSION}"
    )
    p.add_argument("--route", choices=["A","B","C","all"], default="all")
    p.add_argument("--target", default=None,
                   help="configured target ID or PDB ID; defaults to the first approved target")
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--lengths", default="10,12,14")
    p.add_argument("--hotspots", default=None)
    p.add_argument("--chain", default=None,
                   help="must match the approved target chain when provided")
    p.add_argument("--seed", type=int, default=None)
    args = p.parse_args()

    lengths = [int(x) for x in args.lengths.split(",")]
    ts = {}
    if args.chain:
        ts["chain"] = args.chain
    if args.target:
        ts["target_name"] = args.target
    if args.hotspots:
        ts["hotspots"] = args.hotspots
    dc = {"n": args.n, "lengths": lengths, "seed": args.seed}

    all_cands = []
    if args.route in ("A","all"):
        print(f"[Route A v5] target={args.target}, n={args.n}, len={lengths}")
        result = design_rfpeptides(target_spec=ts, design_config=dc)
        all_cands.extend(result)
        print(f"[Route A] 完成: {len(result)} candidates")
    if args.route in ("B","all"):
        print(f"[Route B v5] n={args.n}")
        result = design_motif_guided(target_spec=ts, design_config=dc)
        all_cands.extend(result)
        print(f"[Route B] 完成: {len(result)} candidates")
    if args.route in ("C","all"):
        print(f"[Route C v5] n={args.n}")
        result = design_atsp_derived(target_spec=ts, design_config=dc)
        all_cands.extend(result)
        print(f"[Route C] 完成: {len(result)} candidates")

    print(f"\nDone: {len(all_cands)} candidates")
    print(CandidateIndex.stats())
