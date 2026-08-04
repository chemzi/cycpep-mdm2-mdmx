"""Unit tests for the agents.design package pure functions (no GPU/external tools)."""
import sys, os, json, tempfile, hashlib, subprocess, types
from pathlib import Path

# ── Stubs for data_layer injected before importing the package ──
class EvidenceLogger:
    @staticmethod
    def log(*a, **kw): pass
    @staticmethod
    def error(*a, **kw): pass
    @staticmethod
    def design_batch(*a, **kw): pass

class CandidateIndex:
    _entries = []
    @classmethod
    def add(cls, entry): cls._entries.append(entry)
    @classmethod
    def load(cls): return list(cls._entries)
    @classmethod
    def stats(cls): return f'{len(cls._entries)} entries'

class State:
    _data = {
        'candidate_count': 0,
        'targets': {},
        'pocket_differences': {},
        'known_dual_binders': [
            {"name": "PMI", "sequence": "TSFAEYWNLLSP", "pmid": "34589387"},
            {"name": "pDI", "sequence": "LTFEHYWAQLTS", "pmid": "19910468"},
            {"name": "ATSP_7041", "sequence": "LTFLEYWAAQSL", "pmid": "23946421"},
        ],
        'design_rules': {},
    }
    @classmethod
    def load(cls): return dict(cls._data)
    @classmethod
    def save(cls, data): cls._data = data

def file_hash(path):
    if os.path.exists(path):
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    return ''

# ── Import the package with the data_layer stub in place ──
import data_layer as _real_data_layer  # noqa: E402  (restored after package import)

_stub = types.ModuleType("data_layer")
_stub.EvidenceLogger = EvidenceLogger
_stub.CandidateIndex = CandidateIndex
_stub.State = State
_stub.file_hash = file_hash
sys.modules["data_layer"] = _stub

from agents.design import config as design_config  # noqa: E402
from agents.design import (  # noqa: E402
    manifests, route_a, route_b, route_c, runtime, service, validation,
)

# Restore the real module for functions that import data_layer lazily
# (e.g. service.pareto_front -> data_layer.compute_pareto_front).
sys.modules["data_layer"] = _real_data_layer

# Flat namespace matching the names the old single-file exec() exposed.
from project_config import load_project_config  # noqa: E402
from agents.design.config import (  # noqa: E402
    DESIGN_PIPELINE_VERSION, RFDIFF_CONDA, RFDIFF_DIR, SE3_ROOT, _resolve_output_dir,
)
from agents.design.manifests import _candidate_from_manifest, _write_manifest  # noqa: E402
from agents.design.route_b import design_motif_guided  # noqa: E402
from agents.design.route_c import (  # noqa: E402
    _route_c_base_combos, _route_c_cyclization_pairs, _route_c_design_references,
    design_atsp_derived,
)
from agents.design.runtime import (  # noqa: E402
    _build_refold_script, _rfdiff_subprocess_env, _run_rfdiff,
)
from agents.design.service import (  # noqa: E402
    _load_target_spec, _merge_config, _next_candidate_id, pareto_front, threshold_filter,
)
from agents.design.validation import (  # noqa: E402
    _binder_first_contig, _cheap_filter_sequences, _describe_cyclize,
    _extract_ligandmpnn_binder_sequence, _hotspot_fixed_residues,
    _hotspot_positions, _infer_binder_chain, _parse_binder_residues,
    _pdb_chain_residue_layout, _pdb_chain_sequences, _pdb_residue_range,
    _ring_closure_check, _sequence_quality_score, _synthesizability_violations,
    _validate_sequence, _verify_fixed_sequence_pdb,
)

# ── Approved target fixture used by Design v5 config integration ──
from target_bootstrap import ReviewRequiredError, config_digest

target_fixture = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
target_fixture.write(
    'ATOM      1  CA  ALA B   1       1.000   2.000   3.000  1.00  0.00           C  \n'
)
target_fixture.close()
ACTIVE_PROJECT_CONFIG = load_project_config(raw={
    'project_id': 'design_v5_mdm2_mdmx_test',
    'targets': [
        {
            'id': 'MDM2',
            'structure': {
                'pdb_id': '1YCR',
                'chain': 'A',
                'coordinate_path': target_fixture.name,
                'coordinate_sha256': hashlib.sha256(
                    open(target_fixture.name, 'rb').read()
                ).hexdigest(),
            },
            'binding_site': {'residues': [54, 93, 96], 'status': 'user_reviewed'},
            'design': {'lengths': [8, 9]},
        },
        {
            'id': 'MDMX',
            'structure': {
                'pdb_id': '3DAB',
                'chain': 'B',
                'coordinate_path': target_fixture.name,
                'coordinate_sha256': hashlib.sha256(
                    open(target_fixture.name, 'rb').read()
                ).hexdigest(),
            },
            'binding_site': {'residues': [53, 92, 95], 'status': 'user_reviewed'},
            'design': {'lengths': [8, 9]},
        },
    ],
})
ACTIVE_PROJECT_CONFIG['review'] = {
    'status': 'approved',
    'approved_digest': config_digest(ACTIVE_PROJECT_CONFIG),
}

# Point the design package at this fixture instead of the repo default config.
design_config.ACTIVE_PROJECT_CONFIG = ACTIVE_PROJECT_CONFIG

failures = []


