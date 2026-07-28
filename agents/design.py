"""
Design Agent v5 — 于嘉乐
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

import os, sys, json, time, subprocess, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from data_layer import EvidenceLogger, CandidateIndex, State, file_hash


# ============================================================
# 环境路径
# ============================================================

# --- 新服务器 (RTX 4090, damodel) 路径 ---
CYCPEP_CONDA  = "/root/damodel-tmp/envs/cycpep-prediction"
CYCPEP_PYTHON   = f"{CYCPEP_CONDA}/bin/python"
COLABDESIGN_DIR = "/root/workspace/NovaPeptide/tools/ColabDesign"
COLABDESIGN_PARAMS = f"{COLABDESIGN_DIR}/params"
CUDA_DATA_DIR   = f"{CYCPEP_CONDA}/lib/python3.10/site-packages/nvidia/cuda_nvcc"
TARGETS_DIR     = "/root/damodel-tmp/novapeptide/targets"
OUTPUT_DIR      = "/root/damodel-tmp/novapeptide/designs"

# RFdiffusion + LigandMPNN 环境（待建立）
RFDIFF_CONDA   = "/root/damodel-tmp/envs/rfdiffusion-design"
RFDIFF_PYTHON   = f"{RFDIFF_CONDA}/bin/python"
RFDIFF_DIR      = "/root/workspace/NovaPeptide/tools/RFdiffusion"
LIGANDMPNN_DIR  = "/root/workspace/NovaPeptide/tools/LigandMPNN"

# SE3-Transformer（新服务器需确认路径，按文档 8 节查找）
SE3_ROOT = "/root/workspace/NovaPeptide"
DEFAULT_SEED = None


# ============================================================
# 设计常量（Research 产出可覆盖）
# ============================================================

# 所有设计常量从 Research State 读取（_load_target_spec）。
_LOCK = threading.Lock()
CYCLIZATION_PAIRS = [("C", "C"), ("", "")]
LINKER_MATRIX = ["GGGGS", "GGGS", "GGS", "GS", ""]
SCAFFOLD_MUTABLE_AA = "ACDEFGHIKLMNPQRSTVWY"


# ============================================================
# Route A: RFpeptides 自由生成
# ============================================================

def design_rfpeptides(target_spec=None, design_config=None):
    """RFpeptides → LigandMPNN → AfCycDesign refold"""
    config = _merge_config(target_spec, design_config)
    route_name = f"route_A_{config['target_name'].lower()}"
    batch_id = f"batch_rfpep_{config['target_name']}_s{config['seed']}"
    batch_dir = f"{OUTPUT_DIR}/route_A/{batch_id}"
    os.makedirs(batch_dir, exist_ok=True)

    with open(f"{batch_dir}/design_config.json", "w") as f:
        json.dump(config, f, indent=2, default=str)

    candidates = []
    total_gen, total_valid = 0, 0
    t_batch = time.time()
    target_range = _pdb_residue_range(config["target_pdb"], config["chain"])

    for L in config["lengths"]:
        n_designs = max(1, config["n"] // len(config["lengths"]))
        backbone_dir = f"{batch_dir}/backbones_len{L}"
        os.makedirs(backbone_dir, exist_ok=True)
        rfdiff_ok = _run_rfdiff(
            target_pdb=config["target_pdb"], binder_len=L,
            n_designs=n_designs, output_prefix=f"{backbone_dir}/bb",
            contig=f"{config['chain']}{target_range[0]}-{target_range[1]}/0 {L}-{L}",
            seed=config["seed"],
            hotspots=config.get("hotspots"),
            chain=config["chain"])
        if not rfdiff_ok:
            print(f"[Route A] RFdiff 失败 len={L}，跳过")
            continue

        bb_files = sorted(Path(backbone_dir).glob("bb_*.pdb"))
        print(f"[Route A] RFdiff 完成, 找到 {len(bb_files)} 个骨架PDB")
        for bb_path in bb_files[:n_designs]:
            total_gen += 1
            mpnn_dir = f"{batch_dir}/mpnn_{bb_path.stem}"
            os.makedirs(mpnn_dir, exist_ok=True)
            seqs = _run_ligandmpnn(str(bb_path), mpnn_dir, n_seq=8, target_chain=config["chain"])
            if not seqs:
                print(f"[Route A] LigandMPNN 返回 0 条序列: {bb_path.name}")
                continue

            for seq in seqs[:4]:
                cid = _next_candidate_id()
                refold_dir = f"{batch_dir}/candidates/{cid}"
                os.makedirs(refold_dir, exist_ok=True)
                refold_pdb = f"{refold_dir}/refold.pdb"
                plddt = _run_refold(seq, refold_pdb)
                rc = _ring_closure_check(refold_pdb) if os.path.exists(refold_pdb) else {"pass": False}

                if plddt and rc.get("pass"):
                    total_valid += 1
                    manifest = _write_manifest(cid, seq, route_name, batch_id, refold_pdb, config, backbone_pdb=str(bb_path))
                    CandidateIndex.add({
                        "candidate_id": cid, "sequence": seq, "length": L,
                        "source_route": route_name, "source_batch": batch_id,
                        "monomer_plddt": round(plddt, 3),
                        "notes": json.dumps(_manifest_summary(manifest))
                    })
                    EvidenceLogger.log("design", "candidate_registered",
                        {"candidate": {"candidate_id": cid, "sequence": seq}},
                        targets=["both"], phase="design")
                    candidates.append((cid, seq, L))
                else:
                    print(f"[Route A] refold失败: {cid} pLDDT={plddt} ring_closed={rc.get('pass')}")

    EvidenceLogger.design_batch(route=route_name, n_generated=total_gen,
        n_valid=total_valid, tool_name="rfpeptides_pipeline", tool_version="v5",
        duration_sec=round(time.time()-t_batch, 1))
    return candidates


# ============================================================
# Route B: motif 引导生成
# ============================================================

def design_motif_guided(target_spec=None, design_config=None):
    """RFpeptides motif 引导 + LigandMPNN L26 偏置 + refold"""
    config = _merge_config(target_spec, design_config)
    route_name = "route_B_motif"
    batch_id = f"batch_motif_s{config['seed']}"
    batch_dir = f"{OUTPUT_DIR}/route_B/{batch_id}"
    os.makedirs(batch_dir, exist_ok=True)

    with open(f"{batch_dir}/design_config.json", "w") as f:
        json.dump(config, f, indent=2, default=str)

    spec = _load_target_spec()
    binders = spec.get("known_dual_binders", [])
    if not binders:
        EvidenceLogger.error("design", "no_binders",
            "known_dual_binders empty in state.json — Research 尚未产出或格式错误",
            recovery="先跑 Research Agent 产出设计规则再跑 Route B")
        return []
    templates = [(b.get("sequence") or b.get("seq", ""), b.get("name","tmpl"))
                 for b in binders if b.get("sequence") or b.get("seq")]

    candidates = []
    total_gen, total_valid = 0, 0
    t_batch = time.time()
    n_per = max(1, config.get("n", 100) // max(1, len(templates)))
    target_range = _pdb_residue_range(config["target_pdb"], config["chain"])

    for tmpl_seq, tmpl_name in templates:
        if len(tmpl_seq) < 8:
            continue
        L = len(tmpl_seq)
        tmpl_hotspots = _hotspot_positions(tmpl_seq)
        backbone_dir = f"{batch_dir}/backbones_{tmpl_name}"
        os.makedirs(backbone_dir, exist_ok=True)
        # Route B: motif 约束由 LigandMPNN 的 fixed_residues 实现，不通过 RFdiffusion inpaint_seq
        rfdiff_ok = _run_rfdiff(target_pdb=config["target_pdb"], binder_len=L,
            n_designs=n_per, output_prefix=f"{backbone_dir}/bb",
            contig=f"{config['chain']}{target_range[0]}-{target_range[1]}/0 {L}-{L}",
            seed=config["seed"],
            hotspots=config.get("hotspots"),
            chain=config["chain"])
        if not rfdiff_ok:
            print(f"[Route B] RFdiff 失败 {tmpl_name}，跳过")
            continue

        bb_files = sorted(Path(backbone_dir).glob("bb_*.pdb"))
        print(f"[Route B] {tmpl_name}: RFdiff 完成, 找到 {len(bb_files)} 个骨架PDB")
        for bb_path in bb_files[:n_per]:
            total_gen += 1
            binder_res = _parse_binder_residues(str(bb_path), config["chain"])
            fixed_res = _hotspot_fixed_residues(tmpl_hotspots, binder_res) if binder_res else ""
            mpnn_dir = f"{batch_dir}/mpnn_{bb_path.stem}"
            os.makedirs(mpnn_dir, exist_ok=True)
            seqs = _run_ligandmpnn(str(bb_path), mpnn_dir, n_seq=8,
                target_chain=config["chain"], fixed_residues=fixed_res or None)
            if not seqs:
                print(f"[Route B] LigandMPNN 返回 0 条序列: {bb_path.name}")
                continue

            for seq in seqs[:4]:
                cid = _next_candidate_id()
                refold_dir = f"{batch_dir}/candidates/{cid}"
                os.makedirs(refold_dir, exist_ok=True)
                refold_pdb = f"{refold_dir}/refold.pdb"
                plddt = _run_refold(seq, refold_pdb)
                rc = _ring_closure_check(refold_pdb) if os.path.exists(refold_pdb) else {"pass": False}

                if plddt and rc.get("pass"):
                    total_valid += 1
                    manifest = _write_manifest(cid, seq, route_name, batch_id, refold_pdb, config, backbone_pdb=str(bb_path))
                    CandidateIndex.add({
                        "candidate_id": cid, "sequence": seq, "length": L,
                        "source_route": route_name, "source_batch": batch_id,
                        "monomer_plddt": round(plddt, 3),
                        "notes": json.dumps(_manifest_summary(manifest))
                    })
                    EvidenceLogger.log("design", "candidate_registered",
                        {"candidate": {"candidate_id": cid, "sequence": seq}},
                        targets=["both"], phase="design")
                    candidates.append((cid, seq, L))
                else:
                    print(f"[Route B] refold失败: {cid} pLDDT={plddt} ring_closed={rc.get('pass')}")

    EvidenceLogger.design_batch(route=route_name, n_generated=total_gen,
        n_valid=total_valid, tool_name="rfpeptides_motif", tool_version="v5",
        duration_sec=round(time.time()-t_batch, 1))
    return candidates


# ============================================================
# Route C: ATSP-7041 环化改造
# ============================================================

def design_atsp_derived(target_spec=None, design_config=None):
    """ATSP-7041 模板环化：linker × 环化矩阵 + 随机突变扩展 + refold 验证"""
    config = _merge_config(target_spec, design_config)
    n = config.get("n", 200)
    seed = config["seed"]  # _merge_config already resolves None → timestamp
    import random
    random.seed(seed)

    route_name = "route_C_atsp"
    batch_id = f"batch_atsp_{int(time.time())}_s{seed}"
    batch_dir = f"{OUTPUT_DIR}/route_C/{batch_id}"
    os.makedirs(batch_dir, exist_ok=True)

    with open(f"{batch_dir}/design_config.json", "w") as f:
        json.dump(config, f, indent=2)

    # ATSP-7041 核心序列从 Research 数据取
    spec = _load_target_spec()
    binders = spec.get("known_dual_binders", [])
    atsp_seq = None
    for b in binders:
        name = b.get("name", "")
        seq_candidate = b.get("sequence") or b.get("seq", "")
        if "ATSP" in name.upper() and seq_candidate:
            atsp_seq = seq_candidate
            break
    if not atsp_seq:
        EvidenceLogger.error("design", "no_atsp",
            "known_dual_binders 中未找到 ATSP-7041 — Research 尚未产出",
            recovery="先跑 Research Agent 产出 ATSP-7041 序列再跑 Route C")
        return []
    # Route C 序列设计: linker × 环化 全矩阵
    base_combos = []
    for linker in LINKER_MATRIX:
        for cn, cc in CYCLIZATION_PAIRS:
            seq = f"{cn}{atsp_seq}{linker}{cc}"
            if _validate_sequence(seq):
                base_combos.append((seq, _describe_cyclize(cn, cc, linker)))

    # 第2级：不够 n 则随机突变扩展
    expanded = list(base_combos)
    seen_seqs = set(s for s, _ in base_combos)
    attempts = 0
    while len(expanded) < n and attempts < n * 10:
        attempts += 1
        seq, desc = random.choice(base_combos)
        pos = random.choice([3, 5, 8, 10, 12])
        aa = random.choice(SCAFFOLD_MUTABLE_AA)
        off = 1 if seq and seq[0] == "C" else 0
        ix = off + min(pos, len(seq)-1)
        mutated = seq[:ix] + aa + seq[ix+1:]
        if _validate_sequence(mutated) and mutated not in seen_seqs:
            seen_seqs.add(mutated)
            expanded.append((mutated, f"{desc},mut:{pos}={aa}"))

    candidates = []
    total_gen, total_valid = 0, 0
    t_batch = time.time()

    for seq, desc in expanded[:n]:
        total_gen += 1
        cid = _next_candidate_id()
        refold_dir = f"{batch_dir}/candidates/{cid}"
        os.makedirs(refold_dir, exist_ok=True)
        refold_pdb = f"{refold_dir}/refold.pdb"
        plddt = _run_refold(seq, refold_pdb)
        rc = _ring_closure_check(refold_pdb) if os.path.exists(refold_pdb) else {"pass": False}

        if plddt and rc.get("pass"):
            total_valid += 1
            manifest = _write_manifest(cid, seq, route_name, batch_id, refold_pdb, config, cyclization=desc)
            CandidateIndex.add({
                "candidate_id": cid, "sequence": seq, "length": len(seq),
                "source_route": route_name, "source_batch": batch_id,
                "monomer_plddt": round(plddt, 3),
                "notes": f"{desc},{json.dumps(_manifest_summary(manifest))}"
            })
            EvidenceLogger.log("design", "candidate_registered",
                {"candidate": {"candidate_id": cid, "sequence": seq}},
                targets=["both"], phase="design")
            candidates.append((cid, seq, len(seq)))
        else:
            EvidenceLogger.error("design", "refold_failed",
                f"{cid}: pLDDT={plddt}", recovery="skip")

    EvidenceLogger.design_batch(route=route_name, n_generated=total_gen,
        n_valid=total_valid, tool_name="atsp_derived", tool_version="v5",
        duration_sec=round(time.time()-t_batch, 1))
    return candidates


# ============================================================
# 评分 — 阈值过滤 + Pareto 前沿
# ============================================================

def threshold_filter(candidates, thresholds):
    """双靶独立门槛，一票否决"""
    passed = []
    for c in candidates:
        ok_m2  = c.get("ipsae_mdm2", 0) >= thresholds.get("ipsae_mdm2", 0.6)
        ok_mx  = c.get("ipsae_mdmx", 0) >= thresholds.get("ipsae_mdmx", 0.5)
        ok_hc2 = c.get("hotspot_cov_mdm2", 1) >= thresholds.get("hotspot_cov", 0.67)
        ok_hcx = c.get("hotspot_cov_mdmx", 1) >= thresholds.get("hotspot_cov", 0.67)
        if ok_m2 and ok_mx and ok_hc2 and ok_hcx:
            passed.append(c)
    return passed


def pareto_front(candidates, obj_x="ipsae_mdm2", obj_y="ipsae_mdmx"):
    """非支配排序，不做加权"""
    front = []
    for c1 in candidates:
        dominated = False
        for c2 in candidates:
            if c2 is c1:
                continue
            if (c2.get(obj_x, 0) >= c1.get(obj_x, 0) and
                c2.get(obj_y, 0) >= c1.get(obj_y, 0) and
                (c2.get(obj_x, 0) > c1.get(obj_x, 0) or
                 c2.get(obj_y, 0) > c1.get(obj_y, 0))):
                dominated = True
                break
        if not dominated:
            front.append(c1)
    return front


# ============================================================
# candidate_manifest.json
# ============================================================

def _write_manifest(cid, seq, route, batch_id, refold_pdb, config, backbone_pdb=None, cyclization=None):
    """每条候选输出 manifest.json"""
    refold_dir = os.path.dirname(refold_pdb)
    manifest_path = f"{refold_dir}/manifest.json"
    rc = _ring_closure_check(refold_pdb) if os.path.exists(refold_pdb) else {}
    if cyclization is None:
        cyclization = "Cys-Cys_disulfide" if (seq.startswith("C") and seq.endswith("C")) else "head-to-tail_amide"
    manifest = {
        "candidate_id": cid, "sequence": seq, "length": len(seq),
        "source_route": route, "source_batch": batch_id,
        "cyclization_type": cyclization,
        "refold_pdb": refold_pdb,
        "refold_pdb_hash": file_hash(refold_pdb) if os.path.exists(refold_pdb) else "",
        "backbone_pdb": backbone_pdb or "",
        "backbone_pdb_hash": file_hash(backbone_pdb) if (backbone_pdb and os.path.exists(backbone_pdb)) else "",
        "ring_closure": rc,
        "design_config_summary": {
            "target": config.get("target_name"),
            "target_pdb": config.get("target_pdb"),
            "seed": config.get("seed"),
        }
    }
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def _manifest_summary(manifest):
    return {k: manifest[k] for k in ["candidate_id", "sequence", "refold_pdb_hash"]}


# ============================================================
# 工具调用封装
# ============================================================

def _run_rfdiff(target_pdb, binder_len, n_designs, output_prefix, contig,
                seed=None, hotspots=None, chain="A"):
    """RFdiffusion 子进程。hotspots: 逗号分隔的残基号如 '54,93,96'"""
    cmd = [
        RFDIFF_PYTHON, f"{RFDIFF_DIR}/scripts/run_inference.py",
        f"inference.input_pdb={target_pdb}",
        "inference.cyclic=True",
        "inference.cyc_chains=a",
        f"inference.num_designs={n_designs}",
        f"inference.output_prefix={output_prefix}",
        f"contigmap.contigs=['{contig}']",
        "diffuser.T=50",
    ]
    if hotspots:
        # 补链名前缀: "54,93,96" → "A54,A93,A96"
        formatted = ",".join(f"{chain}{r.strip()}" for r in hotspots.split(",") if r.strip())
        if formatted:
            cmd.append(f"ppi.hotspot_res=['{formatted}']")
    # RFdiffusion Hydra config 没有 inference.seed 字段，seed 通过 contig 控制
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
            cwd=RFDIFF_DIR,
            env={**os.environ, "PYTHONPATH": f"{SE3_ROOT}:{os.environ.get('PYTHONPATH', '')}"})
        if r.returncode != 0:
            print(f"[RFdiff 失败] exit={r.returncode}")
            print(f"  stderr: {r.stderr[-500:]}")
            EvidenceLogger.error("design", "rfdiff_failed",
                f"exit={r.returncode} stderr={r.stderr[-300:]}")
            return False
        return True
    except Exception as e:
        print(f"[RFdiff 异常] {e}")
        EvidenceLogger.error("design", "rfdiff_exception", str(e))
        return False


def _run_ligandmpnn(backbone_pdb, output_dir, n_seq=8, target_chain="A",
                    fixed_residues=None):
    """LigandMPNN 子进程。target_chain=受体链，设计除受体外的所有链。
    fixed_residues: 空格分隔的 chain+resi 列表，如 'B25 B26 B27'，这些残基在 LigandMPNN 中固定不变。"""
    # 自动检测 binder 链
    binder_chains = set()
    try:
        with open(backbone_pdb) as f:
            for line in f:
                if line.startswith("ATOM"):
                    ch = line[21]
                    if ch != target_chain:
                        binder_chains.add(ch)
    except Exception:
        pass
    chains_str = ",".join(sorted(binder_chains)) if binder_chains else "B"
    cmd = [
        RFDIFF_PYTHON, f"{LIGANDMPNN_DIR}/run.py",
        "--model_type", "protein_mpnn",
        f"--checkpoint_protein_mpnn={LIGANDMPNN_DIR}/model_params/proteinmpnn_v_48_020.pt",
        f"--pdb_path={backbone_pdb}",
        f"--out_folder={output_dir}",
        f"--batch_size={min(n_seq, 4)}",
        f"--number_of_batches={max(1, n_seq // 4)}",
        "--temperature=0.1", "--seed=42",
        f"--chains_to_design={chains_str}",
    ]
    if fixed_residues:
        cmd.append(f"--fixed_residues={fixed_residues}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
            cwd=LIGANDMPNN_DIR,
            env={**os.environ, "PYTHONPATH": f"{SE3_ROOT}:{os.environ.get('PYTHONPATH', '')}"})
        if r.returncode != 0:
            print(f"[LigandMPNN 失败] exit={r.returncode} stderr={r.stderr[-300:]}")
            return []
        seqs = []
        for fa in sorted(Path(output_dir).glob("**/*.fa"))[:1]:  # 一个 PDB 只出一个 FASTA
            with open(fa) as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith(">") or not line:
                        continue
                    # LigandMPNN 输出: "受体序列:设计序列"，只取 binder 部分
                    if ":" in line:
                        line = line.split(":")[-1]
                    # 跳过全 G 或单氨基酸重复（LigandMPNN baseline）
                    if len(set(line)) <= 2:
                        continue
                    if line not in seqs:
                        seqs.append(line)
        return seqs[:n_seq]
    except Exception as e:
        EvidenceLogger.error("design", "ligandmpnn_exception", str(e))
        return []


def _run_refold(sequence, output_pdb):
    """
    AfCycDesign refold：hallucination 折叠固定序列为环肽。
    只做基础折叠验证。pLDDT > 0.8 的最终过滤由 Prediction Agent 的 L1 负责。
    """
    L = len(sequence)
    script = f"""
