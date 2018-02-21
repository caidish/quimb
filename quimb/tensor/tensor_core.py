"""Core tensor network tools.
"""
import os
import functools
import operator
import copy
import itertools
import string
import uuid
import re

from cytoolz import (
    unique,
    concat,
    frequencies,
    partition_all,
    merge_with,
)
import numpy as np
import scipy.sparse.linalg as spla
import scipy.linalg.interpolative as sli

from ..accel import prod, njit, realify_scalar
from ..linalg.base_linalg import norm_fro_dense
from ..utils import raise_cant_find_library_function, functions_equal

try:
    import opt_einsum
    einsum = opt_einsum.contract

    @functools.wraps(opt_einsum.contract_path)
    def einsum_path(*args, optimize='greedy', memory_limit=2**28, **kwargs):
        return opt_einsum.contract_path(
            *args, path=optimize, memory_limit=memory_limit, **kwargs)

    try:
        einsum_expression = opt_einsum.contract_expression
    except AttributeError:
        einsum_expression = raise_cant_find_library_function(
            "opt_einsum", "Or a more recent (github?) version is needed for "
            "caching tensor contractions.")

except ImportError:
    extra_msg = "Needed for optimized tensor contractions."
    einsum = einsum_expression = einsum_path = \
        raise_cant_find_library_function("opt_einsum", extra_msg)


@functools.lru_cache(1)
def tensorflow_session():
    import tensorflow as tf
    return tf.Session()


# --------------------------------------------------------------------------- #
#                                Tensor Funcs                                 #
# --------------------------------------------------------------------------- #

def set_join(sets):
    """Combine a sequence of sets.
    """
    new_set = set()
    for each_set in sets:
        new_set |= each_set
    return new_set


def _gen_output_inds(all_inds):
    """Generate the output, i.e. unnique, indices from the set ``inds``. Raise
    if any index found more than twice.
    """
    for ind, freq in frequencies(all_inds).items():
        if freq > 2:
            raise ValueError("The index {} appears more "
                             "than twice!".format(ind))
        elif freq == 1:
            yield ind


def _maybe_map_indices_to_alphabet(a_ix, i_ix, o_ix):
    """``einsum`` need characters a-z,A-Z or equivalent numbers.
    Do this early, and allow *any* index labels.

    Parameters
    ----------
    a_ix : set
        All of the input indices.
    i_ix : sequence of sequence
        The input indices per tensor.
    o_ix : list of int
        The output indices.

    Returns
    -------
    contract_str : str
        The string to feed to einsum/contract.
    """
    if any(i not in opt_einsum.parser.einsum_symbols_set for i in a_ix):
        # need to map inds to alphabet
        if len(a_ix) > len(opt_einsum.parser.einsum_symbols_set):
            raise ValueError("Too many indices to auto-optimize contraction "
                             "for at once, try setting a `structure` "
                             "or do a manual contraction order using tags.")

        amap = dict(zip(a_ix, opt_einsum.parser.einsum_symbols))
        in_str = ("".join(amap[i] for i in ix) for ix in i_ix)
        out_str = "".join(amap[o] for o in o_ix)

    else:
        in_str = ("".join(ix) for ix in i_ix)
        out_str = "".join(o_ix)

    return ",".join(in_str) + "->" + out_str


class HuskArray(np.ndarray):
    """Just an ndarray with only shape defined, so as to allow caching on shape
    alone.
    """

    def __init__(self, shape):
        self.shape = shape


@functools.lru_cache(4096)
def cached_einsum_expr(contract_str, *shapes):
    return einsum_expression(contract_str, *shapes,
                             memory_limit=2**28, optimize='greedy')


def tensor_contract(*tensors, output_inds=None, return_expression=False,
                    backend='numpy'):
    """Efficiently contract multiple tensors, combining their tags.

    Parameters
    ----------
    *tensors : sequence of Tensor
        The tensors to contract.
    output_inds : sequence
        If given, the desired order of output indices, else defaults to the
        order they occur in the input indices.
    return_expression : bool, optional
        If ``True``, return the expression that performs the contraction, for
        e.g. inspection of the order chosen.

    Returns
    -------
    scalar or Tensor
    """
    i_ix = tuple(t.inds for t in tensors)  # input indices per tensor
    a_ix = tuple(concat(i_ix))  # list of all input indices

    if output_inds is None:
        # sort output indices  by input order for efficiency and consistency
        o_ix = tuple(x for x in a_ix if x in [*_gen_output_inds(a_ix)])
    else:
        o_ix = output_inds

    # possibly map indices into the 0-52 range needed by einsum
    contract_str = _maybe_map_indices_to_alphabet([*unique(a_ix)], i_ix, o_ix)

    # perform the contraction
    expression = cached_einsum_expr(contract_str, *(t.shape for t in tensors))

    if return_expression:
        return expression

    if backend == 'numpy':
        o_array = expression(*(t.data for t in tensors))

    elif backend == 'tensorflow':
        sess = tensorflow_session()
        o_array = expression.tensorflow(
            *(t.data for t in tensors), session=sess)

    elif backend == 'cupy':
        o_array = expression.cupy(*(t.data for t in tensors))

    elif backend == 'theano':
        o_array = expression.theano(*(t.data for t in tensors))

    if not o_ix:
        if isinstance(o_array, np.ndarray):
            o_array = np.asscalar(o_array)
        return realify_scalar(o_array)

    # unison of all tags
    o_tags = set_join(t.tags for t in tensors)

    return Tensor(data=o_array, inds=o_ix, tags=o_tags)


# generate a random base to avoid collisions on difference processes ...
r_bs_str = str(uuid.uuid4())[:6]
# but then make the list orderable to help contraction caching
RAND_UUIDS = map("".join, itertools.product(string.hexdigits, repeat=7))


def rand_uuid(base=""):
    """Return a guaranteed unique, shortish identifier, optional appended
    to ``base``.

    Examples
    --------
    >>> rand_uuid()
    '_2e1dae1b'

    >>> rand_uuid('virt-bond')
    'virt-bond_bf342e68'
    """
    return base + "_" + r_bs_str + next(RAND_UUIDS)


@njit  # pragma: no cover
def _trim_singular_vals(s, cutoff, cutoff_mode):
    if cutoff_mode == 1:
        n_chi = np.sum(s > cutoff)
    elif cutoff_mode == 2:
        n_chi = np.sum(s > cutoff * s[0])
    elif cutoff_mode == 3:
        n_chi = s.size
        s2s = 0.0
        for i in range(s.size - 1, -1, -1):
            s2 = s[i]**2
            if not np.isnan(s2):
                s2s += s2
            if s2s > cutoff:
                break
            n_chi -= 1
    else:
        raise ValueError("``cutoff_mode`` not one of {1, 2, 3}.")

    return max(n_chi, 1)


@njit  # pragma: no cover
def _renorm_singular_vals(s, n_chi):
    """Find the normalization constant for ``s`` such that the new sum squared
    of the ``n_chi`` largest values equals the sum squared of all the old ones.
    """
    s_tot_keep = 0.0
    s_tot_lose = 0.0
    for i in range(s.size):
        s2 = s[i]**2
        if not np.isnan(s2):
            if i < n_chi:
                s_tot_keep += s2
            else:
                s_tot_lose += s2
    return ((s_tot_keep + s_tot_lose) / s_tot_keep)**0.5


@njit  # pragma: no cover
def _trim_and_renorm_SVD(U, s, V, cutoff, cutoff_mode, max_bond, absorb):
    if cutoff > 0.0:
        n_chi = _trim_singular_vals(s, cutoff, cutoff_mode)

        if max_bond > 0:
            n_chi = min(n_chi, max_bond)

        if n_chi < s.size:
            norm = _renorm_singular_vals(s, n_chi)
            s = s[:n_chi] * norm
            U = U[..., :n_chi]
            V = V[:n_chi, ...]

    if absorb == -1:
        U *= s.reshape((1, -1))
    elif absorb == 1:
        V *= s.reshape((-1, 1))
    else:
        s **= 0.5
        U *= s.reshape((1, -1))
        V *= s.reshape((-1, 1))

    return U, V


@njit  # pragma: no cover
def _array_split_svd(x, cutoff=-1.0, cutoff_mode=3, max_bond=-1, absorb=0):
    """SVD-decomposition.
    """
    U, s, V = np.linalg.svd(x, full_matrices=False)
    return _trim_and_renorm_SVD(U, s, V, cutoff, cutoff_mode, max_bond, absorb)


def _array_split_svdvals(x):
    """SVD-decomposition, but return singular values only.
    """
    return np.linalg.svd(x, full_matrices=False, compute_uv=False)


@njit  # pragma: no cover
def dag(x):
    """Hermitian conjugate.
    """
    return np.conjugate(x).T


