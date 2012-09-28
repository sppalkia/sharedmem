"""
Dispatch your trivially parallizable jobs with sharedmem.

There are also sharedmem.argsort() and sharedmem.fromfile(),
which are the parallel equivalent of numpy.argsort() and numpy.fromfile().

Environment variable OMP_NUM_THREADS is used to determine the
default number of Slaves.

output = sharedmem.empty(8)
output2 = sharedmem.empty(8)

with sharedmem.Pool() as pool:
  def work(arg1, arg2):
    # do some work, return some value or not.
    output2[arg1] = pool.rank
    with pool.lock:
      output[...] += arg2
    pass
  pool.starmap(work, pool.zipsplit((range(8), 1)))

output2 will be an array of the ranks,
and output will be 36, 36, ....

Pool:
  pool.rank, pool.np, and pool.lock, pool.local

  sharedmem.Pool is much faster than multiprocessing.Pool because
  it has very limited functionality, and does not pickle anything.
  It will give gibberish on machines without a fork.

  grabbing pool.lock ensures a critical section.
  pool.local provides some basic local storage, but is not
  well initialized. Do not use it.

  pool.map() maps the parameters to work directly
  pool.starmap() maps the parameters as an argument list.

  The return value of pool.map() and pool.starmap() is a list of
  the return values of work. The returned list is unordered unless
  ordered=True is set in the call. 
  Returning via the return value is discouraged, as it is much 
  slower than directly writing the output to array objects.

  pool.split() splits the variables given in the first parameter.
  Lists and numpy Arrays are splited to almost equal sized chunks,
  Scalar, and Tuples are repeated.
  pool.zipsplit() zips the result of pool.split() so that it is
  ready for pool.starmap()

Exception handling:
  once a Slave raises an Exception, it is collected by the master,
  and that slave dies, with remaining already assigned to the slave
  unfinished.

  After all Slaves dies(those who did not raise an Exception will keep
  running), the exceptions are collected by the Master, and the first
  recieved exception will be reraised by the Master. 

Joining Slaves:
  On some large number of core machines with heavy IOs, some Slaves
  will take longer than expected to join. We retry a few times until
  eventually give it up. This is not usually a fatal problem as all work
  are already done. 


Debugging:
  sharedmem.set_debug(True)

  in debugging mode no Slaves are spawned. all work is done in the
  Master, thus the work function can be debugged.

Backends:
  sharedmem has 2 parallel backends: Process and Threads.

  If you do not write to array objects and all lengthy jobs
  releases GIL, then the two backends are equivalent. 

  1. Processes. 
  * with sharedmem.Pool(use_threads=False) as pool:
  * There is no need ensure lengthy calculation needs to release
    the GIL.
  * Any numpy array that needs to be written by
    the slaves needs to reside on the shared memory segments. 
    numpy arrays on the shared memory has type SharedMemArray.

  * SharedMemArray are allocated with
      sharedmem.empty()
    or copied from local numpy array with
      sharedmem.copy()

    If possible use empty() for huge data sets.
    Because with copy() the data has to be copied and memory usage
    is (at least temporarily doubled)

  2. Threads
  * with sharedmem.Pool(use_threads=True)
  * Slaves can write to ordinary numpy arrays.
  * need to ensure lengthy calculation releases the GIL.

"""

import multiprocessing as mp
import numpy
import os
import threading
import Queue as queue
import ctypes
import traceback
import copy_reg
import signal
import itertools

from numpy import ctypeslib
from multiprocessing.sharedctypes import RawArray
from listtools import cycle, zip, repeat
from warnings import warn
import heapq

__shmdebug__ = False
__timeout__ = 10

def set_debug(flag):
  """ in debug mode (flag==True), no slaves are spawn,
      rather all work are done in serial on the master thread/process.
      so that if the worker throws out exceptions, debugging from the main
      process context is possible. (in iptyhon, with debug magic command, eg)
  """
  global __shmdebug__
  __shmdebug__ = flag

def set_timeout(timeout):
  """ set max number of timeout retries. each retry we wait for 10 secs. 
      if the master fails to join all slaves after timeout retries,
      a warning will be issued with the total number of alive slaves
      reported. 
      The program continues, but there may be a serious issue in the worker 
      functions.
  """
  global __timeout__
  ret = timeout
  __timeout__ = timeout
  return ret

