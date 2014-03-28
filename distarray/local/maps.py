# encoding: utf-8
#----------------------------------------------------------------------------
#  Copyright (C) 2008-2014, IPython Development Team and Enthought, Inc.
#  Distributed under the terms of the BSD License.  See COPYING.rst.
#----------------------------------------------------------------------------

from __future__ import division

import numpy as np

from distarray.externals.six.moves import range, zip


class MDMap(object):
    
    @classmethod
    def from_dim_data(cls, dim_data):
        self = cls.__new__(cls)
        self.maps = tuple(map_from_dim_dict(dimdict)
                            for dimdict in dim_data)
        self.ndim = len(self.maps)
        return self

    @property
    def local_shape(self):
        return tuple(m.size for m in self.maps)

    def local_from_global(self, *global_ind):
        return tuple(self.maps[dim].local_index[global_ind[dim]]
                     for dim in range(self.ndim))

    def global_from_local(self, *local_ind):
        return tuple(self.maps[dim].global_index[local_ind[dim]]
                     for dim in range(self.ndim))

    def __getitem__(self, idx):
        return self.maps[idx]

    def __len__(self):
        return len(self.maps)


def map_from_dim_dict(dd):

    # Extract parameters from the dimension dictionary.
    dist_type = dd['dist_type']
    size = dd['size']
    start = dd.get('start', None)
    stop = dd.get('stop', None)
    grid_rank = dd.get('proc_grid_rank', 0)
    grid_size = dd.get('proc_grid_size', 1)
    block_size = dd.get('block_size', 1)
    indices = dd.get('indices', None)

    if dist_type == 'n':
        return BlockMap(global_size=size, grid_size=grid_size, grid_rank=grid_rank, start=0, stop=size)
    if dist_type == 'b':
        return BlockMap(global_size=size, grid_size=grid_size, grid_rank=grid_rank, start=start, stop=stop)
    if dist_type == 'c' and block_size == 1:
        return CyclicMap(global_size=size, grid_size=grid_size, grid_rank=grid_rank, start=start)
    if dist_type == 'c' and block_size > 1:
        return BlockCyclicMap(global_size=size, grid_size=grid_size, grid_rank=grid_rank, start=start, block_size=block_size)
    if dist_type == 'u':
        return UnstructuredMap(global_size=size, grid_size=grid_size, grid_rank=grid_rank, indices=indices)
    raise ValueError("Unsupported dist_type of %r" % dist_type)


class MapBase(object):
    pass


class BlockMap(MapBase):

    dist = 'b'

    def __init__(self, global_size, grid_size, grid_rank, start, stop):
        self.start = start
        self.stop = stop
        self.local_size = stop - start
        self.global_size = global_size
        self.grid_size = grid_size
        self.grid_rank = grid_rank


    def local_from_global(self, gidx):
        if gidx < self.start or gidx >= self.stop:
            raise IndexError()
        return gidx - self.start

    def global_from_local(self, lidx):
        if lidx >= self.local_size:
            raise IndexError()
        return lidx + self.start

    def global_slice(self):
        return slice(self.start, self.stop)

    @property
    def dim_dict(self):
        return {'dist_type': self.dist,
                'size': self.global_size,
                'proc_grid_rank': self.grid_rank,
                'proc_grid_size': self.grid_size,
                'start': self.start,
                'stop': self.stop,
                }

    @property
    def global_index(self):
        return list(range(self.start, self.stop))

    @property
    def local_index(self):
        local_indices = range(self.local_size)
        return dict(zip(self.global_index, local_indices))

    @property
    def size(self):
        return len(self.global_index)