@njit  # pragma: no cover
def _array_split_eig(x, cutoff=-1.0, cutoff_mode=3, max_bond=-1, absorb=0):
    """SVD-split via eigen-decomposition.
    """
    if x.shape[0] > x.shape[1]:
        # Get sU, V
        s2, V = np.linalg.eigh(dag(x) @ x)
        U = x @ V
        V = dag(V)

        # Check if want U, sV
        if absorb == 1:
            s = s2**0.5
            U /= s.reshape((1, -1))
            V *= s.reshape((-1, 1))
        # Or sqrt(s)U, sqrt(s)V
        elif absorb == 0:
            sqrts = s2**0.25
            U /= sqrts.reshape((1, -1))
            V *= sqrts.reshape((-1, 1))

    else:
        # Get U, sV
        s2, U = np.linalg.eigh(x @ dag(x))
        V = dag(U) @ x

        # Check if want sU, V
        if absorb == -1:
            s = s2**0.5
            U *= s.reshape((1, -1))
            V /= s.reshape((-1, 1))

        # Or sqrt(s)U, sqrt(s)V
        elif absorb == 0:
            sqrts = s2**0.25
            U *= sqrts.reshape((1, -1))
            V /= sqrts.reshape((-1, 1))

    # eigh produces ascending eigenvalue order -> slice opposite to svd
    if cutoff > 0.0:
        s = s2[::-1]**0.5
        n_chi = _trim_singular_vals(s, cutoff, cutoff_mode)

        if max_bond > 0:
            n_chi = min(n_chi, max_bond)

        if n_chi < s.size:
            norm = _renorm_singular_vals(s, n_chi)
            U = U[..., -n_chi:]
            V = V[-n_chi:, ...]

            if absorb == -1:
                U *= norm
            elif absorb == 0:
                U *= norm**0.5
                V *= norm**0.5
            else:
                V *= norm

    return U, V


@njit
def _array_split_svdvals_eig(x):
    """SVD-decomposition via eigen, but return singular values only.
    """
    if x.shape[0] > x.shape[1]:
        s2 = np.linalg.eigvalsh(dag(x) @ x)
    else:
        s2 = np.linalg.eigvalsh(x @ dag(x))
    return s2**0.5


def _array_split_svds(x, cutoff=0.0, cutoff_mode=3, max_bond=-1, absorb=0):
    """SVD-decomposition using iterative methods. Allows the
    computation of only a certain number of singular values, e.g. max_bond,
    from the get-go, and is thus more efficient. Can also supply LinearOperator.
    """
    d = min(x.shape)

    if max_bond < 0:
        k = sli.estimate_rank(x, eps=cutoff)
    else:
        k = min(d, max_bond)

    if k == d:
        return _array_split_svd(x, cutoff, cutoff_mode, max_bond, absorb)

    U, s, V = spla.svds(x, k=k)
    return _trim_and_renorm_SVD(U, s, V, cutoff, cutoff_mode, max_bond, absorb)


def _array_split_isvd(x, cutoff=0.0, cutoff_mode=3, max_bond=-1, absorb=0):
    """SVD-decomposition using interpolative matrix random methods. Allows the
    computation of only a certain number of singular values, e.g. max_bond,
    from the get-go, and is thus more efficient. Can also supply LinearOperator.
    """
    d = min(x.shape)

    if max_bond < 0:
        k = sli.estimate_rank(x, eps=cutoff)
    else:
        k = min(d, max_bond)

    if k == d:
        return _array_split_svd(x, cutoff, cutoff_mode, max_bond, absorb)

    U, s, V = sli.svd(x, k)
    V = V.conj().T

    return _trim_and_renorm_SVD(U, s, V, cutoff, cutoff_mode, max_bond, absorb)


@njit  # pragma: no cover
def _array_split_qr(x):
    """QR-decomposition.
    """
    Q, R = np.linalg.qr(x)
    return Q, R


@njit  # pragma: no cover
def _array_split_lq(x):
    """LQ-decomposition.
    """
    Q, L = np.linalg.qr(x.T)
    return L.T, Q.T


def tensor_split(T, left_inds, method='svd', max_bond=None, absorb='both',
                 cutoff=1e-10, cutoff_mode='sum2', get=None):
    """Decompose this tensor into two tensors.

    Parameters
    ----------
    T : Tensor
        The tensor to split.
    left_inds : sequence of hashable
        The sequence of inds, which ``tensor`` should already have, to split to
        the 'left'.
    method : {'svd', 'eig', 'isvd', 'svds', qr', 'lq'}, optional
        How to split the tensor.
    cutoff : float, optional
        The threshold below which to discard singular values, only applies to
        ``method='svd'`` and ``method='eig'``.
    cutoff_mode : {'sum2', 'rel', 'abs'}
        Method with which to apply the cutoff threshold:

            - 'sum2': sum squared of values discarded must be ``< cutoff``.
            - 'rel': values less than ``cutoff * s[0]`` discarded.
            - 'abs': values less than ``cutoff`` discarded.

    max_bond: None or int
        If integer, the maxmimum number of singular values to keep, regardless
        of ``cutoff``.
    absorb = {'both', 'left', 'right'}
        Whether to absorb the singular values into both, the left or right
        unitary matrix respectively.
    get : {None, 'arrays', 'tensors', 'values'}
        If given, what to return instead of a TN describing the split.

    Returns
    -------
    TensorNetwork or (Tensor, Tensor) or (array, array) or 1D-array
        Respectively if get={None, 'tensors', 'arrays', 'values'}.
    """
    left_inds = tuple(left_inds)
    right_inds = tuple(x for x in T.inds if x not in left_inds)

    TT = T.transpose(*left_inds, *right_inds)

    left_dims = TT.shape[:len(left_inds)]
    right_dims = TT.shape[len(left_inds):]

    array = TT.data.reshape(prod(left_dims), prod(right_dims))

    if get == 'values':
        return {'svd': _array_split_svdvals,
                'eig': _array_split_svdvals_eig}[method](array)

    opts = {}
    if method not in ('qr', 'lq'):
        # Convert defaults and settings to numeric type for numba funcs
        opts['cutoff'] = {None: -1.0}.get(cutoff, cutoff)
        opts['absorb'] = {'left': -1, 'both': 0, 'right': 1}[absorb]
        opts['max_bond'] = {None: -1}.get(max_bond, max_bond)
        opts['cutoff_mode'] = {'abs': 1, 'rel': 2, 'sum2': 3}[cutoff_mode]

    left, right = {'svd': _array_split_svd,
                   'eig': _array_split_eig,
                   'isvd': _array_split_isvd,
                   'svds': _array_split_svds,
                   'qr': _array_split_qr,
                   'lq': _array_split_lq}[method](array, **opts)

    left = left.reshape(*left_dims, -1)
    right = right.reshape(-1, *right_dims)

    if get == 'arrays':
        return left, right

    bond_ind = rand_uuid()

    Tl = Tensor(data=left, inds=(*left_inds, bond_ind), tags=T.tags)
    Tr = Tensor(data=right, inds=(bond_ind, *right_inds), tags=T.tags)

    if get == 'tensors':
        return Tl, Tr

    return TensorNetwork((Tl, Tr), check_collisions=False)


def tensor_compress_bond(T1, T2, **compress_opts):
    """Inplace compress between the two single tensors. It follows the
    following steps to minimize the size of SVD performed::

        a)|   |        b)|            |        c)|       |
        --1---2--  ->  --1L~~1R--2L~~2R--  ->  --1L~~M~~2R--
          |   |          |   ......   |          |       |
         <*> <*>              >  <                  <*>

                  d)|            |        e)|     |
              ->  --1L~~ML~~MR~~2R--  ->  --1C~~~2C--
                    |....    ....|          |     |
                     >  <    >  <              ^compressed bond
    """
    s_ix, t1_ix = T1.filter_shared_inds(T2)

    if not s_ix:
        raise ValueError("The tensors specified don't share an bond.")
    # a) -> b)
    T1_L, T1_R = T1.split(left_inds=t1_ix, get='tensors',
                          absorb='right', **compress_opts)
    T2_L, T2_R = T2.split(left_inds=s_ix, get='tensors',
                          absorb='left', **compress_opts)
    # b) -> c)
    M = (T1_R @ T2_L)
    M.drop_tags()
    # c) -> d)
    M_L, M_R = M.split(left_inds=T1_L.shared_inds(M), get='tensors',
                       absorb='both', **compress_opts)

    # make sure old bond being used
    ns_ix, = M_L.shared_inds(M_R)
    M_L.reindex({ns_ix: s_ix[0]}, inplace=True)
    M_R.reindex({ns_ix: s_ix[0]}, inplace=True)

    # d) -> e)
    T1C = T1_L.contract(M_L, output_inds=T1.inds)
    T2C = M_R.contract(T2_R, output_inds=T2.inds)

    # update with the new compressed data
    T1.modify(data=T1C.data)
    T2.modify(data=T2C.data)


def tensor_add_bond(T1, T2):
    """Inplace addition of a dummy bond between ``T1`` and ``T2``.
    """
    bnd = rand_uuid()
    T1.modify(data=T1.data[..., np.newaxis], inds=(*T1.inds, bnd))
    T2.modify(data=T2.data[..., np.newaxis], inds=(*T2.inds, bnd))