def cpu_count():
  """ The cpu count defaults to the number of physical cpu cores
      but can be set with OMP_NUM_THREADS environment variable.
      OMP_NUM_THREADS is used because if you hybrid sharedmem with
      some openMP extenstions one environment will do it all.

      on some machines the physical number of cores does not equal
      the number of cpus shall be used. PSC Blacklight for example.

      Pool defaults to use cpu_count() slaves. however it can be overridden
      in Pool.
  """
  num = os.getenv("OMP_NUM_THREADS")
  try:
    return int(num)
  except:
    return mp.cpu_count()

class Pool:
  """
    with Pool() as p
      def work(a, b, c):
        pass
      p.starmap(work, zip(A, B, C))

    To use a Thread pool, pass use_threads=True
    there is a Lock accessible as 'with p.lock'

    Refer to the module document.
  """
  def __enter__(self):
    return self
  def __exit__(self, type, value, traceback):
    pass
  @property
  def rank(self):
    return self._local._rank

  @property 
  def local(self):
    return self._local

  def __init__(self, np=None, use_threads=False, nested=False):
    if np is None: np = cpu_count()
    self.np = np
    self._local = None
    self.serial = False

    if use_threads:
      if threading.currentThread().name != 'MainThread' and not nested:
        self.serial = True
        warn('nested Pool is avoided', stacklevel=2)
      self.QueueFactory = queue.Queue
      self.JoinableQueueFactory = queue.Queue
      def func(*args, **kwargs):
        slave = threading.Thread(*args, **kwargs)
        slave.daemon = True
        return slave
      self.SlaveFactory = func
      self.lock = threading.Lock()
      self._local = threading.local()
    else:
      self.QueueFactory = mp.Queue
      self.JoinableQueueFactory = mp.JoinableQueue
      def func(*args, **kwargs):
        slave = mp.Process(*args, **kwargs)
        slave.daemon = True
        return slave
      self.SlaveFactory = func
      self.lock = mp.Lock()
      self._local = lambda: None
     # master threads's rank is None
      self._local.rank = None

  def zipsplit(self, list, nchunks=None, chunksize=None, axis=0):
    return zip(*self.split(list, nchunks, chunksize, axis))

  def split(self, list, nchunks=None, chunksize=None, axis=0):
    if nchunks is None and chunksize is None:
      nchunks = self.np
    return split(list, nchunks, chunksize, axis)

  def starmap(self, work, sequence, ordered=False, callback=None):
    return self.map(work, sequence, ordered=ordered, callback=callback, star=True)

  def do(self, jobs):
    def work(job):
      job()
    return self.map(work, jobs, ordered=False, star=False)

  def map(self, workfunc, sequence, ordered=False, star=False, callback=None):
    """
      calls workfunc on every item in sequence. the return value is unordered unless ordered=True.
    """
    if __shmdebug__: 
      warn('shm debugging')
      return self.map_debug(workfunc, sequence, ordered, star)
    if self.serial: 
      return self.map_debug(workfunc, sequence, ordered, star)

    L = len(sequence)
    if not hasattr(sequence, '__getitem__'):
      raise TypeError('can only take a slicable sequence')

    def slave(S, Q, rank):
      self._local._rank = rank
      dead = False
      error = None
      while True:
        workcapsule = S.get()
        if workcapsule is None: 
          S.task_done()
          break
        if dead: 
          Q.put((None, None))
          S.task_done()
          continue

        i, = workcapsule
        try:
          if star: out = workfunc(*sequence[i])
          else: out = workfunc(sequence[i])
        except Exception as e:
          error = (e, traceback.format_exc())
          Q.put(error)
          dead = True

        if not dead: Q.put((i, out))
        S.task_done()
    P = []
    Q = self.QueueFactory()
    S = self.JoinableQueueFactory()

    i = 0

    for i, work in enumerate(sequence):
      S.put((i, ))

    for rank in range(self.np):
        S.put(None) # sentinel

    # the slaves will not raise KeyboardInterrupt Exceptions
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    for rank in range(self.np):
        p = self.SlaveFactory(target=slave, args=(S, Q, rank))
        P.append(p)

    for p in P:
        p.start()

    signal.signal(signal.SIGINT, old)

