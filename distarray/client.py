# encoding: utf-8
# ---------------------------------------------------------------------------
#  Copyright (C) 2008-2014, IPython Development Team and Enthought, Inc.
#  Distributed under the terms of the BSD License.  See COPYING.rst.
# ---------------------------------------------------------------------------
"""
The Distarray data structure.`DistArray` objects are proxies for collections of
`LocalArray` objects. They are meant to roughly emulate NumPy `ndarrays`.
"""

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

import operator
from itertools import product

import numpy as np

import distarray
from distarray.client_map import Distribution
from distarray.externals.six import next
from distarray.utils import has_exactly_one, _raise_nie

__all__ = ['DistArray']


# ---------------------------------------------------------------------------
# Code
# ---------------------------------------------------------------------------

def process_return_value(subcontext, result_key, targets):
    """Figure out what to return on the Client.

    Parameters
    ----------
    key : string
        Key corresponding to wrapped function's return value.

    Returns
    -------
    A DistArray (if locally all values are DistArray), a None (if
    locally all values are None), or else, pull the result back to the
    client and return it.  If all but one of the pulled values is None,
    return that non-None value only.
    """
    type_key = subcontext._generate_key()
    type_statement = "{} = str(type({}))".format(type_key, result_key)
    subcontext._execute(type_statement, targets=targets)
    result_type_str = subcontext._pull(type_key, targets=targets)

    def is_NoneType(typestring):
        return (typestring == "<type 'NoneType'>" or
                typestring == "<class 'NoneType'>")

    def is_LocalArray(typestring):
        return typestring == "<class 'distarray.local.localarray.LocalArray'>"

    if all(is_LocalArray(r) for r in result_type_str):
        result = DistArray.from_localarrays(result_key, subcontext)
    elif all(is_NoneType(r) for r in result_type_str):
        result = None
    else:
        result = subcontext._pull(result_key, targets=targets)
        if has_exactly_one(result):
            result = next(x for x in result if x is not None)

    return result

_DIMDATAS = """
{dim_data_name} = {local_name}.dim_data
"""

def _make_mdmap_from_local_dimdata(local_name, context):
    dim_data_name = context._generate_key()
    context._execute(_DIMDATAS.format(local_name=local_name, dim_data_name=dim_data_name))
    dim_datas = context._pull(dim_data_name)
    return Distribution.from_dim_data(context, dim_datas)

def _get_attribute(context, key, name):
    local_key = context._generate_key()
    context._execute0('%s = %s.%s' % (local_key, key, name))
    result = context._pull0(local_key)
    return result