def array_direct_product(X, Y, sum_axes=()):
    """Direct product of two numpy.ndarrays.

    Parameters
    ----------
    X : numpy.ndarray
        First tensor.
    Y : numpy.ndarray
        Second tensor, same shape as ``X``.
    sum_axes : sequence of int
        Axes to sum over rather than direct product, e.g. physical indices when
        adding tensor networks.

    Returns
    -------
    Z : numpy.ndarray
        Same shape as ``X`` and ``Y``, but with every dimension the sum of the
        two respective dimensions, unless it is included in ``sum_axes``.
    """

    if isinstance(sum_axes, int):
        sum_axes = (sum_axes,)

    # parse the intermediate and final shape doubling the size of any axes that
    #   is not to be summed, and preparing slices with which to add X, Y.
    final_shape = []
    selectorX = []
    selectorY = []

    for i, (d1, d2) in enumerate(zip(X.shape, Y.shape)):
        if i not in sum_axes:
            final_shape.append(d1 + d2)
            selectorX.append(slice(0, d1))
            selectorY.append(slice(d1, None))
        else:
            if d1 != d2:
                raise ValueError("Can only add sum tensor indices of the same "
                                 "size.")
            final_shape.append(d1)
            selectorX.append(slice(None))
            selectorY.append(slice(None))

    new_type = np.find_common_type((X.dtype, Y.dtype), ())
    Z = np.zeros(final_shape, dtype=new_type)

    # Add tensors to the diagonals
    Z[selectorX] += X
    Z[selectorY] += Y

    return Z


def tensor_direct_product(T1, T2, sum_inds=(), inplace=False):
    """Direct product of two Tensors. Any axes included in ``sum_inds`` must be
    the same size and will be summed over rather than concatenated. Summing
    over contractions of TensorNetworks equates to contracting a TensorNetwork
    made of direct products of each set of tensors. I.e. (a1 @ b1) + (a2 @ b2)
    == (a1 (+) a2) @ (b1 (+) b2).

    Parameters
    ----------
    T1 : Tensor
        The first tensor.
    T2 : Tensor
        The second tensor, with matching indices and dimensions to ``T1``.
    sum_inds : sequence of hashable, optional
        Axes to sum over rather than combine, e.g. physical indices when
        adding tensor networks.
    inplace : bool, optional
        Whether to modify ``T1`` inplace.

    Returns
    -------
    Tensor
        Like ``T1``, but with each dimension doubled in size if not
        in ``sum_inds``.
    """
    if isinstance(sum_inds, (str, int)):
        sum_inds = (sum_inds,)

    if T2.inds != T1.inds:
        T2 = T2.transpose(*T1.inds)

    sum_axes = tuple(T1.inds.index(ind) for ind in sum_inds)

    if inplace:
        new_T = T1
    else:
        new_T = T1.copy()

    # XXX: add T2s tags?
    new_T.modify(data=array_direct_product(T1.data, T2.data,
                                           sum_axes=sum_axes))
    return new_T


# --------------------------------------------------------------------------- #
#                                Tensor Class                                 #
# --------------------------------------------------------------------------- #

class Tensor(object):
    """A labelled, tagged ndarray.

    Parameters
    ----------
    data : numpy.ndarray
        The n-dimensions data.
    inds : sequence of hashable
        The index labels for each dimension.
    tags : sequence of hashable
        Tags with which to select and filter from multiple tensors.
    """

    def __init__(self, data, inds, tags=None):
        # Short circuit for copying Tensors
        if isinstance(data, Tensor):
            self._data = data.data
            self.inds = data.inds
            self.tags = data.tags.copy()
            return

        self._data = np.asarray(data)
        self.inds = tuple(inds)

        if self._data.ndim != len(self.inds):
            raise ValueError(
                "Wrong number of inds, {}, supplied for array"
                " of shape {}.".format(self.inds, self._data.shape))

        self.tags = (set() if tags is None else
                     {tags} if isinstance(tags, str) else
                     set(tags))

    def copy(self, deep=False):
        """
        """
        if deep:
            return copy.deepcopy(self)
        else:
            return Tensor(self, None)

    def get_data(self):
        return self._data

    def set_data(self, data):
        if data.size != self.size:
            raise ValueError("Cannot set - array size does not match Tensor.")
        elif data.shape != self.shape:
            self._data = data.reshape(self.shape)
        else:
            self._data = data

    data = property(get_data, set_data,
                    doc="The numpy.ndarray with this Tensors' numeric data.")

    def modify(self, data=None, inds=None, tags=None):
        """Overwrite the data of this tensor.
        """
        if data is not None:
            self._data = np.asarray(data)
        if inds is not None:
            self.inds = inds
        if tags is not None:
            self.tags = tags
        if len(self.inds) != self.data.ndim:
            raise ValueError("Mismatch between number of data dimensions and "
                             "number of indices supplied.")

    def conj(self, inplace=False):
        """Conjugate this tensors data (does nothing to indices).
        """
        if inplace:
            self._data = self._data.conj()
            return self
        else:
            return Tensor(self._data.conj(), self.inds, self.tags)

    @property
    def H(self):
        """Conjugate this tensors data (does nothing to indices).
        """
        return self.conj()

    @property
    def shape(self):
        return self._data.shape

    @property
    def ndim(self):
        return self._data.ndim

    @property
    def size(self):
        return self._data.size

    @property
    def dtype(self):
        return self._data.dtype

    def ind_size(self, ind):
        """Return the size of dimension corresponding to ``ind``.
        """
        return self.shape[self.inds.index(ind)]

    def bond_size(self, other):
        """Get the total size of the shared index(es) with ``other``.
        """
        return prod(self.ind_size(i) for i in self.shared_inds(other))

    def inner_inds(self):
        """
        """
        ind_freqs = frequencies(self.inds)
        return tuple(i for i in self.inds if ind_freqs[i] == 2)

    def transpose(self, *output_inds, inplace=False):
        """Transpose this tensor.

        Parameters
        ----------
        output_inds : sequence of hashable
            The desired output sequence of indices.

        Returns
        -------
        Tensor
        """
        if inplace:
            tn = self
        else:
            tn = self.copy()

        output_inds = tuple(output_inds)  # need to re-use this.

        if set(tn.inds) != set(output_inds):
            raise ValueError("'output_inds' must be permutation of the "
                             "current tensor indices, but {} != {}"
                             .format(set(tn.inds), set(output_inds)))

        current_ind_map = {ind: i for i, ind in enumerate(tn.inds)}
        out_shape = tuple(current_ind_map[i] for i in output_inds)

        tn.modify(data=tn.data.transpose(*out_shape), inds=output_inds)
        return tn

    @functools.wraps(tensor_contract)
    def contract(self, *others, output_inds=None, **opts):
        return tensor_contract(self, *others, output_inds=output_inds, **opts)

    @functools.wraps(tensor_direct_product)
    def direct_product(self, other, sum_inds=(), inplace=False):
        return tensor_direct_product(
            self, other, sum_inds=sum_inds, inplace=inplace)

    @functools.wraps(tensor_split)
    def split(self, *args, **kwargs):
        return tensor_split(self, *args, **kwargs)

    def singular_values(self, left_inds, method='svd'):
        """Return the singular values associated with splitting this tensor
        according to ``left_inds``.

        Parameters
        ----------
        left_inds : sequence of hashable
            A subset of this tensors indices that defines 'left'.
        method : {'svd', 'eig'}
            Whether to use the SVD or eigenvalue decomposition to get the
            singular values.

        Returns
        -------
        1d-array
            The singular values.
        """
        return self.split(left_inds=left_inds, method=method, get='values')

    def entropy(self, left_inds, method='svd'):
        """Return the entropy associated with splitting this tensor
        according to ``left_inds``.

        Parameters
        ----------
        left_inds : sequence of hashable
            A subset of this tensors indices that defines 'left'.
        method : {'svd', 'eig'}
            Whether to use the SVD or eigenvalue decomposition to get the
            singular values.

        Returns
        -------
        float
        """
        el = self.singular_values(left_inds=left_inds, method=method)**2
        el = el[el > 0.0]
        return np.sum(-el * np.log2(el))

    def reindex(self, index_map, inplace=False):
        """Rename the indices of this tensor, optionally in-place.

        Parameters
        ----------
        index_map : dict-like
            Mapping of pairs ``{old_ind: new_ind, ...}``.
        inplace : bool, optional
            If ``False`` (the default), a copy of this tensor with the changed
            inds will be returned.
        """
        if inplace:
            new = self
        else:
            new = self.copy()

        new.inds = tuple(index_map.get(ind, ind) for ind in new.inds)
        return new

    def fuse(self, fuse_map, inplace=False):
        """Combine groups of indices into single indices.

        Parameters
        ----------
        fuse_map : dict_like or sequence of tuples.
            Mapping like: ``{new_ind: sequence of existing inds, ...}`` or an
            ordered mapping like ``[(new_ind_1, old_inds_1), ...]`` in which
            case the output tensor's fused inds will be ordered. In both cases
            the new indices are created at the beginning of the tensor's shape.

        Returns
        -------
        Tensor
            The transposed, reshaped and re-labeled tensor.
        """
        if inplace:
            tn = self
        else:
            tn = self.copy()

        if isinstance(fuse_map, dict):
            new_fused_inds, fused_inds = zip(*fuse_map.items())
        else:
            new_fused_inds, fused_inds = zip(*fuse_map)

        unfused_inds = tuple(
            i for i in tn.inds if not
            any(i in fs for fs in fused_inds))

        # transpose tensor to bring groups of fused inds to the beginning
        tn.transpose(*concat(fused_inds), *unfused_inds, inplace=True)

        # for each set of fused dims, group into product, then add remaining
        dims = iter(tn.shape)
        dims = [prod(next(dims) for _ in fs) for fs in fused_inds] + list(dims)

        # create new tensor with new + remaining indices
        tn.modify(data=tn.data.reshape(*dims),
                  inds=(*new_fused_inds, *unfused_inds))
        return tn

    def squeeze(self, inplace=False):
        """Drop any singlet dimensions from this tensor.
        """
        t = self if inplace else self.copy()
        new_shape, new_inds = zip(
            *((d, i) for d, i in zip(self.shape, self.inds) if d > 1))
        if len(t.inds) != len(new_inds):
            t.modify(data=t.data.reshape(new_shape), inds=new_inds)
        return t

    def norm(self):
        """Frobenius norm of this tensor.
        """
        return norm_fro_dense(self.data.reshape(-1))

    def almost_equals(self, other, **kwargs):
        """Check if this tensor is almost the same as another.
        """
        same_inds = (set(self.inds) == set(other.inds))
        if not same_inds:
            return False
        otherT = other.transpose(*self.inds)
        return np.allclose(self.data, otherT.data, **kwargs)

    def drop_tags(self, tags=None):
        """Drop certain tags, defaulting to all, from this tensor.
        """
        if tags is None:
            self.tags = set()
        elif isinstance(tags, str):
            self.tags.discard(tags)
        else:
            for tag in tags:
                self.tags.discard(tag)

    def shared_inds(self, other):
        """Return a tuple of the shared indices between this tensor
        and ``other``.
        """
        return tuple(i for i in self.inds if i in other.inds)

    def filter_shared_inds(self, other):
        """Sort this tensor's indices into a list of those that it shares and
        doesn't share with another tensor.
        """
        shared = []
        unshared = []
        for i in self.inds:
            if i in other.inds:
                shared.append(i)
            else:
                unshared.append(i)
        return shared, unshared

    def __and__(self, other):
        """Combine with another ``Tensor`` or ``TensorNetwork`` into a new
        ``TensorNetwork``.
        """
        return TensorNetwork((self, other))

    def __matmul__(self, other):
        """Explicitly contract with another tensor.
        """
        return tensor_contract(self, other)

    def graph(self, *args, **kwargs):
        """Plot a graph of this tensor and its indices.
        """
        TensorNetwork((self,)).graph(*args, **kwargs)

    def __repr__(self):
        return "Tensor(shape={}, inds={}, tags={})".format(
            self.data.shape,
            self.inds,
            self.tags)