#   the result is not sorted yet
    R = []
    error = []
    while L > 0:
      ind, r = Q.get()
      if isinstance(ind, Exception): 
        error.append((ind, r))
      elif ind is None:
        # the worker dead, nothing done
        pass
      else:
        if callback is not None:
          r = callback(r)
        R.append((ind, r))
      L = L - 1

    S.join()

    # must clear Q before joining the Slaves or we deadlock.
    while not Q.empty():
      warn("unexpected extra queue item: %s" % str(Q.get()))
      
    i = 0
    alive = 1
    while alive > 0 and i < __timeout__:
      alive = 0
      for rank, p in enumerate(P):
        if p.is_alive():
          p.join(10)
          if p.is_alive():
            # we are in a serious Bug of sharedmem if reached here.
            warn("still waiting for slave %d" % rank)
            alive = alive + 1
      i = i + 1

    if alive > 0:
      warn("%d slaves alive after queue joined" % alive)
      
    # now report any errors
    if error:
      raise Exception('%d errors received\n' % len(error) + error[0][1])

    if ordered:
      heapq.heapify(R)
      return numpy.array([ heapq.heappop(R)[1] for i in range(len(R))])
    else:
      return numpy.array([r[1] for r in R ])

  def map_debug(self, work, sequence, ordered=False, star=False):
    if star: return [work(*x) for x in sequence]
    else: return [work(x) for x in sequence]

# Pickling is needed only for mp.Pool. Our pool is directly based on Process
# thus no need to pickle anything

def __unpickle__(ai, dtype):
  dtype = numpy.dtype(dtype)
  tp = ctypeslib._typecodes['|u1']
  # if there are strides, use strides, otherwise the stride is the itemsize of dtype
  if ai['strides']:
    tp *= ai['strides'][-1]
  else:
    tp *= dtype.itemsize
  for i in numpy.asarray(ai['shape'])[::-1]:
    tp *= i
  # grab a flat char array at the sharemem address, with length at least contain ai required
  ra = tp.from_address(ai['data'][0])
  buffer = ctypeslib.as_array(ra).ravel()
  # view it as what it should look like
  shm = numpy.ndarray(buffer=buffer, dtype=dtype, 
      strides=ai['strides'], shape=ai['shape']).view(type=SharedMemArray)
  return shm

def __pickle__(obj):
  return obj.__reduce__()

class SharedMemArray(numpy.ndarray):
  """ 
      SharedMemArray works with multiprocessing.Pool through pickling.
      With sharedmem.Pool pickling is unnecssary. sharemem.Pool is recommended.

      Do not directly create an SharedMemArray or pass it to numpy.view.
      Use sharedmem.empty or sharedmem.copy instead.

      When a SharedMemArray is pickled, only the meta information is stored,
      So that when it is unpicled on the other process, the data is not copied,
      but simply viewed on the same address.
  """
  __array_priority__ = -900.0

  def __new__(cls, shape, dtype='f8'):
    dtype = numpy.dtype(dtype)
    tp = ctypeslib._typecodes['|u1'] * dtype.itemsize
    ra = RawArray(tp, int(numpy.asarray(shape).prod()))
    shm = ctypeslib.as_array(ra)
    if not shape:
      fullshape = dtype.shape
    else:
      if not dtype.shape:
        fullshape = shape
      else:
        if not hasattr(shape, "__iter__"):
          shape = [shape]
        else:
          shape = list(shape)
        if not hasattr(dtype.shape, "__iter__"):
          dshape += [dtype.shape]
        else:
          dshape = list(dtype.shape)
        fullshape = shape + dshape
    return shm.view(dtype=dtype.base, type=SharedMemArray).reshape(fullshape)
    
  def __reduce__(self):
    return __unpickle__, (self.__array_interface__, self.dtype)

copy_reg.pickle(SharedMemArray, __pickle__, __unpickle__)

def empty_like(array, dtype=None):
  if dtype is None: dtype = array.dtype
  return SharedMemArray(array.shape, dtype)

def empty(shape, dtype='f8'):
  """ allocates an empty array on the shared memory """
  return SharedMemArray(shape, dtype)

def copy(a):
  """ copies an array to the shared memory, use
     a = copy(a) to immediately dereference the old 'a' on private memory
   """
  shared = SharedMemArray(a.shape, dtype=a.dtype)
  shared[:] = a[:]
  return shared

def wrap(a):
  return copy(a)

def zipsplit(list, nchunks=None, chunksize=None, axis=0):
  return zip(*self.split(list, nchunks, chunksize, axis))

