"""Unit tests for design.py pure functions (no GPU/external tools needed)"""
import sys, os, json, tempfile, hashlib

# ── Stubs for data_layer ──
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

# ── Load design.py with stubs ──
import __main__
src = open('agents/design.py', encoding='utf-8').read()
src = src.replace(
    'from data_layer import EvidenceLogger, CandidateIndex, State, file_hash',
    '# data_layer stubs injected by test_design.py'
)
# Prevent CLI execution
src = src.replace('if __name__ == "__main__":', 'if False and __name__ == "__main__":')
exec(src)

failures = []

def check(cond, msg):
    if not cond:
        failures.append(msg)
        print(f'  FAIL: {msg}')
    else:
        print(f'  ok: {msg}')

# ── Test 1: _validate_sequence ──
print('Test 1: _validate_sequence')
check(_validate_sequence('ACDEFGHI'), 'basic valid seq')
check(_validate_sequence('CACDEFGHIC'), 'Cys flanked seq')
check(not _validate_sequence(''), 'empty rejected')
check(not _validate_sequence('AAAAA'), 'too short (5) rejected')
check(not _validate_sequence('A' * 21), 'too long (21) rejected')
check(not _validate_sequence('ACDXEFG'), 'nonstandard X rejected')

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
c1 = _next_candidate_id()
c2 = _next_candidate_id()
check(c1 == 'C0001', f'c1={c1}')
check(c2 == 'C0002', f'c2={c2}')

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
check(cfg['target_name'] == '3DAB', f'target_name={cfg["target_name"]}')
check(cfg['chain'] == 'B', f'chain={cfg["chain"]}')
check(cfg['n'] == 50, f'n={cfg["n"]}')
check(cfg['lengths'] == [8, 9], f'lengths={cfg["lengths"]}')
check(cfg['seed'] is not None, 'seed auto-resolved')

# ── Test 6: seed=None -> auto, seed=0 preserved ──
print('Test 6: seed edge cases')
cfg2 = _merge_config(None, {'seed': None})
check(cfg2['seed'] is not None, 'seed=None auto-resolved')
cfg3 = _merge_config(None, {'seed': 0})
check(cfg3['seed'] == 0, f'seed=0 preserved, got {cfg3["seed"]}')

# ── Test 7: threshold_filter ──
print('Test 7: threshold_filter')
cands = [
    {'ipsae_mdm2': 0.7, 'ipsae_mdmx': 0.6, 'hotspot_cov_mdm2': 0.8, 'hotspot_cov_mdmx': 0.8},
    {'ipsae_mdm2': 0.5, 'ipsae_mdmx': 0.6, 'hotspot_cov_mdm2': 0.8, 'hotspot_cov_mdmx': 0.8},
    {'ipsae_mdm2': 0.7, 'ipsae_mdmx': 0.4, 'hotspot_cov_mdm2': 0.8, 'hotspot_cov_mdmx': 0.8},
]
passed = threshold_filter(cands, {})
check(len(passed) == 1, f'1 passed, got {len(passed)}')

# ── Test 8: pareto_front ──
print('Test 8: pareto_front')
cands = [
    {'ipsae_mdm2': 0.7, 'ipsae_mdmx': 0.7},
    {'ipsae_mdm2': 0.8, 'ipsae_mdmx': 0.6},
    {'ipsae_mdm2': 0.6, 'ipsae_mdmx': 0.8},
    {'ipsae_mdm2': 0.5, 'ipsae_mdmx': 0.5},
]
front = pareto_front(cands)
check(len(front) == 3, f'3 on front, got {len(front)}')
front_pairs = [(c['ipsae_mdm2'], c['ipsae_mdmx']) for c in front]
check((0.5, 0.5) not in front_pairs, 'dominated not on front')

