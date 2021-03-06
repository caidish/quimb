###
API
###

Autogenerated summary of the contents of :py:mod:`quimb`.

Modules
=======

.. autosummary::
    :toctree: _autosummary

    quimb
    quimb.accel
    quimb.core
    quimb.calc
    quimb.evo
    quimb.utils

    quimb.gen
    quimb.gen.states
    quimb.gen.operators
    quimb.gen.rand

    quimb.linalg
    quimb.linalg.base_linalg
    quimb.linalg.numpy_linalg
    quimb.linalg.scipy_linalg
    quimb.linalg.slepc_linalg
    quimb.linalg.mpi_launcher
    quimb.linalg.approx_spectral


Functions
=========

accel
-----

.. currentmodule:: quimb.accel
.. autosummary::
    :toctree: _autosummary

    get_thread_pool
    par_reduce
    prod
    make_immutable
    matrixify
    realify_scalar
    realify
    zeroify
    isket
    isbra
    isop
    isvec
    issparse
    isherm
    ispos
    mul_dense
    mul
    dot_dense
    dot_csr_matvec
    par_dot_csr_matvec
    dot_sparse
    dot
    vdot
    rdot
    reshape_for_ldmul
    l_diag_dot_dense
    l_diag_dot_sparse
    ldmul
    reshape_for_rdmul
    r_diag_dot_dense
    r_diag_dot_sparse
    rdmul
    reshape_for_outer
    outer
    explt
    reshape_for_kron
    kron_dense
    kron_dense_big
    kron_sparse
    kron_dispatch


core
----

.. currentmodule:: quimb.core
.. autosummary::
    :toctree: _autosummary

    quimbify
    qu
    ket
    bra
    dop
    sparse
    sparse_matrix
    identity
    eye
    speye
    kron
    kronpow
    eyepad
    perm_eyepad
    permute
    trace
    tr
    itrace
    partial_trace
    ptr
    expectation
    expec
    overlap
    normalize
    nmlz
    chop
    dim_compress
    dim_map
    infer_size


calc
----
.. currentmodule:: quimb.calc
.. autosummary::
    :toctree: _autosummary

    fidelity
    purify
    entropy
    entropy_subsys
    mutual_information
    mutinf
    mutinf_subsys
    schmidt_gap
    tr_sqrt
    tr_sqrt_subsys
    partial_transpose
    negativity
    logarithmic_negativity
    logneg
    logneg_subsys
    concurrence
    one_way_classical_information
    quantum_discord
    trace_distance
    decomp
    pauli_decomp
    bell_decomp
    correlation
    pauli_correlations
    ent_cross_matrix
    is_degenerate
    is_eigenvector
    page_entropy

gen.states
----------

.. currentmodule:: quimb.gen.states
.. autosummary::
    :toctree: _autosummary

    basis_vec
    up
    zplus
    down
    zminus
    plus
    xplus
    minus
    xminus
    yplus
    yminus
    bloch_state
    bell_state
    singlet
    thermal_state
    neel_state
    singlet_pairs
    werner_state
    ghz_state
    w_state
    levi_civita
    perm_state
    graph_state_1d

gen.operators
-------------

.. currentmodule:: quimb.gen.operators
.. autosummary::
    :toctree: _autosummary

    spin_operator
    pauli
    controlled
    ham_heis
    ham_ising
    ham_j1j2
    cmbn
    uniq_perms
    zspin_projector
    swap

gen.rand
--------

.. currentmodule:: quimb.gen.rand
.. autosummary::
    :toctree: _autosummary

    rand_matrix
    rand_herm
    rand_pos
    rand_rho
    rand_ket
    rand_uni
    rand_haar_state
    gen_rand_haar_states
    rand_mix
    rand_product_state
    rand_matrix_product_state
    rand_seperable

evo
---