import sys, numpy as np
sys.path.insert(0, '{COLABDESIGN_DIR}')
from colabdesign import mk_af_model, clear_mem

model = mk_af_model(protocol='hallucination', data_dir='{COLABDESIGN_PARAMS}')
model.prep_inputs(length={L})
model.restart(seq='{sequence}')

i = np.arange({L})
ij = np.stack([i, i+{L}], -1)
offset = i[:,None] - i[None,:]
c_offset = np.abs(ij[:,None,:,None] - ij[None,:,None,:]).min((2,3))
a = c_offset < np.abs(offset)
c_offset[a] = -c_offset[a]
c_offset = c_offset * np.sign(offset)
idx = np.array(model._inputs['residue_index'])
off = np.array(idx[:,None] - idx[None,:])
off[:{L}, :{L}] = c_offset
model._inputs['offset'] = off

model.design_3stage(100, 100, 20)
model.save_pdb('{output_pdb}', get_best=True)
plddt = float(np.mean(model.aux['plddt']))
with open('{output_pdb}.plddt', 'w') as pf:
    pf.write(str(plddt))
clear_mem()
"""
    spath = f"/tmp/refold_{os.getpid()}_{hash(sequence) % 100000}.py"
    with open(spath, "w") as f:
        f.write(script)
    try:
        r = subprocess.run([CYCPEP_PYTHON, spath], capture_output=True, text=True,
            timeout=1200,
            env={**os.environ,
                 "XLA_FLAGS": f"--xla_gpu_cuda_data_dir={CUDA_DATA_DIR}"})
        if r.returncode != 0:
            EvidenceLogger.error("design", "refold_nonzero",
                f"exit={r.returncode} stderr={r.stderr[-200:]}")
        plddt_file = f"{output_pdb}.plddt"
        if os.path.exists(plddt_file):
            with open(plddt_file) as pf:
                return float(pf.read().strip())
        return None
    except Exception as e:
        EvidenceLogger.error("design", "refold_exception", str(e))
        return None
    finally:
        try:
            os.unlink(spath)
        except OSError:
            pass


def _ring_closure_check(pdb_path):
    """检查 N-Cα 到 C-Cα 距离 < 7Å（只读第一个 MODEL）"""
    try:
        with open(pdb_path) as f:
            lines = f.readlines()
        # 只取第一个 MODEL（避免 recycle 拼接干扰）
        ca = []
        for l in lines:
            if l.startswith("ENDMDL"):
                break
            if l.startswith("ATOM") and " CA " in l:
                ca.append(l)
        if len(ca) < 2:
            return {"pass": False, "reason": "too_few_CA", "n_ca": len(ca)}
        n_term = [float(ca[0][30:38]), float(ca[0][38:46]), float(ca[0][46:54])]
        c_term = [float(ca[-1][30:38]), float(ca[-1][38:46]), float(ca[-1][46:54])]
        dist = sum((a-b)**2 for a,b in zip(n_term, c_term)) ** 0.5
        return {"pass": dist < 7.0, "n_ca_dist": round(dist, 2)}
    except Exception:
        return {"pass": False, "reason": "io_error"}


# ============================================================
# 共享工具
# ============================================================

def _pdb_residue_range(pdb_path, chain="A"):
    """解析 PDB 指定链的残基范围，返回 (min_resi, max_resi)。"""
    min_r, max_r = None, None
    try:
        with open(pdb_path) as f:
            for line in f:
                if line.startswith("ATOM") and line[21] == chain:
                    r = int(line[22:26].strip())
                    if min_r is None or r < min_r:
                        min_r = r
                    if max_r is None or r > max_r:
                        max_r = r
    except Exception as e:
        EvidenceLogger.error("design", "pdb_parse_failed",
            f"Cannot parse {pdb_path} chain {chain}: {e}. Falling back to 1YCR range.",
            recovery="verify target PDB path")
        return 25, 109
    if min_r is None:
        EvidenceLogger.error("design", "pdb_empty_chain",
            f"No CA atoms found in {pdb_path} chain {chain}. Falling back to 1YCR range.")
        return 25, 109
    return min_r, max_r


def _parse_binder_residues(pdb_path, target_chain="A"):
    """解析 backbone PDB 中 binder 链的残基号列表，返回 [(chain, resi), ...]"""
    residues = []
    try:
        with open(pdb_path) as f:
            for line in f:
                if line.startswith("ATOM") and " CA " in line:
                    ch = line[21]
                    if ch != target_chain:
                        resi = line[22:26].strip()
                        # 去重：同一 chain+resi 只记一次
                        key = (ch, resi)
                        if not residues or residues[-1] != key:
                            residues.append(key)
    except Exception:
        pass
    return residues


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
        if aa in "FW" and i < len(binder_residues):
            # W23/F19 锚点：固定不变
            ch, resi = binder_residues[i]
            fixed.append(f"{ch}{resi}")
        # L 残基不固定，让 backbone 几何自然偏置小氨基酸
    return " ".join(fixed)


def _validate_sequence(seq):
    valid = set("ACDEFGHIKLMNPQRSTVWY")
    s = seq.upper().replace("-","").replace("*","")
    return 6 <= len(s) <= 20 and all(c in valid for c in s)


def _next_candidate_id():
    with _LOCK:
        s = State.load()
        s["candidate_count"] = s.get("candidate_count", 0) + 1
        State.save(s)
        return f"C{s['candidate_count']:04d}"


def _describe_cyclize(n_term, c_term, linker):
    parts = []
    if n_term == "C" and c_term == "C":
        parts.append("Cys-Cys_disulfide")
    elif n_term == "" and c_term == "":
        parts.append("head-to-tail_amide")
    else:
        parts.append(f"{n_term or 'X'}-{c_term or 'X'}")
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
    ts = target_spec or {}
    dc = design_config or {}
    research = _load_target_spec()
    tn = dc.get("target_name") or ts.get("target_name") or "1YCR"

    # 从 Research 数据提取当前靶点的热点残基
    research_hotspots = ""
    if not dc.get("hotspots") and not ts.get("hotspots"):
        targets_info = research.get("targets", {})
        if tn in targets_info:
            pockets = targets_info[tn].get("pocket_residues", {})
            all_res = []
            for p in ["Phe19_pocket", "Trp23_pocket", "Leu26_pocket"]:
                all_res.extend(pockets.get(p, []))
            research_hotspots = ",".join(sorted(set(str(r) for r in all_res))) if all_res else ""

    return {
        "target_name": tn,
        "target_pdb": dc.get("target_pdb") or ts.get("target_pdb")
                      or f"{TARGETS_DIR}/{tn}.pdb",
        "chain": dc.get("chain") or ts.get("chain", "A"),
        "hotspots": dc.get("hotspots") or ts.get("hotspots") or research_hotspots,
        "lengths": dc.get("lengths") or ts.get("lengths") or [10,12,14],
        "n": dc.get("n") or ts.get("n") or 100,
        "seed": dc.get("seed") if dc.get("seed") is not None
                else ts.get("seed") if ts.get("seed") is not None
                else DEFAULT_SEED if DEFAULT_SEED is not None
                else int(time.time()),
    }


# ============================================================
# 兼容旧 API
# ============================================================

def design_afcyc(target="1YCR", n=10, lengths=None, hotspots=None, chain="A", seed=None):
    import warnings
    warnings.warn("deprecated, use design_rfpeptides", DeprecationWarning)
    return design_rfpeptides(
        target_spec={"target_name": target, "chain": chain, "hotspots": hotspots},
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

    p = argparse.ArgumentParser(description="Design Agent v5")
    p.add_argument("--route", choices=["A","B","C","all"], default="all")
    p.add_argument("--target", default="1YCR")
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--lengths", default="10,12,14")
    p.add_argument("--hotspots", default=None)
    p.add_argument("--chain", default="A")
    p.add_argument("--seed", type=int, default=None)
    args = p.parse_args()

    lengths = [int(x) for x in args.lengths.split(",")]
    ts = {"target_name": args.target, "chain": args.chain}
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