def split(list, nchunks=None, chunksize=None, axis=0):
    """ Split every item in the list into nchunks, and return a list of chunked items.
           - then used with p.starmap(work, zip(*p.split((xxx,xxx,xxxx), chunksize=1024))
        For non sequence items, tuples, and 0d arrays, constructs a repeated iterator,
        For sequence items(but tuples), convert to numpy array then use array_split to split them.
        either give nchunks or chunksize. chunksize is only instructive, nchunk is estimated from chunksize
    """
    if numpy.isscalar(axis): axis = repeat(axis)

    newlist = []
    for item, ax in zip(list, axis):
      if isinstance(item, tuple) or numpy.isscalar(item):
        # do not chop off scalars or tuples
        newlist.append((item, None, None))
        continue
      else:
        item = numpy.asarray(item)
        newlist.append((item, ax, item.shape[ax]))
    L = numpy.array([l for item, ax, l in newlist if l is not None ])
    if len(L) > 0 and numpy.diff(L).any():
      raise ValueError('elements to chop off are of different lenghts')
    L = L[0]

    if nchunks is None:
      if chunksize is None:
        nchunks = cpu_count() * 2
      else:
        nchunks = int(L / chunksize)
        if nchunks == 0: nchunks = 1

    result = []
    for item, ax, length in newlist:
      if length is None:
        # do not chop off scalars or tuples
        result.append(repeat(item))
      else:
        result.append(array_split(item, nchunks, axis=ax))

    return result

def fromfile(filename, dtype, count=None, chunksize=1024 * 1024 * 64, np=None):
  """ the default size 64MB agrees with lustre block size but is not an optimized choice.
  """
  dtype = numpy.dtype(dtype)
  if hasattr(filename, 'seek'):
    file = filename
  else:
    file = open(filename)

  cur = file.tell()

  if count is None:
    file.seek(0, os.SEEK_END)
    length = file.tell()
    count = (length - cur) / dtype.itemsize
    file.seek(cur, os.SEEK_SET)

  buffer = numpy.empty(dtype=dtype, shape=count) 
  start = numpy.arange(0, count, chunksize)
  stop = start + chunksize
  with Pool(use_threads=True, np=np) as pool:
    def work(start, stop):
      if not hasattr(pool.local, 'file'):
        pool.local.file = open(file.name)
      start, stop, step = slice(start, stop).indices(count)
      pool.local.file.seek(cur + start * dtype.itemsize, os.SEEK_SET)
      buffer[start:stop] = numpy.fromfile(pool.local.file, count=stop-start, dtype=dtype)
    pool.starmap(work, zip(start, stop))

  file.seek(cur + count * dtype.itemsize, os.SEEK_SET)
  return buffer

def tofile(file, array, np=None):
  """ write an array to file in parallel with mmap"""
  file = numpy.memmap(file, array.dtype, 'w+', shape=array.shape)
  with Pool(use_threads=True, np=np) as pool:
    def writechunk(file, chunk):
      file[...] = chunk[...]
    pool.starmap(writechunk, pool.zipsplit((file, array)))
  file.flush()

def __round_to_power_of_two(i):
  if i == 0: return i
  if (i & (i - 1)) == 0: return i
  i = i - 1
  i |= (i >> 1)
  i |= (i >> 2)
  i |= (i >> 4)
  i |= (i >> 8)
  i |= (i >> 16)
  i |= (i >> 32)
  return i + 1

def argsort(data, order=None):
  """
     parallel argsort, like numpy.argsort

     first call numpy.argsort on nchunks of data,
     then merge the returned arg.
     it uses 2 * len(data) * int64.itemsize of memory during calculation,
     that is len(data) * int64.itemsize in addition to the size of the returned array.
     the default chunksize (65536*16) gives a sorting time of 0.4 seconds on a single core 2G Hz computer.
     which is justified by the cost of spawning threads and etc.

     it uses an extra len(data)  * sizeof('i8') for the merging.
     we use threads because it turns out with threads the speed is faster(by 10%~20%)
     for sorting a 100,000,000 'f8' array, on a 16 core machine.
     
     TODO: shall try to use the inplace merge mentioned in 
            http://keithschwarz.com/interesting/code/?dir=inplace-merge.
  """

  from _mergesort import merge
  from _mergesort import reorderdtype