AA1_TO_3 = {
    'A': 'ALA', 'C': 'CYS', 'D': 'ASP', 'E': 'GLU', 'F': 'PHE',
    'G': 'GLY', 'H': 'HIS', 'I': 'ILE', 'K': 'LYS', 'L': 'LEU',
    'M': 'MET', 'N': 'ASN', 'P': 'PRO', 'Q': 'GLN', 'R': 'ARG',
    'S': 'SER', 'T': 'THR', 'V': 'VAL', 'W': 'TRP', 'Y': 'TYR',
}


def pdb_atom(serial, atom_name, residue_name, chain, residue_number, xyz):
    x, y, z = xyz
    element = atom_name[0]
    return (
        f'ATOM  {serial:5d} {atom_name:>4s} {residue_name:>3s} '
        f'{chain}{residue_number:4d}    '
        f'{x:8.3f}{y:8.3f}{z:8.3f}{1.00:6.2f}{0.00:6.2f}'
        f'          {element:>2s}  \n'
    )


def monomer_pdb(
        sequence, *, chain='A', nc_distance=1.33, sg_distance=2.03,
        include_terminal_atoms=True):
    lines, serial = [], 1
    for index, amino_acid in enumerate(sequence, 1):
        residue_name = AA1_TO_3[amino_acid]
        if index == 1 and include_terminal_atoms:
            lines.append(
                pdb_atom(serial, 'N', residue_name, chain, index, (0.0, 0.0, 0.0))
            )
            serial += 1
        # Put CA atoms far apart so closure tests cannot accidentally use them.
        lines.append(
            pdb_atom(
                serial, 'CA', residue_name, chain, index,
                (20.0 + index * 3.0, 0.0, 0.0),
            )
        )
        serial += 1
        if amino_acid == 'C' and index in (1, len(sequence)):
            x = 0.0 if index == 1 else sg_distance
            lines.append(
                pdb_atom(serial, 'SG', residue_name, chain, index, (x, 2.0, 0.0))
            )
            serial += 1
        if index == len(sequence) and include_terminal_atoms:
            lines.append(
                pdb_atom(
                    serial, 'C', residue_name, chain, index,
                    (nc_distance, 0.0, 0.0),
                )
            )
            serial += 1
    return ''.join(lines)


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print(f'  FAIL: {msg}')
    else:
        print(f'  ok: {msg}')


def check_raises(error_type, fn, msg):
    try:
        fn()
    except error_type:
        check(True, msg)
    except Exception as exc:
        check(False, f'{msg}: raised {type(exc).__name__}, expected {error_type.__name__}')
    else:
        check(False, f'{msg}: did not raise {error_type.__name__}')

# ── Test 1: _validate_sequence ──
print('Test 1: _validate_sequence')
check(_validate_sequence('ACDEFGHI'), 'basic valid seq')
check(_validate_sequence('GDEETGE'), '7-aa KEAP1 benchmark sequence accepted')
check(_validate_sequence('CACDEFGHIC'), 'Cys flanked seq')
check(not _validate_sequence(''), 'empty rejected')
check(not _validate_sequence('AAAAA'), 'too short (5) rejected')
check(not _validate_sequence('AAAAAA'), '6-aa sequence rejected')
check(not _validate_sequence('A' * 21), 'too long (21) rejected')
check(not _validate_sequence('ACDXEFG'), 'nonstandard X rejected')
check(not _validate_sequence(None), 'None rejected')
check(_validate_sequence('acdefghi'), 'lowercase accepted')
check(_validate_sequence('ACD-EFG*HI'), 'hyphen and star stripped')
check(_binder_first_contig('A', 328, 609, 7) == '7-7 A328-609/0',
      'RFdiffusion contig accepts 7-aa cyclic binder')

# ── Test 2: _describe_cyclize ──
print('Test 2: _describe_cyclize')
d = _describe_cyclize('C', 'C', '')
check('Cys-Cys_disulfide' in d, f'Cys-Cys -> {d}')
d = _describe_cyclize('', '', '')
check('head-to-tail_amide' in d, f'head-to-tail -> {d}')
d = _describe_cyclize('', '', 'GGGGS')
check('linker=GGGGS' in d, f'linker -> {d}')

# ── Test 3: _next_candidate_id ──
print('Test 3: _next_candidate_id')
State._data['candidate_count'] = 0
CandidateIndex._entries = []
c1 = _next_candidate_id()
c2 = _next_candidate_id()
check(c1 == 'C0001', f'c1={c1}')
check(c2 == 'C0002', f'c2={c2}')

print('Test 3b: _next_candidate_id reconciles a stale state counter')
State._data['candidate_count'] = 2
CandidateIndex._entries = [{'candidate_id': 'C0505'}]
c3 = _next_candidate_id()
check(c3 == 'C0506', f'c3={c3}')

# ── Test 4: _load_target_spec ──
print('Test 4: _load_target_spec')
spec = _load_target_spec()
check('targets' in spec, 'has targets')
check('known_dual_binders' in spec, 'has known_dual_binders')
check('design_rules' in spec, 'has design_rules')
check(len(spec['known_dual_binders']) == 3, f'3 binders, got {len(spec["known_dual_binders"])}')

# ── Test 5: _merge_config ──
print('Test 5: _merge_config')
cfg = _merge_config({'target_name': '3DAB', 'chain': 'B'}, {'n': 50, 'lengths': [8, 9]})
check(cfg['target_name'] == 'MDMX', f'target_name={cfg["target_name"]}')
check(cfg['target_id'] == 'MDMX', f'target_id={cfg["target_id"]}')
check(cfg['target_pdb'] == os.path.realpath(target_fixture.name),
      f'approved coordinate_path={cfg["target_pdb"]}')