.. currentmodule:: quimb.evo
.. autosummary::
    :toctree: _autosummary

    QuEvo
    schrodinger_eq_ket
    schrodinger_eq_dop
    schrodinger_eq_dop_vectorized
    lindblad_eq
    lindblad_eq_vectorized


linalg
------

.. currentmodule:: quimb.linalg.base_linalg
.. autosummary::
    :toctree: _autosummary

    eigsys
    eigvals
    eigvecs
    seigsys
    seigvals
    seigvecs
    groundstate
    groundenergy
    bound_spectrum
    eigsys_window
    eigvals_window
    eigvecs_window
    svd
    svds
    norm_2
    norm_fro_dense
    norm_fro_sparse
    norm_trace_dense
    norm
    expm
    expm_multiply
    sqrtm

Specialised Linalg
------------------

.. currentmodule:: quimb.linalg.numpy_linalg
.. autosummary::
    :toctree: _autosummary

    eigsys_numpy
    eigvals_numpy
    sort_inds
    seigsys_numpy
    numpy_svds

.. currentmodule:: quimb.linalg.scipy_linalg
.. autosummary::
    :toctree: _autosummary

    seigsys_scipy
    scipy_svds


.. currentmodule:: quimb.linalg.slepc_linalg
.. autosummary::
    :toctree: _autosummary

    get_default_comm
    init_petsc_and_slepc
    get_petsc
    get_slepc
    slice_sparse_matrix_to_components
    convert_mat_to_petsc
    convert_vec_to_petsc
    new_petsc_vec
    gather_petsc_array
    seigsys_slepc
    svds_slepc
    mfn_multiply_slepc

MPI stuff
---------

.. currentmodule:: quimb.linalg.mpi_launcher
.. autosummary::
    :toctree: _autosummary

    CachedPoolWithShutdown
    GetMPIBeforeCall
    SpawnMPIProcessesFunc
    seigsys_slepc_mpi
    seigsys_slepc_spawn
    svds_slepc_mpi
    svds_slepc_spawn
    mfn_multiply_slepc_mpi
    mfn_multiply_slepc_spawn


Approximate spectral linear algebra
-----------------------------------

.. currentmodule:: quimb.linalg.approx_spectral
.. autosummary::
    :toctree: _autosummary

    HuskArray
    LazyPtrOperator
    LazyPtrPptOperator
    get_cntrct_inds_ptr_dot
    prepare_lazy_ptr_dot
    get_path_lazy_ptr_dot
    do_lazy_ptr_dot
    lazy_ptr_dot
    get_cntrct_inds_ptr_ppt_dot
    prepare_lazy_ptr_ppt_dot
    get_path_lazy_ptr_ppt_dot
    do_lazy_ptr_ppt_dot
    lazy_ptr_ppt_dot
    inner
    norm_fro
    construct_lanczos_tridiag
    lanczos_tridiag_eig
    calc_trace_fn_tridiag
    approx_spectral_function
    tr_abs_approx
    tr_exp_approx
    tr_sqrt_approx
    tr_xlogx_approx
    entropy_subsys_approx
    norm_ppt_subsys_approx
    logneg_subsys_approx
    negativity_subsys_approx


Tensor Networks
---------------

.. currentmodule:: quimb.tensor
.. autosummary::
    :toctree: _autosummary

    einsum
    einsum_path
    tensor_contract
    tensor_direct_product
    Tensor
    TensorNetwork
    rand_tensor
    MPS_rand_state
    MPS_product_state
    MPS_computational_state
    MPS_rand_computational_state
    MPS_neel_state
    MPS_zero_state
    MPO_identity
    MPO_identity_like
    MPO_zeros
    MPO_zeros_like
    MPO_rand
    MPO_rand_herm
    MPOSpinHam
    MPO_ham_ising
    MPO_ham_XY
    MPO_ham_heis
    MPO_ham_mbl
    MatrixProductState
    MatrixProductOperator
    align_TN_1D
    MovingEnvironment
    DMRG
    DMRG1
    DMRG2
    DMRGX