#  if len(data) < 64*65536: return data.argsort()

  if order: 
    newdtype = reorderdtype(data.dtype, order)
    data = data.view(newdtype)

  if cpu_count() <= 1: return data.argsort()

  nchunks = __round_to_power_of_two(cpu_count()) * 4

  arg1 = numpy.empty(len(data), dtype='i8')

  data_split = numpy.array_split(data, nchunks)
  sublengths = numpy.array([len(x) for x in data_split], dtype='i8')
  suboffsets = numpy.zeros(shape = sublengths.shape, dtype='i8')
  suboffsets[1:] = sublengths.cumsum()[:-1]

  arg_split = numpy.array_split(arg1, nchunks)

  with Pool(use_threads=True) as pool:
    def work(data, arg):
      arg[:] = data.argsort()
    pool.starmap(work, zip(data_split, arg_split))
  
  arg2 = numpy.empty(len(data), dtype='i8')

  def work(off1, len1, off2, len2, arg1, arg2, data):
    merge(data[off1:off1+len1+len2], arg1[off1:off1+len1], arg1[off2:off2+len2], arg2[off1:off1+len1+len2])

  while len(sublengths) > 1:
    with Pool(use_threads=True) as pool:
      pool.starmap(work, zip(suboffsets[::2], sublengths[::2], suboffsets[1::2], sublengths[1::2], repeat(arg1), repeat(arg2), repeat(data)))
    arg1, arg2 = arg2, arg1
    suboffsets = [x for x in suboffsets[::2]]
    sublengths = [x+y for x,y in zip(sublengths[::2], sublengths[1::2])]

  return arg1

def take(source, indices, axis=None, out=None, mode='wrap'):
  """ the default mode is 'wrap', because 'raise' will copy the output array.
      we use threads because it is faster than processes.
      need the patch on numpy ticket 2131 to release the GIL.

  """
  if cpu_count() <= 1:
    return numpy.take(source, indices, axis, out, mode)

  indices = numpy.asarray(indices, dtype='i8')
  if out is None:
    if axis is None:
      out = numpy.empty(dtype=source.dtype, shape=indices.shape)
    else:
      shape = []
      for d, n in enumerate(source.shape):
        if d < axis or d > axis:
          shape += [n]
        else:
          for dd, nn in enumerate(indices.shape):
            shape += [nn]
      out = numpy.empty(dtype=source.dtype, shape=shape)

  with Pool(use_threads=True) as pool:
    def work(arg, out):
      #needs numpy ticket #2156
      if len(out) == 0: return
      source.take(arg, axis=axis, out=out, mode=mode)
    pool.starmap(work, pool.zipsplit((indices, out)))
  return out

def array_split(ary,indices_or_sections,axis = 0):
    """
    Split an array into multiple sub-arrays.

    The only difference from numpy.array_split is we do not apply the
    kludge that 'fixes' the 0 length array dimentions. We try to preserve
    the original shape as much as possible, and only slice along axis

    Please refer to the ``split`` documentation.  The only difference
    between these functions is that ``array_split`` allows
    `indices_or_sections` to be an integer that does *not* equally
    divide the axis.

    See Also
    --------
    split : Split array into multiple sub-arrays of equal size.

    Examples
    --------
    >>> x = np.arange(8.0)
    >>> np.array_split(x, 3)
        [array([ 0.,  1.,  2.]), array([ 3.,  4.,  5.]), array([ 6.,  7.])]

    """
    try:
        Ntotal = numpy.array(ary.shape)[axis]
    except AttributeError:
        Ntotal = len(ary)
    try: # handle scalar case.
        Nsections = len(indices_or_sections) + 1
        div_points = [0] + list(indices_or_sections) + [Ntotal]
    except TypeError: #indices_or_sections is a scalar, not an array.
        Nsections = int(indices_or_sections)
        if Nsections <= 0:
            raise ValueError('number sections must be larger than 0.')
        Neach_section,extras = divmod(Ntotal,Nsections)
        section_sizes = [0] + \
                        extras * [Neach_section+1] + \
                        (Nsections-extras) * [Neach_section]
        div_points = numpy.array(section_sizes).cumsum()

    sub_arys = []
    sary = numpy.swapaxes(ary,axis,0)
    for i in range(Nsections):
        st = div_points[i]; end = div_points[i+1]
        sub_arys.append(numpy.swapaxes(sary[st:end],axis,0))

    return sub_arys