check(cfg['chain'] == 'B', f'chain={cfg["chain"]}')
check(cfg['hotspots'] == '53,92,95', f'hotspots={cfg["hotspots"]}')
check(cfg['n'] == 50, f'n={cfg["n"]}')
check(cfg['lengths'] == [8, 9], f'lengths={cfg["lengths"]}')
check(cfg['seed'] is not None, 'seed auto-resolved')
check_raises(
    ValueError,
    lambda: _merge_config({'target_name': '3DAB'}, {'target_pdb': '/tmp/unapproved.pdb'}),
    'unapproved target_pdb override rejected',
)

# ── Test 6: seed=None -> auto, seed=0 preserved ──
print('Test 6: seed edge cases')
cfg2 = _merge_config(None, {'seed': None})
check(cfg2['seed'] is not None, 'seed=None auto-resolved')
cfg3 = _merge_config(None, {'seed': 0})
check(cfg3['seed'] == 0, f'seed=0 preserved, got {cfg3["seed"]}')

# ── Test 6b: project approval gate ──
print('Test 6b: project approval gate')
approved_review = ACTIVE_PROJECT_CONFIG['review']
ACTIVE_PROJECT_CONFIG['review'] = {'status': 'pending'}
check_raises(ReviewRequiredError, lambda: _merge_config(None, None), 'unapproved project rejected')
ACTIVE_PROJECT_CONFIG['review'] = approved_review

# ── Test 7: threshold_filter ──
print('Test 7: threshold_filter')
cands = [
    {'ipsae_mdm2': 0.7, 'ipsae_mdmx': 0.6, 'hotspot_cov_mdm2': 0.8, 'hotspot_cov_mdmx': 0.8},
    {'ipsae_mdm2': 0.5, 'ipsae_mdmx': 0.6, 'hotspot_cov_mdm2': 0.8, 'hotspot_cov_mdmx': 0.8},
    {'ipsae_mdm2': 0.7, 'ipsae_mdmx': 0.4, 'hotspot_cov_mdm2': 0.8, 'hotspot_cov_mdmx': 0.8},
]
# P1-1: MDM legacy thresholds rejected without explicit opt-in
os.environ["CYCPEP_ALLOW_UNVALIDATED_MDM_THRESHOLDS"] = "1"
passed = threshold_filter(cands, {})
check(len(passed) == 1, f'1 passed, got {len(passed)}')
nested = [{
    'metrics': {'targets': {
        'MDM2': {'ipsae': 0.7, 'hotspot_cov': 0.8},
        'MDMX': {'ipsae': 0.6, 'hotspot_cov': 0.8},
    }}
}]
check(threshold_filter(nested, {}) == nested, 'nested per-target metrics pass')
del os.environ["CYCPEP_ALLOW_UNVALIDATED_MDM_THRESHOLDS"]

# P1-1: without opt-in, uncalibrated MDM thresholds ⇒ hard-reject
print('Test 7b: uncalibrated threshold gate')
rejected = threshold_filter(cands, {})
check(len(rejected) == 0, f'uncalibrated thresholds rejected: got {len(rejected)}')

# ── Test 8: pareto_front ──
print('Test 8: pareto_front')
cands = [
    {'candidate_id': 'C1', 'ipsae_mdm2': 0.7, 'ipsae_mdmx': 0.7},
    {'candidate_id': 'C2', 'ipsae_mdm2': 0.8, 'ipsae_mdmx': 0.6},
    {'candidate_id': 'C3', 'ipsae_mdm2': 0.6, 'ipsae_mdmx': 0.8},
    {'candidate_id': 'C4', 'ipsae_mdm2': 0.5, 'ipsae_mdmx': 0.5},
]
front = pareto_front(cands)
check(len(front) == 3, f'3 on front, got {len(front)}')
front_pairs = [(c['ipsae_mdm2'], c['ipsae_mdmx']) for c in front]
check((0.5, 0.5) not in front_pairs, 'dominated not on front')

# ── Test 9: _ring_closure_check ──
print('Test 9: _ring_closure_check')
tmp1 = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
tmp1.write(monomer_pdb('ACDEFGHI', nc_distance=1.33))
tmp1.close()
rc = _ring_closure_check(
    tmp1.name, 'head-to-tail_amide', sequence='ACDEFGHI'
)
check(rc['pass'] is True, f'peptide C-N geometry should pass, got {rc}')
check(rc['atom_1'] == 'last:C' and rc['atom_2'] == 'first:N',
      f'head-to-tail must inspect C-N, got {rc}')
check(rc['distance_angstrom'] == 1.33, f'C-N distance should be recorded, got {rc}')
os.unlink(tmp1.name)

# CA atoms remain far apart, yet a valid C-N bond passes.  Conversely, close CA
# atoms cannot rescue an invalid C-N distance.
tmp2 = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
tmp2.write(monomer_pdb('ACDEFGHI', nc_distance=3.00))
tmp2.close()
rc2 = _ring_closure_check(
    tmp2.name, 'head-to-tail_amide', sequence='ACDEFGHI'
)
check(rc2['pass'] is False, f'long peptide C-N distance should fail, got {rc2}')
check(rc2['reason'] == 'distance_out_of_range',
      f'failed C-N distance should be auditable, got {rc2}')
os.unlink(tmp2.name)

tmp3 = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
tmp3.write(monomer_pdb('CACDEFGHIC', sg_distance=2.03))
tmp3.close()
rc3 = _ring_closure_check(
    tmp3.name, 'Cys-Cys_disulfide', sequence='CACDEFGHIC'
)
check(rc3['pass'] is True, f'disulfide SG-SG geometry should pass, got {rc3}')
check(rc3['atom_1'] == 'first:SG' and rc3['atom_2'] == 'last:SG',
      f'disulfide must inspect SG-SG, got {rc3}')