class DistArray(object):

    __array_priority__ = 20.0

    def __init__(self, mdmap, dtype):
        """ Creates a new empty distarray according to the multi-dimensional
        map given.
        """
        # FIXME: code duplication with context.py.
        ctx = mdmap.context
        # FIXME: this is bad...
        comm_name = ctx._comm_key
        # FIXME: and this is bad...
        da_key = ctx._generate_key()
        names = ctx._key_and_push(mdmap.shape, mdmap.dist, mdmap.grid_shape, dtype)
        shape_name, dist_name, grid_shape_name, dtype_name = names
        cmd = ('{da_key} = distarray.local.empty('
               'distarray.local.maps.Distribution.from_shape({shape_name}, '
               '{dist_name}, {grid_shape_name}, {comm_name}), {dtype_name})')
        ctx._execute(cmd.format(**locals()))
        self.mdmap = mdmap
        self.key = da_key
        self._dtype = dtype

    @classmethod
    def from_localarrays(cls, key, context):
        """ The caller has already created the LocalArray objects.  `key` is
        their name on the engines.  This classmethod creates a DistArray that
        refers to these LocalArrays.

        """
        da = cls.__new__(cls)
        da.key = key
        da.mdmap = _make_mdmap_from_local_dimdata(key, context)
        da._dtype = _get_attribute(context, key, 'dtype')
        return da

    def __del__(self):
        self.context.delete_key(self.key)

    def __repr__(self):
        s = '<DistArray(shape=%r, targets=%r)>' % \
            (self.shape, self.context.targets)
        return s

    def _normalize_index(self, index):
        """Check index is within bounds. Coerce to tuple. And positivify."""
        # coerce to tuple
        if isinstance(index, int) or isinstance(index, slice):
            index = (index,)
        elif isinstance(index, list):
            index = tuple(index)

        # positivify
        out = []
        for dim, i in enumerate(index):
            if i >= 0:
                out.append(i)
            else:
                out.append(self.shape[dim] + i)
        index = tuple(out)

        # Check index is within bounds
        for dim, i in enumerate(index):
            if (i >= self.shape[dim]) or (i < 0):
                raise IndexError("index %s out of bound of DistArray with shap"
                                 "e %s" % (index, self.shape))
        return index

    def __getitem__(self, index):
        #TODO: FIXME: major performance improvements possible here,
        # especially for special cases like `index == slice(None)`.
        # This would dramatically improve tondarray's performance.
        def getit(arr_name, index):
            arr = eval(arr_name)
            return arr.global_index[index]

        index = self._normalize_index(index)

        if isinstance(index, tuple):
            targets = self.mdmap.owning_targets(index)
            if len(targets) == 1:
                # make subview
                subview = self.context.view.client[targets]
                result = subview.apply_sync(getit, self.key, index)[0]
                return result

            result_key = self.context._generate_key()
            fmt = '%s = %s.checked_getitem(%s)'
            statement = fmt % (result_key, self.key, index)
            self.context._execute(statement, targets=targets)
            result = process_return_value(self.context, result_key, targets=targets)
            if result is None:
                raise IndexError("Index %r is out of bounds" % (index,))
            else:
                return result
        else:
            raise TypeError("Invalid index type.")

    def __setitem__(self, index, value):
        #TODO: FIXME: major performance improvements possible here.
        # Especially when `index == slice(None)` and value is an
        # ndarray, since for block and cyclic, we can generate slices of
        # `value` and assign to local arrays. This would dramatically
        # improve the fromndarray method's performance.
        index = self._normalize_index(index)

        if isinstance(index, tuple):
            targets = self.mdmap.owning_targets(index)
            result_key = self.context._generate_key()
            fmt = '%s = %s.checked_setitem(%s, %s)'
            statement = fmt % (result_key, self.key, index, value)
            self.context._execute(statement, targets=targets)
            result = process_return_value(self.context, result_key, targets=targets)
            if result is None:
                raise IndexError("Index %r is out of bounds" % (index,))

        else:
            raise TypeError("Invalid index type.")

    @property
    def context(self):
        return self.mdmap.context

    @property
    def shape(self):
        return self.mdmap.shape

    @property
    def global_size(self):
        return reduce(operator.mul, self.shape)

    @property
    def dist(self):
        return self.mdmap.dist

    @property
    def grid_shape(self):
        return self.mdmap.grid_shape

    @property
    def ndim(self):
        return len(self.shape)

    @property
    def nbytes(self):
        return self.global_size * self.itemsize

    @property
    def dtype(self):
        return self._dtype

    @property
    def itemsize(self):
        return self._dtype.itemsize

    def tondarray(self):
        """Returns the distributed array as an ndarray."""
        arr = np.empty(self.shape, dtype=self.dtype)
        local_name = self.context._generate_key()
        self.context._execute('%s = %s.copy()' % (local_name, self.key))
        local_arrays = self.context._pull(local_name)
        for local_array in local_arrays:
            maps = (list(ax_map.global_iter) for ax_map in
                    local_array.distribution)
            for index in product(*maps):
                arr[index] = local_array.global_index[index]
        return arr

    toarray = tondarray

    def get_dist_matrix(self):
        key = self.context._generate_key()
        self.context._execute0(
            '%s = %s.get_dist_matrix()' % (key, self.key))
        result = self.context._pull0(key)
        return result

    def fill(self, value):
        value_key = self.context._generate_key()
        self.context._push({value_key:value})
        self.context._execute('%s.fill(%s)' % (self.key, value_key))

    #TODO FIXME: implement axis and out kwargs.
    def sum(self, axis=None, dtype=None, out=None):
        if axis or out is not None:
            _raise_nie()
        keys = self.context._key_and_push(axis, dtype)
        result_key = self.context._generate_key()
        subs = (result_key, self.key) + keys
        self.context._execute('%s = %s.sum(%s,%s)' % subs)
        result = self.context._pull0(result_key)
        return result

    def mean(self, axis=None, dtype=float, out=None):
        if axis or out is not None:
            _raise_nie()
        keys = self.context._key_and_push(axis, dtype)
        result_key = self.context._generate_key()
        subs = (result_key, self.key) + keys
        self.context._execute('%s = %s.mean(axis=%s, dtype=%s)' % subs)
        result = self.context._pull0(result_key)
        return result

    def var(self, axis=None, dtype=None, out=None):
        if axis or out is not None:
            _raise_nie()
        keys = self.context._key_and_push(axis, dtype)
        result_key = self.context._generate_key()
        subs = (result_key, self.key) + keys
        self.context._execute('%s = %s.var(%s,%s)' % subs)
        result = self.context._pull0(result_key)
        return result

    def std(self, axis=None, dtype=None, out=None):
        if axis or out is not None:
            _raise_nie()
        keys = self.context._key_and_push(axis, dtype)
        result_key = self.context._generate_key()
        subs = (result_key, self.key) + keys
        self.context._execute('%s = %s.std(%s,%s)' % subs)
        result = self.context._pull0(result_key)
        return result

    def get_ndarrays(self):
        """Pull the local ndarrays from the engines.

        Returns
        -------
        list of ndarrays
            one ndarray per process

        """
        key = self.context._generate_key()
        self.context._execute('%s = %s.get_localarray()' % (key, self.key))
        result = self.context._pull(key)
        return result

    def get_localarrays(self):
        """Pull the LocalArray objects from the engines.

        Returns
        -------
        list of localarrays
            one localarray per process

        """
        result = self.context._pull(self.key)
        return result

    def get_localshapes(self):
        key = self.context._generate_key()
        self.context._execute('%s = %s.local_shape' % (key, self.key))
        result = self.context._pull(key)
        return result

    # Binary operators

    def _binary_op_from_ufunc(self, other, func, rop_str=None, *args, **kwargs):
        if hasattr(other, '__array_priority__') and hasattr(other, rop_str):
            if other.__array_priority__ > self.__array_priority__:
                rop = getattr(other, rop_str)
                return rop(self)
        return func(self, other, *args, **kwargs)

    def _rbinary_op_from_ufunc(self, other, func, lop_str, *args, **kwargs):
        if hasattr(other, '__array_priority__') and hasattr(other, lop_str):
            if other.__array_priority__ > self.__array_priority__:
                lop = getattr(other, lop_str)
                return lop(self)
        return func(other, self, *args, **kwargs)

    def __add__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.add, '__radd__', *args, **kwargs)

    def __sub__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.subtract, '__rsub__', *args, **kwargs)

    def __mul__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.multiply, '__rmul__', *args, **kwargs)

    def __div__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.divide, '__rdiv__', *args, **kwargs)

    def __truediv__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.true_divide, '__rtruediv__', *args, **kwargs)

    def __floordiv__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.floor_divide, '__rfloordiv__', *args, **kwargs)

    def __mod__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.mod, '__rdiv__', *args, **kwargs)

    def __pow__(self, other, modulo=None, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.power, '__rpower__', *args, **kwargs)

    def __lshift__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.left_shift, '__rlshift__', *args, **kwargs)

    def __rshift__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.right_shift, '__rrshift__', *args, **kwargs)

    def __and__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.bitwise_and, '__rand__', *args, **kwargs)

    def __or__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.bitwise_or, '__ror__', *args, **kwargs)

    def __xor__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.bitwise_xor, '__rxor__', *args, **kwargs)

    # Binary - right versions

    def __radd__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.add, '__add__', *args, **kwargs)

    def __rsub__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.subtract, '__sub__', *args, **kwargs)

    def __rmul__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.multiply, '__mul__', *args, **kwargs)

    def __rdiv__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.divide, '__div__', *args, **kwargs)

    def __rtruediv__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.true_divide, '__truediv__', *args, **kwargs)

    def __rfloordiv__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.floor_divide, '__floordiv__', *args, **kwargs)

    def __rmod__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.mod, '__mod__', *args, **kwargs)

    def __rpow__(self, other, modulo=None, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.power, '__pow__', *args, **kwargs)

    def __rlshift__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.left_shift, '__lshift__', *args, **kwargs)

    def __rrshift__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.right_shift, '__rshift__', *args, **kwargs)

    def __rand__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.bitwise_and, '__and__', *args, **kwargs)

    def __ror__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.bitwise_or, '__or__', *args, **kwargs)

    def __rxor__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.bitwise_xor, '__xor__', *args, **kwargs)

    def __neg__(self, *args, **kwargs):
        return distarray.negative(self, *args, **kwargs)

    def __pos__(self, *args, **kwargs):
        return self

    def __abs__(self, *args, **kwargs):
        return distarray.abs(self, *args, **kwargs)

    def __invert__(self, *args, **kwargs):
        return distarray.invert(self, *args, **kwargs)

    # Boolean comparisons

    def __lt__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.less, '__lt__', *args, **kwargs)

    def __le__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.less_equal, '__le__', *args, **kwargs)

    def __eq__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.equal, '__eq__', *args, **kwargs)

    def __ne__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.not_equal, '__ne__', *args, **kwargs)

    def __gt__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.greater, '__gt__', *args, **kwargs)

    def __ge__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.greater_equal, '__ge__', *args, **kwargs)