class CyclicMap(MapBase):

    dist = 'c'

    def __init__(self, global_size, grid_size, grid_rank, start):
        if start != grid_rank:
            msg = "start value (given %d) does not equal grid_rank (given %d)"
            raise ValueError(msg % (start, grid_rank))
        if start >= grid_size:
            msg = "start (%d) is greater or equal to grid_size (%d)"
            raise ValueError(msg % (start, grid_size))
        self.start = start
        self.grid_size = grid_size
        self.grid_rank = grid_rank

        self.local_size = (global_size - 1 - grid_rank) // grid_size + 1
        self.global_size = global_size


    def local_from_global(self, gidx):
        if (gidx - self.start) % self.grid_size:
            raise IndexError()
        return (gidx - self.start) // self.grid_size

    def global_from_local(self, lidx):
        if lidx >= self.local_size:
            raise IndexError()
        return (lidx * self.grid_size) + self.start

    def global_slice(self):
        return slice(self.start, self.global_size, self.grid_size)

    @property
    def dim_dict(self):
        return {'dist_type': self.dist,
                'size': self.global_size,
                'proc_grid_rank': self.grid_rank,
                'proc_grid_size': self.grid_size,
                'start': self.start,
                }

    @property
    def global_index(self):
        return list(range(self.start, self.global_size, self.grid_size))

    @property
    def local_index(self):
        local_indices = range(self.local_size)
        return dict(zip(self.global_index, local_indices))

    @property
    def size(self):
        return len(self.global_index)


class BlockCyclicMap(MapBase):

    dist = 'c'
    
    def __init__(self, global_size, grid_size, grid_rank, start, block_size):
        if start % block_size:
            raise ValueError()
        self.start = start
        self.start_block = start // block_size
        self.block_size = block_size
        global_nblocks = global_size // block_size
        if global_nblocks * block_size != global_size:
            raise ValueError()
        self.grid_size = grid_size

        local_nblocks = (global_nblocks - 1 - grid_rank) // grid_size + 1
        self.local_size = local_nblocks * block_size
        self.global_size = global_size


    def local_from_global(self, gidx):
        global_block, offset = divmod(gidx, self.block_size)
        if (global_block - self.start_block) % self.grid_size:
            raise IndexError()
        return self.block_size * ((global_block - self.start_block) // self.grid_size) + offset

    def global_from_local(self, lidx):
        if lidx >= self.local_size:
            raise IndexError()
        local_block, offset = divmod(lidx, self.block_size)
        global_block = (local_block * self.grid_size) + self.start_block
        return global_block * self.block_size + offset

    def global_slice(self):
        raise NotImplementedError()
        # TODO: FIXME: Not really a slice, but a fancy index, since can't
        # represent blockcyclic as a slice!
        # idxs = np.empty(self.size, dtype=np.int)
        # for offset in range(self.block_size):

    @property
    def dim_dict(self):
        return {'dist_type': self.dist,
                'size': self.global_size,
                'proc_grid_rank': self.grid_rank,
                'proc_grid_size': self.grid_size,
                'start': self.start,
                'block_size': self.block_size,
                }

    @property
    def size(self):
        return len(self.global_index)

    @property
    def global_index(self):
        _global_index = np.empty((self.local_size,), dtype=np.int32)
        # FIXME: this is the slow way to do this...
        for i in range(self.local_size):
            _global_index[i] = self.global_from_local(i)
        return _global_index

    @property
    def local_index(self):
        local_indices = range(self.local_size)
        return dict(zip(self.global_index, local_indices))


class UnstructuredMap(MapBase):

    dist = 'u'

    def __init__(self, global_size, grid_size, grid_rank, indices):
        self.global_size = global_size
        self.grid_size = grid_size
        self.grid_rank = grid_rank
        self.indices = list(indices)
        self.local_size = len(self.indices)
        local_indices = range(len(self.indices))
        self._local_index = dict(zip(self.indices, local_indices))

    def local_from_global(self, gidx):
        try:
            lidx = self.indices.index(gidx)
        except ValueError:
            raise IndexError()
        return lidx

    def global_from_local(self, lidx):
        return self.indices[lidx]

    def global_slice(self):
        raise NotImplementedError()

    @property
    def dim_dict(self):
        return {'dist_type': self.dist,
                'size': self.global_size,
                'proc_grid_rank': self.grid_rank,
                'proc_grid_size': self.grid_size,
                'indices': self.indices,
                }

    @property
    def global_index(self):
        return self.indices

    @property
    def local_index(self):
        return self._local_index

    @property
    def size(self):
        return len(self.global_index)