os.unlink(tmp3.name)

tmp4 = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
tmp4.write(monomer_pdb('ACDEFGHI'))
tmp4.close()
rc4 = _ring_closure_check(
    tmp4.name, 'Cys-Cys_disulfide', sequence='ACDEFGHI'
)
check(rc4['pass'] is False and rc4['reason'] == 'terminal_residues_not_cysteine',
      f'disulfide requires terminal cysteines, got {rc4}')
unsupported = _ring_closure_check(tmp4.name, 'stapled_hydrocarbon')
check(unsupported['pass'] is False and unsupported['reason'] == 'unsupported_cyclization',
      f'unknown chemistry must fail closed, got {unsupported}')
os.unlink(tmp4.name)

tmp5 = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
tmp5.write(monomer_pdb('ACDEFGHI', include_terminal_atoms=False))
tmp5.close()
missing = _ring_closure_check(
    tmp5.name, 'head-to-tail_amide', sequence='ACDEFGHI'
)
check(missing['pass'] is False and missing['reason'] == 'closure_atom_missing',
      f'missing closure atoms must fail closed, got {missing}')
os.unlink(tmp5.name)

tmp6 = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
tmp6.write(monomer_pdb('ACDEFGHI', chain='A'))
tmp6.write(monomer_pdb('ACDEFGHI', chain='B'))
tmp6.close()
ambiguous = _ring_closure_check(
    tmp6.name, 'head-to-tail_amide', sequence='ACDEFGHI'
)
check(ambiguous['pass'] is False and ambiguous['reason'] == 'ambiguous_monomer_chain',
      f'multi-chain refold must fail closed, got {ambiguous}')
os.unlink(tmp6.name)

tmp7 = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
tmp7.write(monomer_pdb('CACDEFGHIC', sg_distance=3.50))
tmp7.close()
far_disulfide = _ring_closure_check(
    tmp7.name, 'Cys-Cys_disulfide', sequence='CACDEFGHIC'
)
check(far_disulfide['pass'] is False and far_disulfide['reason'] == 'distance_out_of_range',
      f'long SG-SG distance must fail, got {far_disulfide}')
os.unlink(tmp7.name)

check(_pdb_residue_range(target_fixture.name, 'B') == (1, 1),
      'approved target chain range parsed without legacy fallback')

# ── Test 10: _hotspot_positions ──
print('Test 10: _hotspot_positions')
hp = _hotspot_positions('TSFAEYWNLLSP')
check(hp == {2: 'F', 6: 'W', 8: 'L', 9: 'L'}, f'got {hp}')

# ── Test 11: _hotspot_fixed_residues ──
print('Test 11: _hotspot_fixed_residues')
binder_res = [('a', str(i + 1)) for i in range(12)]
hp = {2: 'F', 6: 'W', 7: 'L', 8: 'L'}
fixed = _hotspot_fixed_residues(hp, binder_res)
check('a3' in fixed, f'F@2 should map to a3, got: {fixed}')
check('a7' in fixed, f'W@6 should map to a7, got: {fixed}')
check('a8' not in fixed, f'L@7 should NOT be fixed, got: {fixed}')
check('a9' not in fixed, f'L@8 should NOT be fixed, got: {fixed}')

# ── Test 12: Route C expansion ──
print('Test 12: Route C expansion logic')
check(_route_c_cyclization_pairs('head_to_tail_cyclic_peptide') == [('', '')],
      'head-to-tail project excludes terminal-disulfide Route C candidates')
check(_route_c_cyclization_pairs('disulfide_cyclic_peptide') == [('C', 'C')],
      'disulfide project excludes head-to-tail Route C candidates')
route_c_head_to_tail = _route_c_base_combos(
    'TSFAEYWNLLSP', [12], 'head_to_tail_cyclic_peptide'
)
check(route_c_head_to_tail == [('TSFAEYWNLLSP', 'head-to-tail_amide')],
      f'Route C honours approved length and modality, got {route_c_head_to_tail}')
check_raises(
    ValueError,
    lambda: _route_c_cyclization_pairs('stapled_peptide'),
    'unsupported Route C chemistry fails closed',
)
import random
random.seed(42)
orig = [('LTFLEYWAAQSL', 'head-to-tail_amide')]
expanded = list(orig)
seen = set(s for s, _ in orig)
n, attempts = 10, 0
while len(expanded) < n and attempts < n * 10:
    attempts += 1
    seq, desc = random.choice(orig)
    aa = random.choice('ACDEFGHIKLMNPQRSTVWY')
    off = 1 if seq and seq[0] == 'C' else 0
    tail_guard = 1 if seq and seq[-1] == 'C' else 0
    max_pos = len(seq) - off - tail_guard
    if max_pos < 2:
        continue
    pos = random.randint(1, max_pos)
    ix = off + pos - 1
    mut = seq[:ix] + aa + seq[ix + 1:]
    if _validate_sequence(mut) and mut not in seen:
        seen.add(mut)
        expanded.append((mut, f'{desc},mut:{pos}={aa}'))
check(len(expanded) == n, f'expected {n}, got {len(expanded)}')
check(all(_validate_sequence(s) for s, _ in expanded), 'all sequences valid')

