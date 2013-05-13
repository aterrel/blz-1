from __future__ import absolute_import

from .datashape.util import broadcastable
from .datashape.coretypes import DataShape
from .datadescriptor.blaze_func_descriptor import BlazeFuncDescriptor
from .array import Array
from .cgen.utils import letters
from .blaze_kernels import KernelObj, Argument, fuse_kerneltree


# A KernelTree is just the bare element-wise kernel functions
# (no arguments).  Any arguments are identified as unique-names
# in an abstract name-space
# All nodes in the kernel tree can also be named
#   or else a unique-name
# from the abstract name-space will be created.
# Each KernelTree has a single llvm module name-space
class KernelTree(object):
    _stream_of_unique_names = letters()
    _stream_of_unique_kernels = letters()

    def __init__(self, node, children=[], name=None):
        assert isinstance(node, KernelObj)
        for el in children:
            assert isinstance(el, (KernelTree, Argument))
        self.node = node
        self.children = children
        if name is None:
            name = 'node_' + next(self._stream_of_unique_names)
        self.name = name

    def sorted_nodes(self):
        """Return depth-first list of unique KernelTree Nodes.

        The root of the tree will be the last node.
        """
        nodes = []
        self.visit(nodes)
        return nodes

    @property
    def leafnode(self):
        return all(isinstance(child, Argument) for child in self.children)

    def visit(self, nodes):
        if not self.leafnode:
            for child in self.children:
                if isinstance(child, KernelTree):
                    child.visit(nodes)
        if self not in nodes:
            nodes.append(self)

    def fuse(self, name=None):
        if name is None:
            name = 'flat_' + self.name
        krnlobj, children = fuse_kerneltree(self, name)
        return KernelTree(krnlobj, children)

    def flatten_tree(self, name=None):
        """Take a composite kernel tree and flatten it creating a single
        node object with a fused kernel and all children as Arguments.

        """
        if name is None:
            name = 'flat_' + self.name + next(self._stream_of_unique_kernels)

        return kernel

# Convert list of comma-separated strings into a list of integers showing
#  the rank of each argument and a list of sets of tuples.  Each set
#  shows dimensions of arguments that must match by indicating a 2-tuple
#  where the first integer is the argument number (output is argument 0) and
#  the second integer is the dimension
# Example:  If the rank-signature is ['M,L', 'L,K', 'K', '']
#           then the list of integer ranks is [2, 2, 1, 0]
#           while the list of connections is [{(0,1),(1,0)},{(1,1),(2,0)}]
def process_signature(ranksignature):
    ranklist = [0 if not arg else len(arg.split(',')) for arg in ranksignature]

    varmap = {}
    for i, arg in enumerate(ranksignature):
        if not arg:
            continue
        for k, var in enumerate(arg.split(',')):
            varmap.setdefault(var, []).append((i, k))

    connections = [set(val) for val in varmap.values() if len(val) > 1]
    return ranklist, connections


# Process type-table dictionary which maps a signature list with
#   (input-type1, input-type2, output_type) to a kernel into a
#   lookup-table dictionary which maps a input-only signature list
#   with a tuple of the output-type plus the signature
def process_typetable(typetable):
    newtable = {}
    for key, value in typetable.iteritems():
        newtable[key[:-1]] = (key[-1], value)
    return newtable

# Define the Blaze Function
#   * A Blaze Function is a callable that takes Concrete Arrays and returns
#        Deferred Concrete Arrays
#   * At the core of the Blaze Function is a kernel which is a type-resolved
#        element-wise expression graph where elements can be any contiguous
#        primitive type (right-most part of the data-shape)
#   * Kernels have a type signature which we break up into the rank-signature
#       and the primitive type signature because a BlazeFunc will have one
#       rank-signature but possibly multiple primitive type signatures.
#   * Kernels for a particular type might be inline jitted or loaded from
#       a shared-library --- uses BLIR kernel engine on-top of LLVM.
#   * Example BlazeFuncs are sin, svd, eig, fft, sum, prod, inner1d, add, mul
#       etc --- kernels all work on in-memory "elements"

class BlazeFunc(object):
    def __init__(self, name, ranksignature, typetable, inouts=[]):
        """
        Construct a Blaze Function from a rank-signature and keyword arguments.

        The typetable is a dictionary with keys a tuple of types
        and values as corresponding BlazeScalarKernel objects.  The
        tuple of types has Input types first with the Output Type last

        Arguments
        =========
        ranksignature : ['name1,M', 'M,name3', 'L']
                        a list of comma-separated strings where names indicate
                        unique sizes.  The last argument is the rank-signature
                        of the output.  An empty-string or None means a scalar.

        typetable :  dictionary mapping argument types to an implementation
                     kernel which is an instance of a BlazeScalarKernel object

        inouts : list of integers corresponding to arguments which are
                  input and output arguments
        """
        self.name = name

        # FIXME:  Think about merging the dispatch table and rank-signature
        #         so that dispatch can occur on different rank
        self.ranks, self.rankconnect = process_signature(ranksignature)
        self.dispatch = process_typetable(typetable)
        self.inouts = inouts

    @property
    def nin(self):
        return len(self.ranks)-1

    def compatible(self, args):
        # check for broadcastability
        # TODO: figure out correct types as well
        dshapes = [arg._data.dshape for arg in args]
        return broadcastable(dshapes, self.ranks,
                                rankconnect=self.rankconnect)

    def __call__(self, *args, **kwds):
        # convert inputs to Arrays
        # build an AST and return Arrays with a Deferred Data Descriptor
        # The eval method of the Array does the actual computation
        #args = map(blaze.asarray, args)


        # FIXME:  The compatible function should unify a suitable
        #          function even if the types are not all the same

        # Find the kernel from the dispatch table
        types = tuple(arr._data.dshape.measure for arr in args)
        out_type, kernel = self.dispatch[types]

        # Check rank-signature compatibility and broadcastability of arguments
        outshape = self.compatible(args)

        # Construct output dshape
        outdshape = DataShape(outshape+(out_type,))

        kernelobj = KernelObj(kernel, self.ranks, self.name)

        # Create a new BlazeFuncDescriptor with this
        # kerneltree and a new set of args depending on
        #  unique new arguments in the expression. 
        children = []
        for i, arg in enumerate(args):
            data = arg._data
            if isinstance(data, BlazeFuncDescriptor):
                children.append(data.kerneltree)
            else:
                tree_arg = Argument(arg, kernel.kind[i],
                                    self.ranks[i], kernel.argtypes[i])
                children.append(tree_arg)

        kerneltree = KernelTree(kernelobj, children)
        data = BlazeFuncDescriptor(kerneltree, outdshape)

        # Construct an Array object from new data descriptor
        # Standard propagation of user-defined meta-data.
        user = {self.name: [arg.user for arg in args]}

        # FIXME:  Check for axes alignment and labels alignment
        axes = args[0].axes
        labels = args[0].labels

        return Array(data, axes, labels, user)



