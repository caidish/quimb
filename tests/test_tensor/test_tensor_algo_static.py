import pytest

import numpy as np
from numpy.testing import assert_allclose

from quimb import (
    ham_heis,
    expec,
    seigsys,
    plus,
    is_eigenvector,
    eigsys,
)

from quimb.tensor import (
    align_TN_1D,
    MPS_rand_state,
    MPS_product_state,
    MPS_computational_state,
    MPS_neel_state,
    MPO_ham_ising,
    MPO_ham_XY,
    MPO_ham_heis,
    MPO_ham_mbl,
    MovingEnvironment,
    DMRG1,
    DMRG2,
    DMRGX,
)


class TestMovingEnvironment:
    def test_bsz1_start_left(self):
        tn = MPS_rand_state(6, bond_dim=7)
        env = MovingEnvironment(tn, n=6, start='left', bsz=1)
        assert env.pos == 0
        assert len(env().tensors) == 2
        env.move_right()
        assert env.pos == 1
        assert len(env().tensors) == 3
        env.move_right()
        assert env.pos == 2
        assert len(env().tensors) == 3
        env.move_to(5)
        assert env.pos == 5
        assert len(env().tensors) == 2

    def test_bsz1_start_right(self):
        tn = MPS_rand_state(6, bond_dim=7)
        env = MovingEnvironment(tn, n=6, start='right', bsz=1)
        assert env.pos == 5
        assert len(env().tensors) == 2
        env.move_left()
        assert env.pos == 4
        assert len(env().tensors) == 3
        env.move_left()
        assert env.pos == 3
        assert len(env().tensors) == 3
        env.move_to(0)
        assert env.pos == 0
        assert len(env().tensors) == 2

    def test_bsz2_start_left(self):
        tn = MPS_rand_state(6, bond_dim=7)
        env = MovingEnvironment(tn, n=6, start='left', bsz=2)
        assert len(env().tensors) == 3
        env.move_right()
        assert len(env().tensors) == 4
        env.move_right()
        assert len(env().tensors) == 4
        with pytest.raises(ValueError):
            env.move_to(5)
        env.move_to(4)
        assert env.pos == 4
        assert len(env().tensors) == 3

    def test_bsz2_start_right(self):
        tn = MPS_rand_state(6, bond_dim=7)
        env = MovingEnvironment(tn, n=6, start='right', bsz=2)
        assert env.pos == 4
        assert len(env().tensors) == 3
        env.move_left()
        assert env.pos == 3
        assert len(env().tensors) == 4
        env.move_left()
        assert env.pos == 2
        assert len(env().tensors) == 4
        with pytest.raises(ValueError):
            env.move_to(-1)
        env.move_to(0)
        assert env.pos == 0
        assert len(env().tensors) == 3


class TestDMRG1:

    def test_single_explicit_sweep(self):
        h = MPO_ham_heis(5)
        dmrg = DMRG1(h, bond_dims=3)
        assert dmrg._k.site[0].dtype == float

        energy_tn = (dmrg._b | dmrg.ham | dmrg._k)

        e0 = energy_tn ^ ...
        assert abs(e0.imag) < 1e-13

        de1 = dmrg.sweep_right()
        e1 = energy_tn ^ ...
        assert_allclose(de1, e1)
        assert abs(e1.imag) < 1e-13

        de2 = dmrg.sweep_right()
        e2 = energy_tn ^ ...
        assert_allclose(de2, e2)
        assert abs(e2.imag) < 1e-13

        # state is already left canonized after right sweep
        de3 = dmrg.sweep_left(canonize=False)
        e3 = energy_tn ^ ...
        assert_allclose(de3, e3)
        assert abs(e2.imag) < 1e-13

        de4 = dmrg.sweep_left()
        e4 = energy_tn ^ ...
        assert_allclose(de4, e4)
        assert abs(e2.imag) < 1e-13

        # test still normalized
        assert dmrg._k.site[0].dtype == float
        align_TN_1D(dmrg._k, dmrg._b, inplace=True)
        assert_allclose(abs(dmrg._b @ dmrg._k), 1)

        assert e1.real < e0.real
        assert e2.real < e1.real
        assert e3.real < e2.real
        assert e4.real < e3.real

    @pytest.mark.parametrize("dense", [False, True])
    @pytest.mark.parametrize("MPO_ham", [MPO_ham_XY, MPO_ham_heis])
    def test_ground_state_matches(self, dense, MPO_ham):
        h = MPO_ham(6)
        dmrg = DMRG1(h, bond_dims=8)
        dmrg.opts['eff_eig_dense'] = dense
        assert dmrg.solve()
        eff_e, mps_gs = dmrg.energy, dmrg.state
        mps_gs_dense = mps_gs.to_dense()

        assert_allclose(mps_gs_dense.H @ mps_gs_dense, 1.0)

        h_dense = h.to_dense()

        # check against dense form
        actual_e, gs = seigsys(h_dense, k=1)
        assert_allclose(actual_e, eff_e)
        assert_allclose(abs(expec(mps_gs_dense, gs)), 1.0)

        # check against actual MPO_ham
        if MPO_ham is MPO_ham_XY:
            ham_dense = ham_heis(6, cyclic=False, j=(1.0, 1.0, 0.0))
        elif MPO_ham is MPO_ham_heis:
            ham_dense = ham_heis(6, cyclic=False)

        actual_e, gs = seigsys(ham_dense, k=1)
        assert_allclose(actual_e, eff_e)
        assert_allclose(abs(expec(mps_gs_dense, gs)), 1.0)

    def test_ising_and_MPS_product_state(self):
        h = MPO_ham_ising(6, bx=2.0, j=0.1)
        dmrg = DMRG1(h, bond_dims=8)
        assert dmrg.solve()
        eff_e, mps_gs = dmrg.energy, dmrg.state
        mps_gs_dense = mps_gs.to_dense()
        assert_allclose(mps_gs_dense.H @ mps_gs_dense, 1.0)

        # check against dense
        h_dense = h.to_dense()
        actual_e, gs = seigsys(h_dense, k=1)
        assert_allclose(actual_e, eff_e)
        assert_allclose(abs(expec(mps_gs_dense, gs)), 1.0)

        exp_gs = MPS_product_state([plus()] * 6)
        assert_allclose(abs(exp_gs.H @ mps_gs), 1.0, rtol=1e-3)