# ── Test 13: Route B empty binders guard ──
print('Test 13: Route B empty binders returns []')
State._data['known_dual_binders'] = []
result = design_motif_guided(target_spec={'target_name': 'MDMX'}, design_config={'n': 10})
check(result == [], f'empty binders should return [], got {result}')
State._data['known_dual_binders'] = [
    {'name': 'PMI', 'sequence': 'TSFAEYWNLLSP', 'pmid': '34589387'}
]

# ── Test 14: _write_manifest ──
print('Test 14: _write_manifest + cyclization detection')
tmp_pdb = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
tmp_pdb.write(monomer_pdb('CACDEFGHIC', sg_distance=2.03))
tmp_pdb.close()
cfg_test = {'target_name': '1YCR', 'target_pdb': '/tmp/test.pdb', 'seed': 42}
m1 = _write_manifest('C0001', 'CACDEFGHIC', 'route_A_test', 'batch_1', tmp_pdb.name, cfg_test)
check(m1['cyclization_type'] == 'Cys-Cys_disulfide', f'Cys flanked -> {m1["cyclization_type"]}')
check(m1['design_pipeline_version'] == DESIGN_PIPELINE_VERSION,
      f'manifest records Design version {DESIGN_PIPELINE_VERSION}')
check(m1['backbone_pdb'] == '', 'no backbone -> empty string')
check(m1['design_reference_pdb'] == '', 'missing reference remains explicit')
check(len(m1['refold_pdb_hash']) > 0, 'refold hash present')
check(m1['ring_closure']['pass'] is True, f'disulfide manifest geometry passes: {m1["ring_closure"]}')
# With cyclization arg
m2 = _write_manifest(
    'C0002', 'CACDEFGHIC', 'route_C_test', 'batch_2', tmp_pdb.name,
    cfg_test, cyclization='Cys-Cys_disulfide,linker=GGGGS',
)
check(m2['cyclization_type'] == 'Cys-Cys_disulfide',
      f'custom description is canonicalized -> {m2["cyclization_type"]}')
check(m2['cyclization_description'] == 'Cys-Cys_disulfide,linker=GGGGS',
      'cyclization modifiers remain auditable')
handoff = _candidate_from_manifest(m2, 0.9)
check(handoff['manifest_path'] == m2['manifest_path'], 'candidate handoff preserves manifest path')
check(handoff['design_pdb_path'] == tmp_pdb.name, 'candidate handoff preserves refold PDB')
check(handoff['cyclization_bonds'][0]['bond_type'] == 'disulfide',
      'candidate handoff preserves cyclization bond intent')
os.unlink(tmp_pdb.name)

head_pdb = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
head_pdb.write(monomer_pdb('ACDEFGHI', nc_distance=1.33))
head_pdb.close()
m3 = _write_manifest(
    'C0003', 'ACDEFGHI', 'route_C_test', 'batch_3', head_pdb.name,
    cfg_test, cyclization='head-to-tail_amide,linker=GGGGS',
)
check(m3['cyclization_type'] == 'head-to-tail_amide',
      'head-to-tail modifiers do not corrupt the stable downstream contract')
check(m3['ring_closure']['pass'] is True,
      f'head-to-tail manifest carries C-N geometry: {m3["ring_closure"]}')
os.unlink(head_pdb.name)

reference_pdb = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
reference_pdb.write(monomer_pdb('ACDEFGHI', chain='B'))
reference_pdb.write('REMARK independent RFdiffusion target-bound backbone\n')
reference_pdb.close()
refold_pdb = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
refold_pdb.write(monomer_pdb('ACDEFGHI', chain='A'))
refold_pdb.close()
m4 = _write_manifest(
    'C0004', 'ACDEFGHI', 'route_C_test', 'batch_4', refold_pdb.name,
    cfg_test, backbone_pdb=reference_pdb.name,
)
check(m4['design_reference_pdb'] == os.path.realpath(reference_pdb.name),
      'manifest records explicit independent L7 reference')
check(m4['design_reference_role'] == 'rfdiffusion_target_bound_backbone',
      'manifest records the reference-generating role')
check(m4['backbone_pdb'] == m4['design_reference_pdb'],
      'legacy backbone alias remains consistent')
check_raises(
    ValueError,
    lambda: _write_manifest(
        'C0005', 'ACDEFGHI', 'route_C_test', 'batch_5', refold_pdb.name,
        cfg_test, backbone_pdb=refold_pdb.name,
    ),
    'fixed-sequence refold cannot be reused as its own L7 reference',
)
os.unlink(reference_pdb.name)
os.unlink(refold_pdb.name)

# ── Test 15: cheap filter ──
print('Test 15: cheap pre-filter')
# synthesizability
check(len(_synthesizability_violations('ATDEFGHI')) == 0, 'clean seq passes')
check(len(_synthesizability_violations('AAAAANGLLL')) > 0, 'NG deamidation caught')
check(len(_synthesizability_violations('AAAADPLLL')) > 0, 'DP cleavage caught')
check(len(_synthesizability_violations('IIIIIILLLLLL')) > 0, 'hydrophobic aggregation caught')
check(len(_synthesizability_violations('CCCACCC')) > 0, 'stray Cys caught')
check(len(_synthesizability_violations('LTFLEYWAAQSL')) == 0, 'ATSP-7041 (has W) passes')
# quality score
s1 = _sequence_quality_score('ATDEFGHI')
s2 = _sequence_quality_score('IIIIIILLLL')
check(s1 > s2, f'balanced > hydrophobic: {s1:.2f} vs {s2:.2f}')
# W penalty
s_atsp = _sequence_quality_score('LTFLEYWAAQSL')
s_now  = _sequence_quality_score('LTFLEYAAAQSL')  # W→A, no oxidation penalty
check(s_now > s_atsp, f'W penalty applied: {s_now:.3f} > {s_atsp:.3f}')
# top-k filter (all-G passes synthesizability but scores very low)
filtered = _cheap_filter_sequences(
    ['LTFLEYWAAQSL', 'TSFAEYWNLLSP', 'GLITPEGFSK', 'ATDEFGHI', 'GGGGGGGGGG'], top_k=4)