# ------------------------- Add ufunc like methods -------------------------- #

def _make_promote_array_func(op, meth_name):

    @functools.wraps(getattr(np.ndarray, meth_name))
    def _promote_array_func(self, other):
        """Use standard array func, but make sure Tensor inds match.
        """
        if isinstance(other, Tensor):

            if set(self.inds) != set(other.inds):
                raise ValueError(
                    "The indicies of these two tensors do not "
                    "match: {} != {}".format(self.inds, other.inds))

            otherT = other.transpose(*self.inds)

            return Tensor(
                data=op(self.data, otherT.data), inds=self.inds,
                tags=self.tags | other.tags)
        else:
            return Tensor(data=op(self.data, other),
                          inds=self.inds, tags=self.tags)

    return _promote_array_func


for meth_name, op in [('__add__', operator.__add__),
                      ('__sub__', operator.__sub__),
                      ('__mul__', operator.__mul__),
                      ('__pow__', operator.__pow__),
                      ('__truediv__', operator.__truediv__)]:
    setattr(Tensor, meth_name, _make_promote_array_func(op, meth_name))


def _make_rhand_array_promote_func(op, meth_name):

    @functools.wraps(getattr(np.ndarray, meth_name))
    def _rhand_array_promote_func(self, other):
        """Right hand operations -- no need to check ind equality first.
        """
        return Tensor(data=op(other, self.data),
                      inds=self.inds, tags=self.tags)

    return _rhand_array_promote_func


for meth_name, op in [('__radd__', operator.__add__),
                      ('__rsub__', operator.__sub__),
                      ('__rmul__', operator.__mul__),
                      ('__rpow__', operator.__pow__),
                      ('__rtruediv__', operator.__truediv__)]:
    setattr(Tensor, meth_name, _make_rhand_array_promote_func(op, meth_name))


class TNLinearOperator(spla.LinearOperator):
    """Get a linear operator - something that replicates the matrix-vector
    operation - for an arbitrary *uncontracted* TensorNetwork, e.g:

         / | | \   -> upper_inds
        L--H-H--R
         \ | | /   -> lower_inds

    This can then be supplied to scipy's sparse linear algebra routines. This
    currently assumes the effective operator is square.

    Parameters
    ----------
    tns : sequence of Tensors or TensorNetwork
        A representation of the hamiltonian
    upper_inds : sequence of hashable
        The upper inds of the effective hamiltonian network.
    lower_inds : sequence of hashable
        The lower inds of the effective hamiltonian network. These should be
        ordered the same way as ``upper_inds``.
    dims : tuple of int, or None
        The dimensions corresponding to the inds. Will figure out if None.
    """

    def __init__(self, tns, upper_inds, lower_inds, udims=None, ldims=None):

        if isinstance(tns, TensorNetwork):
            self._tensors = tns.tensors

            if udims is None or ldims is None:
                ix_sz = tns.ind_sizes()
                udims = tuple(ix_sz[i] for i in upper_inds)
                ldims = tuple(ix_sz[i] for i in lower_inds)

        else:
            self._tensors = tuple(tns)

            if udims is None or ldims is None:
                ix_sz = dict(zip(concat((t.inds, t.shape) for t in tns)))
                udims = tuple(ix_sz[i] for i in upper_inds)
                ldims = tuple(ix_sz[i] for i in lower_inds)

        self.upper_inds, self.lower_inds = upper_inds, lower_inds
        self.udims, self.ud = udims, prod(udims)
        self.ldims, self.ld = ldims, prod(ldims)

        super().__init__(dtype=self._tensors[0].dtype,
                         shape=(self.ud, self.ld))

    def _matvec(self, vec):
        iT = Tensor(vec.reshape(*self.udims), inds=self.upper_inds)
        oT = tensor_contract(*self._tensors, iT, output_inds=self.lower_inds)
        return oT.data.reshape(*vec.shape)

    def _rmatvec(self, vec):
        iT = Tensor(vec.conj().reshape(*self.ldims), inds=self.lower_inds)
        oT = tensor_contract(*self._tensors, iT, output_inds=self.upper_inds)
        return oT.data.conj().reshape(*vec.shape)


# --------------------------------------------------------------------------- #
#                            Tensor Network Class                             #
# --------------------------------------------------------------------------- #

class SiteIndexer(object):
    """
    """

    def __init__(self, tn):
        self.tn = tn

    def __getitem__(self, site):
        if site < 0:
            site = self.tn.nsites + site
        site_tag = self.tn.structure.format(site)
        return self.tn[site_tag]

    def __setitem__(self, site, tensor):
        if site < 0:
            site = self.tn.nsites + site
        site_tag = self.tn.structure.format(site)
        self.tn[site_tag] = tensor

    def __delitem__(self, site):
        if site < 0:
            site = self.tn.nsites + site
        site_tag = self.tn.structure.format(site)
        del self.tn[site_tag]