# ── Test 9: _ring_closure_check ──
print('Test 9: _ring_closure_check')
# Normal case: close CA atoms
pdb_close = (
    'ATOM      1  N   ALA A   1       1.000   2.000   3.000  1.00  0.00           N  \n'
    'ATOM      2  CA  ALA A   1       1.500   2.500   3.500  1.00  0.00           C  \n'
    'ATOM      3  N   ALA A  12       5.000   6.000   7.000  1.00  0.00           N  \n'
    'ATOM      4  CA  ALA A  12       5.500   6.500   7.500  1.00  0.00           C  \n'
)
tmp1 = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
tmp1.write(pdb_close)
tmp1.close()
rc = _ring_closure_check(tmp1.name)
check(rc['pass'] is True, f'close CA should pass, got {rc}')
os.unlink(tmp1.name)

# Multi-model: should only read first MODEL
pdb_multi = (
    'ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00           C  \n'
    'ENDMDL\n'
    'ATOM    100  CA  ALA A   1      99.000  99.000  99.000  1.00  0.00           C  \n'
    'ATOM    101  CA  ALA A  12      99.000  99.000  99.000  1.00  0.00           C  \n'
)
tmp2 = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
tmp2.write(pdb_multi)
tmp2.close()
rc2 = _ring_closure_check(tmp2.name)
check(rc2['pass'] is False, f'multi-model (1 CA) should fail, got {rc2}')
check(rc2.get('n_ca') == 1, f'should report n_ca=1, got {rc2}')
os.unlink(tmp2.name)

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
import random
random.seed(42)
orig = [('LTFLEYWAAQSL', 'head-to-tail_amide')]
expanded = list(orig)
seen = set(s for s, _ in orig)
n, attempts = 10, 0
while len(expanded) < n and attempts < n * 10:
    attempts += 1
    seq, desc = random.choice(orig)
    pos = random.choice([3, 5, 8, 10, 12])
    aa = random.choice('ACDEFGHIKLMNPQRSTVWY')
    off = 1 if seq and seq[0] == 'C' else 0
    ix = off + min(pos, len(seq) - 1)
    mut = seq[:ix] + aa + seq[ix + 1:]
    if _validate_sequence(mut) and mut not in seen:
        seen.add(mut)
        expanded.append((mut, f'{desc},mut:{pos}={aa}'))
check(len(expanded) == n, f'expected {n}, got {len(expanded)}')
check(all(_validate_sequence(s) for s, _ in expanded), 'all sequences valid')

# ── Test 13: Route B empty binders guard ──
print('Test 13: Route B empty binders returns []')
State._data['known_dual_binders'] = []
result = design_motif_guided(target_spec={'target_name': '1YCR'}, design_config={'n': 10})
check(result == [], f'empty binders should return [], got {result}')
State._data['known_dual_binders'] = [
    {'name': 'PMI', 'sequence': 'TSFAEYWNLLSP', 'pmid': '34589387'}
]

# ── Test 14: _write_manifest ──
print('Test 14: _write_manifest + cyclization detection')
tmp_pdb = tempfile.NamedTemporaryFile(mode='w', suffix='.pdb', delete=False)
tmp_pdb.write(pdb_close)
tmp_pdb.close()
cfg_test = {'target_name': '1YCR', 'target_pdb': '/tmp/test.pdb', 'seed': 42}
m1 = _write_manifest('C0001', 'CACDEFGHIC', 'route_A_test', 'batch_1', tmp_pdb.name, cfg_test)
check(m1['cyclization_type'] == 'Cys-Cys_disulfide', f'Cys flanked -> {m1["cyclization_type"]}')
check(m1['backbone_pdb'] == '', 'no backbone -> empty string')
check(len(m1['refold_pdb_hash']) > 0, 'refold hash present')
check('pass' in m1['ring_closure'], 'ring_closure has pass')
# With cyclization arg
m2 = _write_manifest('C0002', 'ACDEFGHI', 'route_C_test', 'batch_2', tmp_pdb.name, cfg_test, cyclization='Cys-Cys_disulfide,linker=GGGGS')
check(m2['cyclization_type'] == 'Cys-Cys_disulfide,linker=GGGGS', f'custom cyclization -> {m2["cyclization_type"]}')
os.unlink(tmp_pdb.name)

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

# ── Summary ──
print()
if failures:
    print(f'FAILED: {len(failures)} test(s)')
    for f in failures:
        print(f'  - {f}')
    sys.exit(1)
else:
    print('ALL 15 TESTS PASSED')