check(len(filtered) == 4, f'top_k=4, got {len(filtered)}')
check('GGGGGGGGGG' not in [s for s,_ in filtered], 'all-G ranked out of top-4')
# ATSP passes (W is soft penalty, not hard reject)
check(any('LTFLE' in s for s,_ in filtered), 'ATSP-7041 passes')

# ── Test 16: refold preserves the fixed sequence across restart ──
print('Test 16: fixed-sequence refold script')
refold_script = _build_refold_script('ACDEFGHI', '/tmp/refold.pdb')
check('model.prep_inputs(length=8)' in refold_script, 'refold prepares the sequence length')
check("model.restart(seed=0, seq='ACDEFGHI')" in refold_script,
      'restart receives the fixed sequence')
check('model.set_seq' not in refold_script, 'legacy set_seq call removed')
check('\nmodel.restart()\n' not in refold_script, 'sequence-resetting restart removed')
check('model.predict(' in refold_script, 'fixed sequence uses prediction-only API')
check('model.design_3stage' not in refold_script, 'sequence optimizer is absent')
check('model.get_seq(get_best=False)' in refold_script, 'model sequence is verified')
check('pdb_sequences' in refold_script, 'output PDB sequence is independently verified')
check('len(pdb_sequences) != 1' in refold_script,
      'fixed-sequence refold rejects extra PDB chains')
check('rev-parse' in refold_script, 'ColabDesign commit is pinned at runtime')
check('--untracked-files=no' in refold_script, 'tracked dependency changes are rejected')
check('"offset" in batch' in refold_script, 'cyclic offset backend capability is checked')

fixed_pdb = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
fixed_pdb.write(monomer_pdb('ACDEFGHI'))
fixed_pdb.close()
check(_verify_fixed_sequence_pdb(fixed_pdb.name, 'ACDEFGHI') == {'A': 'ACDEFGHI'},
      'host-side verifier accepts the exact saved PDB sequence')
check_raises(
    ValueError,
    lambda: _verify_fixed_sequence_pdb(fixed_pdb.name, 'AAAAAAAA'),
    'host-side verifier rejects a saved PDB sequence mismatch',
)
with open(fixed_pdb.name, 'a') as handle:
    handle.write(monomer_pdb('ACDEFGHI', chain='B'))
check_raises(
    ValueError,
    lambda: _verify_fixed_sequence_pdb(fixed_pdb.name, 'ACDEFGHI'),
    'host-side verifier rejects an extra output chain',
)
os.unlink(fixed_pdb.name)

# ── Test 17: RFdiffusion subprocess receives validated activate.d runtime ──
print('Test 17: RFdiffusion subprocess environment')
rfdiff_env = _rfdiff_subprocess_env()
check(rfdiff_env['DGLBACKEND'] == 'pytorch', 'DGL backend is explicit')
check(SE3_ROOT in rfdiff_env['PYTHONPATH'], 'SE3Transformer is on PYTHONPATH')
check(RFDIFF_DIR in rfdiff_env['PYTHONPATH'], 'RFdiffusion source is on PYTHONPATH')
check(f'{RFDIFF_CONDA}/lib' in rfdiff_env['LD_LIBRARY_PATH'],
      'RFdiffusion environment libraries are on LD_LIBRARY_PATH')
check(
    _binder_first_contig('A', 25, 109, 10) == '10-10 A25-109/0',
    'RFdiffusion contig puts cyclic binder before fixed receptor',
)

# Hydra must receive hotspots as a list, not one comma-containing string.
captured_run = {}
original_subprocess_run = subprocess.run
class _SuccessfulRun:
    returncode = 0
    stderr = ''
subprocess.run = lambda cmd, **kwargs: (
    captured_run.update({'cmd': cmd, 'kwargs': kwargs}) or _SuccessfulRun()
)
try:
    check(
        _run_rfdiff('/tmp/target.pdb', 10, 1, '/tmp/out',
                    _binder_first_contig('A', 25, 109, 10),
                    hotspots='54,93,96', chain='A'),
        'RFdiffusion command wrapper accepts a successful run',
    )
finally:
    subprocess.run = original_subprocess_run
check("ppi.hotspot_res=['A54','A93','A96']" in captured_run['cmd'],
      'Hydra hotspot residues are passed as quoted strings')
check("contigmap.contigs=['10-10 A25-109/0']" in captured_run['cmd'],
      'Hydra receives binder-first contig order')
check("inference.seed" not in str(captured_run['cmd']),
      'seed omitted when None')

# RFdiffusion does not support per-run seeds at the GPU/DGL level;
# _run_rfdiff intentionally ignores the seed parameter.  The seed is
# still honoured by LigandMPNN, AfCycDesign, and Route C mutation.
captured_run2 = {}
subprocess.run = lambda cmd, **kwargs: (
    captured_run2.update({'cmd': cmd, 'kwargs': kwargs}) or _SuccessfulRun()
)
try:
    _run_rfdiff('/tmp/target.pdb', 10, 1, '/tmp/out',
                _binder_first_contig('A', 25, 109, 10),
                seed=42, chain='A')