class TensorNetwork(object):
    """A collection of (as yet uncontracted) Tensors.

    Parameters
    ----------
    tensors : sequence of Tensor or TensorNetwork
        The objects to combine. The new network will be a *view* onto these
        constituent tensors unless explicitly copied.
    structure : str, optional
        A string, with integer format specifier, that describes how to range
        over the network's tags in order to contract it.
    structure_bsz : int, optional
        How many sites to group together when auto contracting. Eg for 3 (with
        the dotted lines denoting vertical strips of tensors to be contracted):

            .....       i        ........ i        ...i.
            O-O-O-O-O-O-O-        /-O-O-O-O-        /-O-
            | | | | | | |   ->   1  | | | |   ->   2  |   ->  etc.
            O-O-O-O-O-O-O-        \-O-O-O-O-        \-O-

        Should not require tensor contractions with more than 52 unique
        indices.
    nsites : int, optional
        The total number of sites, if explicitly known. This will be calculated
        using `structure` if needed but not specified. When the network is not
        dense in sites, i.e. ``sites != range(nsites)``, this should be the
        total number of sites the network is embedded in::

            0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10  :-> nsites=10

                  0--0--0--------0--0         :-> sites=(2, 3, 4, 7, 8)
                  |  |  |        |  |

    sites : sequence of int, optional
        The indices of the sites present in this network, defaults to
        ``range(nsites)``. But could be e.g. ``[0, 1, 4, 5, 7]`` if some sites
        have been removed.
    check_collisions : bool, optional
        If True, the default, then Tensors and TensorNetworks with double
        indices which match another Tensor or TensorNetworks double indices
        will have those indices' names mangled. Should be explicily turned off
        when it is known that no collisions will take place -- i.e. when not
        adding any new tensors.
    virtual : bool, optional
        Whether the TensorNetwork should be a *view* onto the tensors it is
        given, or a copy of them. E.g. if a virtual TN is constructed, any
        changes to a Tensor's indices will propagate to all TNs viewing that
        Tensor.

    Members
    -------
    tensor_index : dict
        Mapping of unique ids to tensors, like``{tensor_id: tensor, ...}``.
        I.e. this is where the tensors are 'stored' by the network.
    tag_index : dict
        Mapping of tags to a set of tensor ids which have those tags. I.e.
        ``{tag: {tensor_id_1, tensor_id_2, ...}}``. Thus to select those
        tensors could do: ``map(tensor_index.__getitem__, tag_index[tag])``.
    """

    def __init__(self, tensors, *,
                 check_collisions=True,
                 structure=None,
                 structure_bsz=None,
                 nsites=None,
                 sites=None,
                 virtual=False):

        self.site = SiteIndexer(self)

        # short-circuit for copying TensorNetworks
        if isinstance(tensors, TensorNetwork):
            self.structure = tensors.structure
            self.nsites = tensors.nsites
            self.sites = tensors.sites
            self.structure_bsz = tensors.structure_bsz
            self.tag_index = {
                tg: nms.copy() for tg, nms in tensors.tag_index.items()}
            self.tensor_index = {nm: tsr if virtual else tsr.copy()
                                 for nm, tsr in tensors.tensor_index.items()}
            return

        self.structure = structure
        self.structure_bsz = structure_bsz
        self.nsites = nsites
        self.sites = sites

        self.tensor_index = {}
        self.tag_index = {}

        inner_inds = set()
        for t in tensors:
            self.add(t, virtual=virtual, inner_inds=inner_inds,
                     check_collisions=check_collisions)

        if self.structure:
            # set the list of indices of sites which are present
            if self.sites is None:
                if self.nsites is None:
                    self.nsites = self.calc_nsites()
                self.sites = range(self.nsites)
            else:
                if self.nsites is None:
                    raise ValueError("The total number of sites, ``nsites`` "
                                     "must be specified when a custom subset, "
                                     "i.e. ``sites``, is.")
                self.sites = self.sites

            # set default blocksize
            if self.structure_bsz is None:
                self.structure_bsz = 5

    def _combine_properties(self, other):
        props_equals = (('structure', lambda u, v: u == v),
                        ('nsites', lambda u, v: u == v),
                        ('structure_bsz', lambda u, v: u == v),
                        ('contract_structured_all', functions_equal))

        for prop, equal in props_equals:

            # check whether to inherit ... or compare properties
            u, v = getattr(self, prop, None), getattr(other, prop, None)

            if v is not None:
                # don't have prop yet -> inherit
                if u is None:
                    setattr(self, prop, v)

                # both have prop, and don't match -> raise
                elif not equal(u, v):
                    raise ValueError(
                        "Conflicting values found on tensor networks for "
                        "property {}. First value: {}, second value: {}"
                        .format(prop, u, v))

    def __and__(self, other):
        """Combine this tensor network with more tensors, without contracting.
        Copies the tensors.
        """
        return TensorNetwork((self, other))

    def __or__(self, other):
        """Combine this tensor network with more tensors, without contracting.
        Views the constituent tensors.
        """
        return TensorNetwork((self, other), virtual=True)

    # ------------------------------- Methods ------------------------------- #

    def copy(self, virtual=False, deep=False):
        """Copy this ``TensorNetwork``. If ``deep=False``, (the default), then
        everything but the actual numeric data will be copied.
        """
        if deep:
            return copy.deepcopy(self)
        return self.__class__(self, virtual=virtual)

    def add_tensor(self, tensor, name=None, virtual=False):
        """Add a single tensor to this network - mangle its name if neccessary.
        """
        # check for name conflict
        if name is None:
            name = rand_uuid(base="_T")
        else:
            try:
                self.tensor_index[name]
                name = rand_uuid(base="_T")
            except KeyError:
                # name is fine to keep
                pass

        # add tensor to the main index
        self.tensor_index[name] = tensor if virtual else tensor.copy()

        # add its name to the relevant tags, or create a new tag
        for tag in tensor.tags:
            try:
                self.tag_index[tag].add(name)
            except (KeyError, TypeError):
                self.tag_index[tag] = {name}

    def add_tensor_network(self, tn, virtual=False, check_collisions=True,
                           inner_inds=None):
        """
        """
        self._combine_sites(tn)
        self._combine_properties(tn)

        if check_collisions:  # add tensors individually
            if inner_inds is None:
                inner_inds = set(self.inner_inds())

            # check for matching inner_indices -> need to re-index
            tn_iix = set(tn.inner_inds())
            b_ix = inner_inds & tn_iix

            if b_ix:
                g_ix = tn_iix - inner_inds
                new_inds = {rand_uuid() for _ in range(len(b_ix))}
                reind_map = dict(zip(b_ix, new_inds))
                inner_inds |= new_inds
                inner_inds |= g_ix
            else:
                inner_inds |= tn_iix

            # add tensors, reindexing if necessary
            for nm, tsr in tn.tensor_index.items():
                if b_ix and any(i in reind_map for i in tsr.inds):
                    tsr = tsr.reindex(reind_map, inplace=virtual)
                self.add_tensor(tsr, virtual=virtual, name=nm)

        else:  # directly add tensor/tag indexes
            for nm, tsr in tn.tensor_index.items():
                self.tensor_index[nm] = tsr if virtual else tsr.copy()

            self.tag_index = merge_with(
                set_join, self.tag_index, tn.tag_index)

    def add(self, t, virtual=False, check_collisions=True, inner_inds=None):
        """
        """
        istensor = isinstance(t, Tensor)
        istensornetwork = isinstance(t, TensorNetwork)

        if not (istensor or istensornetwork):
            raise TypeError("TensorNetwork should be called as "
                            "`TensorNetwork(tensors, ...)`, where each "
                            "object in 'tensors' is a Tensor or "
                            "TensorNetwork.")

        if istensor:
            self.add_tensor(t, virtual=virtual)
        else:
            self.add_tensor_network(t, virtual=virtual, inner_inds=inner_inds,
                                    check_collisions=check_collisions)

    def __iand__(self, tensor):
        """Inplace, but non-virtual, addition of a Tensor or TensorNetwork to
        this network. It should not have any conflicting indices.
        """
        self.add(tensor, virtual=False)
        return self

    def __ior__(self, tensor):
        """Inplace, virtual, addition of a Tensor or TensorNetwork to this
        network. It should not have any conflicting indices.
        """
        self.add(tensor, virtual=True)
        return self

    def calc_nsites(self):
        """Calculate how many tags there are which match ``structure``.
        """
        return len(re.findall(self.structure.format("(\d+)"), str(self.tags)))

    def calc_sites(self):
        """Calculate with sites this TensorNetwork contain based on its
        ``structure``.
        """
        matches = re.findall(self.structure.format("(\d+)"), str(self.tags))
        sites = sorted(map(int, matches))

        # check if can convert to contiguous range
        mn, mx = min(sites), max(sites) + 1
        if len(sites) == mx - mn:
            sites = range(mn, mx)

        return sites

    def _combine_sites(self, other):
        """Correctly combine the sites list of two TNs.
        """
        if (self.sites != other.sites) and (other.sites is not None):
            if self.sites is None:
                self.sites = other.sites
            else:
                self.sites = tuple(sorted(set(self.sites) | set(other.sites)))

                mn, mx = min(self.sites), max(self.sites) + 1
                if len(self.sites) == mx - mn:
                    self.sites = range(mn, mx)

    def _pop_tensor(self, name):
        """Remove a tensor from this network, returning said tensor.
        """
        # remove the tensor from the tag index
        for tag in self.tensor_index[name].tags:
            tagged_names = self.tag_index[tag]
            tagged_names.discard(name)
            if not tagged_names:
                del self.tag_index[tag]

        # pop the tensor itself
        return self.tensor_index.pop(name)

    def _del_tensor(self, name):
        """Delete a tensor from this network.
        """
        # remove the tensor from the tag index
        for tag in self.tensor_index[name].tags:
            tagged_names = self.tag_index[tag]
            tagged_names.discard(name)
            if not tagged_names:
                del self.tag_index[tag]

        # delete the tensor itself
        del self.tensor_index[name]

    def add_tag(self, tag, where=None, mode='all'):
        """Add tag to every tensor in this network, or if ``where`` is
        specified, the tensors matching those tags -- i.e. adds the tag to
        all tensors in ``self.select_tensors(where, mode=mode)``.
        """
        names = self._get_names_from_tags(where, mode=mode)
        names_tensors = ((n, self.tensor_index[n]) for n in names)

        for n, t in names_tensors:
            t.tags.add(tag)

        try:
            self.tag_index[tag] |= names
        except KeyError:
            self.tag_index[tag] = set(names)

    def drop_tags(self, tags):
        """Remove a tag from any tensors in this network which have it.
        Inplace operation.

        Parameters
        ----------
        tags : str or sequence of str
            The tag or tags to drop.
        """
        if isinstance(tags, str):
            tags = (tags,)
        else:
            tags = tuple(tags)  # need to iterate over twice

        for t in self.tensor_index.values():
            t.drop_tags(tags)

        for tag in tags:
            del self.tag_index[tag]

    def retag(self, tag_map, inplace=False):
        """Rename tags for all tensors in this network, optionally in-place.

        Parameters
        ----------
        tag_map : dict-like
            Mapping of pairs ``{old_tag: new_tag, ...}``.
        """
        retagged = self if inplace else self.copy()

        def _retag_single(tag_map):
            # for each remapping pair
            for old_tag, new_tag in tag_map.items():
                # get each tensor with that tag
                for tensor in retagged.tag_index[old_tag]:
                    retagged.tensor_index[tensor].tags.remove(old_tag)
                    retagged.tensor_index[tensor].tags.add(new_tag)
                # and update the tag index
                retagged.tag_index[new_tag] = retagged.tag_index.pop(old_tag)

        # to avoid muddling tags e.g. when swapping, need intermediary step
        midtags = [rand_uuid() for _ in range(len(tag_map))]
        _retag_single({ot: mt for ot, mt in zip(tag_map.keys(), midtags)})
        _retag_single({mt: nt for mt, nt in zip(midtags, tag_map.values())})

        return retagged

    def reindex(self, index_map, inplace=False):
        """Rename indices for all tensors in this network, optionally in-place.

        Parameters
        ----------
        index_map : dict-like
            Mapping of pairs ``{old_ind: new_ind, ...}``.
        """
        new_tn = self if inplace else self.copy()

        for t in new_tn.tensor_index.values():
            t.reindex(index_map, inplace=True)
        return new_tn

    def conj(self, inplace=False):
        """Conjugate all the tensors in this network (leaves all indices).
        """
        new_tn = self if inplace else self.copy()

        for t in new_tn.tensor_index.values():
            t.conj(inplace=True)

        return new_tn

    @property
    def H(self):
        """Conjugate all the tensors in this network (leaves all indices).
        """
        return self.conj()

    def multiply(self, x, inplace=False):
        """Scalar multiplication of this tensor network with ``x``.
        """
        multiplied = self if inplace else self.copy()
        tensor = next(iter(multiplied.tensor_index.values()))
        tensor.modify(data=tensor.data * x)
        return multiplied

    def __mul__(self, other):
        """Scalar multiplication.
        """
        return self.multiply(other, inplace=False)

    def __rmul__(self, other):
        """Right side scalar multiplication.
        """
        return self.multiply(other, inplace=False)

    def __imul__(self, other):
        """Inplace scalar multiplication.
        """
        return self.multiply(other, inplace=True)

    def __truediv__(self, other):
        """Scalar division.
        """
        return self.multiply(1 / other, inplace=False)

    def __itruediv__(self, other):
        """Inplace scalar division.
        """
        return self.multiply(1 / other, inplace=True)

    @property
    def tensors(self):
        return tuple(self.tensor_index.values())

    def tensors_sorted(self):
        """Return a tuple of tensors sorted by their respective tags, such that
        the tensors of two networks with the same tag structure can be
        iterated over pairwise.
        """
        ts_and_sorted_tags = [(tensor, sorted(tensor.tags))
                              for tensor in self.tensor_index.values()]
        ts_and_sorted_tags.sort(key=lambda x: x[1])
        return tuple(x[0] for x in ts_and_sorted_tags)

    # ----------------- selecting and splitting the network ----------------- #

    def _get_names_from_tags(self, tags, mode='all'):
        """Return the set of names that match ``tags``. If ``mode='all'``,
        each tensor must contain every tag. If ``mode='any'``, each tensor
        only need contain one of the tags.
        """
        if tags is None:
            return set(self.tensor_index)

        try:
            return self.tag_index[tags]
        except (KeyError, TypeError):
            combine = {'all': operator.and_, 'any': operator.or_}[mode]
            return functools.reduce(combine, (self.tag_index[t] for t in tags))

    def select_tensors(self, tags, mode='all'):
        """Return the sequence of tensors that match ``tags``. If
        ``mode='all'``, each tensor must contain every tag. If ``mode='any'``,
        each tensor can contain any of the tags.

        Parameters
        ----------
        tags : hashable or sequence of hashable
            The tag or tag sequence.
        mode : {'all', 'any'}
            Whether to require matching all or any of the tags.

        Returns
        -------
        tagged_tensors : tuple of Tensor
            The tagged tensors.

        See Also
        --------
        select, partition, partition_tensors
        """
        names = self._get_names_from_tags(tags, mode=mode)
        return tuple(self.tensor_index[n] for n in names)

    def select(self, tags, mode='all'):
        """Get a TensorNetwork comprising tensors that match all or any of
        ``tags``, inherit the network properties/structure from ``self``.

        Parameters
        ----------
        tags : hashable or sequence of hashable
            The tag or tag sequence.
        mode : {'all', 'any'}
            Whether to require matching all or any of the tags.

        Returns
        -------
        tagged_tensors : tuple of Tensor
            The tagged tensors.

        See Also
        --------
        select_tensors, partition, partition_tensors
        """
        tagged_names = self._get_names_from_tags(tags, mode=mode)
        ts = (self.tensor_index[n] for n in tagged_names)

        kws = {'check_collisions': False, 'structure': self.structure,
               'structure_bsz': self.structure_bsz, 'nsites': self.nsites}

        tn = TensorNetwork(ts, **kws)

        if self.structure is not None:
            tn.sites = tn.calc_sites()

        return tn

    def __getitem__(self, tags):
        """Get the tensor(s) associated with ``tags``. Only returns tensors
        which match *all* of the tags.

        Parameters
        ----------
        tags : str or sequence of str
            The tags used to select the tensor(s)

        Returns
        -------
        Tensor or sequence of Tensors
        """
        tensors = self.select_tensors(tags, mode='all')

        if len(tensors) == 1:
            return tensors[0]

        return tensors

    def __setitem__(self, tags, tensor):
        """Set the single tensor uniquely associated with ``tags``.
        """
        names = self._get_names_from_tags(tags, mode='all')
        if len(names) != 1:
            raise KeyError("'TensorNetwork.__setitem__' is meant for a single "
                           "existing tensor only - found {} with tag(s) '{}'."
                           .format(len(names), tags))

        if not isinstance(tensor, Tensor):
            raise TypeError("Can only set value with a new 'Tensor'.")

        name, = names

        # check if tags match, else need to modify TN structure
        if self.tensor_index[name].tags != tensor.tags:
            self._del_tensor(name)
            self.add_tensor(tensor, name, virtual=True)
        else:
            self.tensor_index[name] = tensor

    def __delitem__(self, tags):
        """Delete any tensors associated with ``tags``.
        """
        names = self._get_names_from_tags(tags, mode='all')
        for name in tuple(names):
            self._del_tensor(name)

    def partition_tensors(self, tags, inplace=False, mode='any'):
        """Split this TN into a list of tensors containing any or all of
        ``tags`` and a ``TensorNetwork`` of the the rest.

        Parameters
        ----------
        tags : sequence of hashable
            The list of tags to filter the tensors by. Use ``...``
            (``Ellipsis``) to filter all.
        inplace : bool, optional
            If true, remove tagged tensors from self, else create a new network
            with the tensors removed.
        mode : {'all', 'any'}
            Whether to require matching all or any of the tags.

        Returns
        -------
        (u_tn, t_ts) : (TensorNetwork, tuple of Tensors)
            The untagged tensor network, and the sequence of tagged Tensors.

        See Also
        --------
        partition, select, select_tensors
        """

        # contract all
        if tags is ...:
            return None, self.tensor_index.values()

        # Else get the locations of where each tag is found on tensor
        tagged_names = self._get_names_from_tags(tags, mode=mode)

        # check if all tensors have been tagged
        if len(tagged_names) == len(self.tensor_index):
            return None, self.tensor_index.values()

        # Copy untagged to new network, and pop tagged tensors from this
        if inplace:
            untagged_tn = self
        else:
            untagged_tn = self.copy()
        tagged_ts = tuple(map(untagged_tn._pop_tensor, sorted(tagged_names)))

        return untagged_tn, tagged_ts

    def partition(self, tags, mode='any', inplace=False, calc_sites=True):
        """Split this TN into two, based on which tensors have any or all of
        ``tags``. Unlike ``partition_tensors``, both results are TNs which
        inherit the structure of the initial TN.

        Parameters
        ----------
        tags : sequence of hashable
            The tags to split the network with.
        mode : {'any', 'all'}
            Whether to split based on matching any or all of the tags.
        inplace : bool
            If True, actually remove the tagged tensors from self.
        calc_sites : bool
            If True, calculate which sites belong to which network.

        Returns
        -------
        untagged_tn, tagged_tn : (TensorNetwork, TensorNetwork)
            The untagged and tagged tensor networs.

        See Also
        --------
        partition_tensors, select, select_tensors
        """
        tagged_names = self._get_names_from_tags(tags, mode=mode)

        kws = {'check_collisions': False, 'structure': self.structure,
               'structure_bsz': self.structure_bsz, 'nsites': self.nsites}

        if inplace:
            t1 = self
            t2s = [t1._pop_tensor(name) for name in tagged_names]
            t2 = TensorNetwork(t2s, **kws)

        else:  # rebuild both -> quicker
            t1s, t2s = [], []
            for name, tensor in self.tensor_index.items():
                (t2s if name in tagged_names else t1s).append(tensor)

            t1, t2 = TensorNetwork(t1s, **kws), TensorNetwork(t2s, **kws)

        if calc_sites and self.structure is not None:
            t1.sites = t1.calc_sites()
            t2.sites = t2.calc_sites()

        return t1, t2

    def replace_with_identity(self, where, mode='any', inplace=False):
        """Replace all tensors marked by ``where`` with an
        identity. E.g. if ``X`` denote ``where`` tensors::


            ---1  X--X--2---         ---1---2---
               |  |  |  |      -->          |
               X--X--X  |                   |

        Parameters
        ----------
        where : tag or seq of tags
            Tags specifying the tensors to replace.
        mode : {'any', 'all'}
            Whether to replace tensors matching any or all the tags ``where``.
        inplace : bool
            Perform operation in place.

        Returns
        -------
        TensorNetwork
            The TN, with section replaced with identity.

        See Also
        --------
        replace_with_svd
        """
        tn = self if inplace else self.copy()

        if not where:
            return tn

        (dl, il), (dr, ir) = TensorNetwork(
            self.select_tensors(where, mode=mode)).outer_dims_inds()

        if dl != dr:
            raise ValueError(
                "Can only replace_with_identity when the remaining indices "
                "have matching dimensions, but {} != {}.".format(dl, dr))

        for t in where:
            del tn[t]

        tn.reindex({il: ir}, inplace=True)
        return tn

    def replace_with_svd(self, where, left_inds, eps,
                         mode='any', inplace=False):
        """Replace all tensors marked by ``where`` with an iteratively
        constructed SVD. E.g. if ``X`` denote ``where`` tensors::

                                     __       __
            ---X  X--X  X---           \     /
               |  |  |  |      -->      U~s~VH
            ---X--X--X--X            __/     \
                  |     +---     left_inds    \__
                  X

        Parameters
        ----------
        where : tag or seq of tags
            Tags specifying the tensors to replace.
        left_inds : ind or sequence of inds
            The indices defining the left hand side of the SVD.
        eps : float
            The tolerance to perform the SVD with, affects the number of
            singular values kept. See
            :func:`scipy.linalg.interpolative.estimate_rank`.
        mode : {'any', 'all'}
            Whether to replace tensors matching any or all the tags ``where``.
        inplace : bool
            Perform operation in place.

        Returns
        -------

        See Also
        --------
        replace_with_identity
        """
        leave, svd_section = self.partition(where, mode=mode, inplace=inplace,
                                            calc_sites=False)

        left_shp, rght_shp, _left_inds, rght_inds = [], [], [], []
        for d, i in svd_section.outer_dims_inds():
            if i in left_inds:
                left_shp.append(d)
                _left_inds.append(i)
            else:
                rght_shp.append(d)
                rght_inds.append(i)

        left_inds = _left_inds

        A = svd_section.aslinearoperator(upper_inds=rght_inds, udims=rght_shp,
                                         lower_inds=left_inds, ldims=left_shp)

        U, V = _array_split_isvd(A, cutoff=eps)
        U = U.reshape(*left_shp, -1)
        V = V.reshape(-1, *rght_shp)

        new_bnd = rand_uuid()
        tags = svd_section.tags

        # Add the new, compressed tensors back in
        leave |= Tensor(U, inds=(*left_inds, new_bnd), tags=tags)
        leave |= Tensor(V, inds=(new_bnd, *rght_inds), tags=tags)

        return leave

    def convert_to_zero(self):
        """Inplace conversion of this network to an all zero tensor network.
        """
        outer_inds = self.outer_inds()

        for T in self.tensors:
            new_shape = tuple(d if i in outer_inds else 1
                              for d, i in zip(T.shape, T.inds))
            T.modify(data=np.zeros(new_shape, dtype=T.dtype))

    def compress_between(self, tags1, tags2, **compress_opts):
        """Compress the bond between the two single tensors in this network
        specified by ``tags1`` and ``tags2`` using ``tensor_compress_bond``.
        This is an inplace operation.
        """
        n1, = self._get_names_from_tags(tags1, mode='all')
        n2, = self._get_names_from_tags(tags2, mode='all')
        tensor_compress_bond(self.tensor_index[n1], self.tensor_index[n2])

    def compress_all(self, **compress_opts):
        """Inplace compress all bonds in this network.
        """
        for T1, T2 in itertools.combinations(self.tensors, 2):
            try:
                tensor_compress_bond(T1, T2, **compress_opts)
            except ValueError:
                continue
            except ZeroDivisionError:
                self.convert_to_zero()
                break

    def add_bond_between(self, tags1, tags2):
        """Inplace addition of a dummmy (size 1) bond between the single
        tensors specified by by ``tags1`` and ``tags2``.
        """
        n1, = self._get_names_from_tags(tags1, mode='all')
        n2, = self._get_names_from_tags(tags2, mode='all')
        tensor_add_bond(self.tensor_index[n1], self.tensor_index[n2])

    # ----------------------- contracting the network ----------------------- #

    def contract_tags(self, tags, inplace=False, mode='any', **opts):
        """Contract the tensors that match any or all of ``tags``.

        Parameters
        ----------
        tags : sequence of hashable
            The list of tags to filter the tensors by. Use ``...``
            (``Ellipsis``) to contract all.
        inplace : bool, optional
            Whether to perform the contraction inplace.
        mode : {'all', 'any'}
            Whether to require matching all or any of the tags.

        Returns
        -------
        TensorNetwork, Tensor or scalar
            The result of the contraction, still a ``TensorNetwork`` if the
            contraction was only partial.

        See Also
        --------
        contract, contract_cumulative, contract_structured
        """
        untagged_tn, tagged_ts = self.partition_tensors(
            tags, inplace=inplace, mode=mode)

        if not tagged_ts:
            raise ValueError("No tags were found - nothing to contract. "
                             "(Change this to a no-op maybe?)")

        contracted = tensor_contract(*tagged_ts, **opts)

        if untagged_tn is None:
            return contracted

        untagged_tn.add_tensor(contracted, virtual=True)
        return untagged_tn

    def contract_cumulative(self, tags_seq, inplace=False, **opts):
        """Cumulative contraction of tensor network. Contract the first set of
        tags, then that set with the next set, then both of those with the next
        and so forth. Could also be described as an manually ordered
        contraction of all tags in ``tags_seq``.

        Parameters
        ----------
        tags_seq : sequence of sequence of hashable
            The list of tag-groups to cumulatively contract.
        inplace : bool, optional
            Whether to perform the contraction inplace.

        Returns
        -------
        TensorNetwork, Tensor or scalar
            The result of the contraction, still a ``TensorNetwork`` if the
            contraction was only partial.

        See Also
        --------
        contract, contract_tags, contract_structured
        """
        tn = self if inplace else self.copy()
        ctags = set()

        for tags in tags_seq:
            # accumulate tags from each contractions
            if isinstance(tags, str):
                tags = {tags}
            else:
                tags = set(tags)

            ctags |= tags

            # peform the next contraction
            tn = tn.contract_tags(ctags, inplace=True, mode='any', **opts)

            if isinstance(tn, Tensor) or np.isscalar(tn):
                # nothing more to contract
                break

        return tn

    def parse_tag_slice(self, tag_slice):
        """Take a slice object, and work out its implied start, stop and step,
        taking into account counting negatively from the end etc.
        """
        if tag_slice.start is None:
            start = 0
        elif tag_slice.start is ...:
            start = self.nsites - 1
        elif tag_slice.start < 0:
            start = self.nsites + tag_slice.start
        else:
            start = tag_slice.start

        if (tag_slice.stop is ...) or (tag_slice.stop is None):
            stop = self.nsites
        elif tag_slice.stop < 0:
            stop = self.nsites + tag_slice.stop
        else:
            stop = tag_slice.stop

        step = 1 if stop > start else -1
        return start, stop, step

    def contract_structured(self, tag_slice, inplace=False, **opts):
        """Perform a structured contraction, translating ``tag_slice`` from a
        ``slice`` or `...` to a cumulative sequence of tags.

        Parameters
        ----------
        tag_slice : slice or ...
            The range of sites, or `...` for all.
        inplace : bool, optional
            Whether to perform the contraction inplace.

        Returns
        -------
        TensorNetwork, Tensor or scalar
            The result of the contraction, still a ``TensorNetwork`` if the
            contraction was only partial.

        See Also
        --------
        contract, contract_tags, contract_cumulative
        """
        # check for all sites
        if tag_slice is ...:

            # check for a custom structured full contract sequence
            if hasattr(self, "contract_structured_all"):
                return self.contract_structured_all(
                    self, inplace=inplace, **opts)

            # else slice over all sites
            tag_slice = slice(0, self.nsites)

        # filter sites by the slice, but also which sites are present at all
        tags_seq = (self.structure.format(i)
                    for i in range(*self.parse_tag_slice(tag_slice))
                    if i in self.sites)

        # partition sites into `structure_bsz` groups
        if self.structure_bsz > 1:
            tags_seq = partition_all(self.structure_bsz, tags_seq)

        # contract each block of sites cumulatively
        return self.contract_cumulative(tags_seq, inplace=inplace, **opts)

    def contract(self, tags=..., inplace=False, **opts):
        """Contract some, or all, of the tensors in this network. This method
        dispatches to ``contract_structured`` or ``contract_tags``.

        Parameters
        ----------
        tags : sequence of hashable
            Any tensors with any of these tags with be contracted. Set to
            ``...`` (``Ellipsis``) to contract all tensors, the default.
        inplace : bool, optional
            Whether to perform the contraction inplace.
        opts
            Passed to ``tensor_contract``.

        Returns
        -------
        TensorNetwork, Tensor or scalar
            The result of the contraction, still a ``TensorNetwork`` if the
            contraction was only partial.

        See Also
        --------
        contract_structured, contract_tags, contract_cumulative
        """

        # Check for a structured strategy for performing contraction...
        if self.structure is not None:

            # but only use for total or slice tags
            if (tags is ...) or isinstance(tags, slice):
                return self.contract_structured(tags, inplace=inplace, **opts)

        # Else just contract those tensors specified by tags.
        return self.contract_tags(tags, inplace=inplace, **opts)

    def __rshift__(self, tags_seq):
        """Overload of '>>' for TensorNetwork.contract_cumulative.
        """
        return self.contract_cumulative(tags_seq)

    def __irshift__(self, tags_seq):
        """Overload of '>>=' for inplace TensorNetwork.contract_cumulative.
        """
        return self.contract_cumulative(tags_seq, inplace=True)

    def __xor__(self, tags):
        """Overload of '^' for TensorNetwork.contract.
        """
        return self.contract(tags)

    def __ixor__(self, tags):
        """Overload of '^=' for inplace TensorNetwork.contract.
        """
        return self.contract(tags, inplace=True)

    def __matmul__(self, other):
        """Overload "@" to mean full contraction with another network.
        """
        return TensorNetwork((self, other)) ^ ...

    @functools.wraps(TNLinearOperator)
    def aslinearoperator(self, upper_inds, lower_inds, udims=None, ldims=None):
        return TNLinearOperator(self, upper_inds, lower_inds, udims, ldims)

    # --------------- information about indices and dimensions -------------- #

    @property
    def tags(self):
        return tuple(self.tag_index.keys())

    def ind_sizes(self):
        """Get dict of each index mapped to its size.
        """
        ix_szs = (zip(t.inds, t.shape) for t in self.tensor_index.values())
        return dict(concat(ix_szs))

    def all_inds_dims(self):
        """Return a list of all indices, and the corresponding list of
        dimensions from the tensor network.
        """
        return zip(*concat(zip(t.inds, t.shape)
                           for t in self.tensor_index.values()))

    def all_inds(self):
        """Return a tuple of all indices (with repetition) in this network.
        """
        return tuple(concat(t.inds for t in self.tensor_index.values()))

    def inner_inds(self):
        """Return tuple of all inner indices, i.e. those that appear twice.
        """
        all_inds = self.all_inds()
        ind_freqs = frequencies(all_inds)
        return tuple(i for i in all_inds if ind_freqs[i] == 2)

    def outer_dims_inds(self):
        """Get the 'outer' pairs of dimension and indices, i.e. as if this
        tensor network was fully contracted.
        """
        inds, dims = self.all_inds_dims()
        ind_freqs = frequencies(inds)
        return tuple((d, i) for d, i in zip(dims, inds) if ind_freqs[i] == 1)

    def outer_inds(self):
        """Actual, i.e. exterior, indices of this TensorNetwork.
        """
        return tuple(di[1] for di in self.outer_dims_inds())

    def squeeze(self, inplace=False):
        """Drop singlet bonds and dimensions from this tensor network.
        """
        tn = self if inplace else self.copy()
        for t in tn.tensor_index.values():
            t.squeeze(inplace=True)
        return tn

    def max_bond(self):
        """Return the size of the largest bond in this network.
        """
        return max(max(t.shape) for t in self.tensor_index.values())

    @property
    def shape(self):
        """Actual, i.e. exterior, shape of this TensorNetwork.
        """
        return tuple(di[0] for di in self.outer_dims_inds())

    @property
    def dtype(self):
        """The dtype of this TensorNetwork, note this just randomly samples the
        dtype of *one* tensor and thus assumes they all have the same dtype.
        """
        return next(iter(self.tensor_index.values())).dtype

    # ------------------------------ printing ------------------------------- #

    def graph(tn, color=None, label_inds=None, label_tags=None,
              iterations=200, figsize=(6, 6), legend=True, **plot_opts):
        """Plot this tensor network as a networkx graph using matplotlib,
        with edge width corresponding to bond dimension.

        Parameters
        ----------
        iterations : int, optional
            How many iterations to perform when when finding the best layout
            using node repulsion. Ramp this up if the graph is drawing
            messily.
        color : sequence of tags, optional
            If given, uniquely color any tensors which have each of the tags.
            If some tensors have more than of the tags, only one color will
            be shown.
        figsize : tuple of int
            The size of the drawing.
        plot_opts
            Supplied to ``networkx.draw``.
        """
        import networkx as nx
        import matplotlib.pyplot as plt
        import math

        # build the graph
        G = nx.Graph()
        ts = list(tn.tensors)
        n = len(ts)

        if label_inds is None:
            label_inds = (n <= 20)
            label_tags = (n <= 20)

        labels = {}

        for i, t1 in enumerate(ts):
            for ix in t1.inds:
                found_ind = False

                # check to see if index is linked to another tensor
                for j in range(0, n):
                    if j == i:
                        continue

                    t2 = ts[j]
                    if ix in t2.inds:
                        found_ind = True
                        G.add_edge(i, j, weight=t1.bond_size(t2))

                # else it must be an 'external' index
                if not found_ind:
                    ext_lbl = "ext{}".format(ix)
                    G.add_edge(i, ext_lbl, weight=t1.ind_size(ix))

                    # optionally label the external index
                    if label_inds:
                        labels[ext_lbl] = ix

        edge_weights = [x[2]['weight'] for x in G.edges(data=True)]

        # color the nodes
        if color is None:
            colors = {}
        elif isinstance(color, str):
            colors = {color: plt.get_cmap('tab10').colors[0]}
        else:

            # choose longest nice seq of colors
            if len(color) > 10:
                rgbs = plt.get_cmap('tab20').colors
            else:
                rgbs = plt.get_cmap('tab10').colors

            # extend
            extras = [plt.get_cmap(i).colors
                      for i in ('Dark2', 'Set2', 'Set3', 'Accent', 'Set1')]

            # but also resort to random if too long
            def get_rand_colors():
                while True:
                    yield tuple(np.random.rand(3))
            rgbs = concat((rgbs, *extras, get_rand_colors()))

            colors = {tag: c for tag, c in zip(color, rgbs)}

        for i, t1 in enumerate(ts):
            G.node[i]['color'] = None
            for tag in t1.tags:
                if tag in colors:
                    G.node[i]['color'] = colors[tag]

            # optionally label the tensor's tags
            if label_tags:
                labels[i] = str(t1.tags)

        # Set the size of the nodes, so that dangling inds appear so.
        # Also set the colors of any tagged tensors.
        szs = []
        crs = []
        for nd in G.nodes:
            if isinstance(nd, str):
                szs += [0]
                crs += [(1.0, 1.0, 1.0)]
            else:
                szs += [500 / n**0.6]
                if G.node[nd]['color'] is not None:
                    crs += [G.node[nd]['color']]
                else:
                    crs += [(0.6, 0.6, 0.6)]

        edge_weights = [math.log2(d) for d in edge_weights]

        plt.figure(figsize=figsize)

        # use spectral layout as starting point
        pos0 = nx.spectral_layout(G)
        # but then relax using spring layout
        pos = nx.spring_layout(G, pos=pos0, iterations=iterations)
        nx.draw(G, node_size=szs, node_color=crs, pos=pos, labels=labels,
                with_labels=True, width=edge_weights, **plot_opts)

        # create legend
        if colors and legend:
            handles = []
            for color in colors.values():
                handles += [plt.Line2D([0], [0], marker='o', color=color,
                                       linestyle='', markersize=10)]

            # needed in case '_' is the first character
            lbls = [" {}".format(l) for l in colors]

            plt.legend(handles, lbls, ncol=max(int(len(handles) / 20), 1),
                       loc='center left', bbox_to_anchor=(1, 0.5))

        plt.show()

    def __repr__(self):
        return "{}([{}{}{}]{}{})".format(
            self.__class__.__name__,
            os.linesep,
            "".join(["    " + repr(t) + "," + os.linesep
                     for t in self.tensors[:-1]]),
            "    " + repr(self.tensors[-1]) + "," + os.linesep,
            ", structure='{}'".format(self.structure) if
            self.structure is not None else "",
            ", nsites={}".format(self.nsites) if
            self.nsites is not None else "")