class TestDMRG2:
    @pytest.mark.parametrize("dense", [False, True])
    @pytest.mark.parametrize("MPO_ham", [MPO_ham_XY, MPO_ham_heis])
    def test_matches_exact(self, dense, MPO_ham):
        h = MPO_ham(6)
        dmrg = DMRG2(h, bond_dims=8)
        assert dmrg._k.site[0].dtype == float
        dmrg.opts['eff_eig_dense'] = dense
        assert dmrg.solve()

        # XXX: need to dispatch SLEPc seigsys on real input
        # assert dmrg._k.site[0].dtype == float

        eff_e, mps_gs = dmrg.energy, dmrg.state
        mps_gs_dense = mps_gs.to_dense()

        assert_allclose(mps_gs_dense.H @ mps_gs_dense, 1.0)

        h_dense = h.to_dense()

        # check against dense form
        actual_e, gs = seigsys(h_dense, k=1)
        assert_allclose(actual_e, eff_e)
        assert_allclose(abs(expec(mps_gs_dense, gs)), 1.0)

        # check against actual MPO_ham
        if MPO_ham is MPO_ham_XY:
            ham_dense = ham_heis(6, cyclic=False, j=(1.0, 1.0, 0.0))
        elif MPO_ham is MPO_ham_heis:
            ham_dense = ham_heis(6, cyclic=False)

        actual_e, gs = seigsys(ham_dense, k=1)
        assert_allclose(actual_e, eff_e)
        assert_allclose(abs(expec(mps_gs_dense, gs)), 1.0)


class TestDMRGX:

    def test_explicit_sweeps(self):
        # import pdb; pdb.set_trace()
        n = 8
        chi = 16
        ham = MPO_ham_mbl(n, dh=5, run=42)
        p0 = MPS_neel_state(n).expand_bond_dimension(chi)

        b0 = p0.H
        align_TN_1D(p0, ham, b0, inplace=True)
        en0 = np.asscalar(p0 & ham & b0 ^ ...)

        dmrgx = DMRGX(ham, p0, chi)
        dmrgx.sweep_right()
        en1 = dmrgx.sweep_left(canonize=False)
        assert en0 != en1

        dmrgx.sweep_right(canonize=False)
        en = dmrgx.sweep_right(canonize=True)

        # check normalized
        assert_allclose(dmrgx._k.H @ dmrgx._k, 1.0)

        k = dmrgx._k.to_dense()
        h = ham.to_dense()
        el, ev = eigsys(h)

        # check variance very low
        assert np.abs((k.H @ h @ h @ k) - (k.H @ h @ k)**2) < 1e-12

        # check exactly one eigenvalue matched well
        assert np.sum(np.abs(el - en) < 1e-12) == 1

        # check exactly one eigenvector is matched with high fidelity
        ovlps = (ev.H @ k).A**2
        big_ovlps = ovlps[ovlps > 1e-12]
        assert_allclose(big_ovlps, [1])

        # check fully
        assert is_eigenvector(k, h)

    def test_solve_bigger(self):
        n = 14
        chi = 16
        ham = MPO_ham_mbl(n, dh=8, run=42)
        p0 = MPS_computational_state('00110111000101')
        dmrgx = DMRGX(ham, p0, chi)
        assert dmrgx.solve(tol=1e-5, sweep_sequence='R')
        assert dmrgx.state.site[0].dtype == float