finally:
    subprocess.run = original_subprocess_run
check("inference.seed" not in str(captured_run2['cmd']),
      'seed intentionally omitted (RFdiffusion GPU non-deterministic)')

# Route C must acquire an independent target-bound reference for every sequence
# before it is allowed to create a fixed-sequence refold candidate.
with tempfile.TemporaryDirectory() as route_c_root:
    def _fake_route_c_rfdiff(**kwargs):
        prefix = Path(kwargs['output_prefix'])
        prefix.parent.mkdir(parents=True, exist_ok=True)
        for index in range(kwargs['n_designs']):
            (prefix.parent / f'{prefix.name}_{index}.pdb').write_text(
                monomer_pdb('A' * kwargs['binder_len'], chain='A')
                + monomer_pdb('AAA', chain='B')
            )
        return True

    original_run_rfdiff = route_c._run_rfdiff
    route_c._run_rfdiff = _fake_route_c_rfdiff
    try:
        route_c_references = _route_c_design_references(
            {
                'target_pdb': target_fixture.name,
                'chain': 'B',
                'hotspots': '',
                'seed': 42,
            },
            route_c_root,
            [
                ('ACDEFGHI', 'first'),
                ('KLMNPQRS', 'second'),
                ('ACDEFGHIK', 'third'),
            ],
        )
    finally:
        route_c._run_rfdiff = original_run_rfdiff
    check(set(route_c_references) == {0, 1, 2},
          'Route C obtains one validated L7 reference per sequence')
    check(len(set(route_c_references.values())) == 3,
          'Route C does not reuse one reference across candidates')

# RFdiffusion may relabel output chains. Discover the binder by residue count
# and map LigandMPNN FASTA segments using the emitted PDB chain order.
chain_fixture = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
serial = 1
# RFdiffusion may emit the generated chain before receptor chain records even
# though LigandMPNN serializes FASTA segments by sorted chain ID.
for chain, start, count in [('B', 1, 8), ('A', 25, 3)]:
    for residue in range(start, start + count):
        chain_fixture.write(
            f'ATOM  {serial:5d}  CA  ALA {chain}{residue:4d}    '
            f'{serial:8.3f}{0.0:8.3f}{0.0:8.3f}'
            f'{1.00:6.2f}{0.00:6.2f}           C  \n'
        )
        serial += 1
chain_fixture.close()
layout = _pdb_chain_residue_layout(chain_fixture.name)
input_sequences = _pdb_chain_sequences(chain_fixture.name)
check(_infer_binder_chain(chain_fixture.name, 8) == 'B',
      'binder chain is discovered from emitted residue count')
check(len(_parse_binder_residues(chain_fixture.name, 'B')) == 8,
      'only validated binder residues are passed to motif mapping')
check(
    _extract_ligandmpnn_binder_sequence(
        'AAA:ACDEFGHI', 'B', layout, input_sequences
    ) == 'ACDEFGHI',
    'LigandMPNN FASTA extraction follows its sorted chain order',
)
check_raises(
    ValueError,
    lambda: _extract_ligandmpnn_binder_sequence(
        'GGG:ACDEFGHI', 'B', layout, input_sequences
    ),
    'LigandMPNN output that changes the fixed receptor is rejected',
)
check_raises(
    ValueError,
    lambda: _extract_ligandmpnn_binder_sequence(
        'ACDEFGHI', 'B', layout
    ),
    'malformed multi-chain LigandMPNN FASTA is rejected',
)
os.unlink(chain_fixture.name)

# ── Test 18: output directory resolution is permission-safe in CI ──
print('Test 18: CI-safe output directory resolution')
class _DeniedDamodelPath:
    def is_dir(self):
        raise PermissionError(13, 'permission denied', '/root/damodel-tmp/novapeptide')

with tempfile.TemporaryDirectory() as runner_temp:
    explicit_dir = Path(runner_temp) / 'explicit-designs'
    resolved_explicit = _resolve_output_dir(
        {'CYCPEP_DESIGN_ROOT': str(explicit_dir)}, _DeniedDamodelPath())
    check(resolved_explicit == explicit_dir,
          'explicit design root bypasses inaccessible /root path')

    resolved_ci = _resolve_output_dir(
        {'RUNNER_TEMP': runner_temp}, _DeniedDamodelPath())
    expected_ci = Path(runner_temp) / 'novapeptide' / 'designs'
    check(resolved_ci == expected_ci,
          'permission error falls back to GitHub runner temp')

# ── Test 19: _pdb_residue_range hotspot validation (P1 fix) ──
print('Test 19: _pdb_residue_range hotspot validation')

# Fixture: two segments separated by gap>50
two_seg_fixture = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
# Segment 1: residues 1-20
for i in range(1, 21):
    two_seg_fixture.write(
        f'ATOM  {i:5d}  CA  ALA A{i:4d}       {i}.000   2.000   3.000  1.00  0.00           C  \n'
    )
# Segment 2: residues 100-110 (gap 80 > 50)
for i in range(100, 111):
    two_seg_fixture.write(
        f'ATOM  {i:5d}  CA  ALA A{i:4d}       {i}.000   2.000   3.000  1.00  0.00           C  \n'
    )
two_seg_fixture.close()

# Without hotspots → longest segment (1-20: span=19 vs 100-110: span=10)
rng = _pdb_residue_range(two_seg_fixture.name, 'A')
check(rng == (1, 20), f'longest segment without hotspots, got {rng}')

# Hotspot in shorter segment → must return that segment
rng2 = _pdb_residue_range(two_seg_fixture.name, 'A', hotspot_residues=[105])
check(rng2 == (100, 110), f'hotspot at 105 forces second segment, got {rng2}')

# Hotspot in longer segment → still returns longest
rng3 = _pdb_residue_range(two_seg_fixture.name, 'A', hotspot_residues=[10, 15])
check(rng3 == (1, 20), f'hotspots in longest segment, got {rng3}')

# Hotspots spanning both segments → ValueError
check_raises(ValueError,
    lambda: _pdb_residue_range(two_seg_fixture.name, 'A', hotspot_residues=[10, 105]),
    'hotspots spanning two segments must raise ValueError')

# Hotspot absent from PDB entirely → ValueError (P0: no silent fallback)
check_raises(ValueError,
    lambda: _pdb_residue_range(two_seg_fixture.name, 'A', hotspot_residues=[999]),
    'all hotspots absent from PDB must raise ValueError')

# Empty hotspot string → back to longest segment
rng5 = _pdb_residue_range(two_seg_fixture.name, 'A', hotspot_residues=[])
check(rng5 == (1, 20), f'empty hotspots → longest segment, got {rng5}')

# Hotspot isolated between segments (gap>50 both sides) → uses its own single-residue segment
gap_fixture = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
for i in range(1, 6):
    gap_fixture.write(
        f'ATOM  {i:5d}  CA  ALA A{i:4d}       {i}.000   2.000   3.000  1.00  0.00           C  \n'
    )
# Single isolated residue at position 80 (use same format as loop for correct PDB column alignment)
gap_fixture.write(
    f'ATOM  {100:5d}  CA  ALA A{80:4d}       80.000   2.000   3.000  1.00  0.00           C  \n'
)
for i in range(150, 156):
    gap_fixture.write(
        f'ATOM  {i:5d}  CA  ALA A{i:4d}       {i}.000   2.000   3.000  1.00  0.00           C  \n'
    )
gap_fixture.close()
rng_gap = _pdb_residue_range(gap_fixture.name, 'A', hotspot_residues=[80])
check(rng_gap == (80, 80), f'isolated hotspot → own segment, got {rng_gap}')
os.unlink(gap_fixture.name)

# Single segment PDB → works with or without hotspots
single_seg = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
for i in range(50, 71):
    single_seg.write(
        f'ATOM  {i:5d}  CA  ALA A{i:4d}       {i}.000   2.000   3.000  1.00  0.00           C  \n'
    )
single_seg.close()
rng5 = _pdb_residue_range(single_seg.name, 'A', hotspot_residues=[53, 68])
check(rng5 == (50, 70), f'single segment with hotspots, got {rng5}')

os.unlink(two_seg_fixture.name)
os.unlink(single_seg.name)

# ── Test 20: Route C empty base_combos guard (P2 fix) ──
print('Test 20: Route C empty base_combos guard')

# Simulate empty base_combos path: no linker×cyclization combo passes synthesizability
# The guard added after base_combos construction should return [] before random.choice
# We verify the guard exists by checking the source directly
guard_pattern = 'if not base_combos:'
with open('agents/design/route_c.py', encoding='utf-8') as f:
    design_source = f.read()
check(guard_pattern in design_source,
      'empty base_combos guard is present in design_atsp_derived')

# Also verify the EvidenceLogger.error call is present for this path
check('route_c_empty' in design_source,
      '"route_c_empty" evidence event is logged for empty base_combos')

# ── Test 21: seed type coercion and range validation (P4 fix) ──
print('Test 21: seed type coercion and range validation')

# String seed → int
cfg_str_seed = _merge_config({'target_name': '3DAB', 'chain': 'B'}, {'seed': '42'})
check(cfg_str_seed['seed'] == 42, f'string seed coerced to int, got {type(cfg_str_seed["seed"]).__name__}={cfg_str_seed["seed"]}')
check(isinstance(cfg_str_seed['seed'], int), f'seed must be int, got {type(cfg_str_seed["seed"]).__name__}')

# Float seed → int (whole-number float tolerated)
cfg_float_seed = _merge_config({'target_name': '3DAB', 'chain': 'B'}, {'seed': 42.0})
check(cfg_float_seed['seed'] == 42, f'float seed coerced to int, got {cfg_float_seed["seed"]}')

# Fractional float seed must be rejected (P1-2)
check_raises(ValueError,
    lambda: _merge_config({'target_name': '3DAB', 'chain': 'B'}, {'seed': 42.9}),
    'fractional float seed rejected')

# Negative seed → ValueError
check_raises(ValueError,
    lambda: _merge_config({'target_name': '3DAB', 'chain': 'B'}, {'seed': -1}),
    'negative seed must raise ValueError')

# Seed above int32 max → ValueError
check_raises(ValueError,
    lambda: _merge_config({'target_name': '3DAB', 'chain': 'B'}, {'seed': 2**31}),
    'seed above 2^31-1 must raise ValueError')

# Seed=0 still preserved (edge case from Test 6)
cfg_zero = _merge_config({'target_name': '3DAB', 'chain': 'B'}, {'seed': 0})
check(cfg_zero['seed'] == 0, f'seed=0 preserved, got {cfg_zero["seed"]}')

os.unlink(target_fixture.name)

# ── Summary ──
print()
if failures:
    print(f'FAILED: {len(failures)} test(s)')
    for f in failures:
        print(f'  - {f}')
    sys.exit(1)
else:
    print('ALL 21 TEST GROUPS PASSED')
