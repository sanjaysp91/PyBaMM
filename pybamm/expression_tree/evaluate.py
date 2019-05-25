#
# Simplify a symbol
#
import pybamm

# need numpy imported for code generated in EvaluatorPython
import numpy as np  # noqa: F401
import scipy.sparse
from collections import OrderedDict


def id_to_python_variable(symbol_id, constant=False):
    if constant:
        var_format = "self.const_{:05d}"
    else:
        var_format = "self.var_{:05d}"
    return var_format.format(symbol_id).replace("-", "m")


def find_symbols(symbol, constant_symbols, variable_symbols):
    """
    This function converts an expression tree to a dictionary of node id's and strings
    specifying valid python code to calculate that nodes value, given y and t.

    The function distinguishes between nodes that represent constant nodes in the tree
    (e.g. a pybamm.Matrix), and those that are variable (e.g. subtrees that contain
    pybamm.StateVector). The former are put in `constant_symbols`, the latter in
    `variable_symbols`

    Note that it is important that the arguments `constant_symbols` and
    `variable_symbols` be and *ordered* dict, since the final ordering of the code lines
    are important for the calculations. A dict is specified rather than a list so that
    identical subtrees (which give identical id's) are not recalculated in the code

    Parameters
    ----------
    symbol : :class:`pybamm.Symbol`
        The symbol or expression tree to convert

    constant_symbol: collections.OrderedDict
        The output dictionary of constant symbol ids to lines of code

    variable_symbol: collections.OrderedDict
        The output dictionary of variable (with y or t) symbol ids to lines of code

    """
    pybamm.logger.debug("Convert to python: {!s}".format(symbol))

    if symbol.is_constant():
        constant_symbols[symbol.id] = symbol.evaluate()
        return

    # process children recursivly
    for child in symbol.children:
        find_symbols(child, constant_symbols, variable_symbols)

    # calculate the variable names that will hold the result of calcaulating the
    # children variables
    children_vars = [id_to_python_variable(child.id, child.is_constant())
                     for child in symbol.children]

    if isinstance(symbol, pybamm.BinaryOperator):
        # Multiplication and Division need special handling for scipy sparse matrices
        # TODO: we can pass through a dummy y and t to get the type and then hardcode
        # the right line, avoiding these checks
        if isinstance(symbol, pybamm.Multiplication):
            symbol_str = \
                "{0}.multiply({1}) if scipy.sparse.issparse({0}) else " \
                "{1}.multiply({0}) if scipy.sparse.issparse({1}) else " \
                "{0} * {1}".format(children_vars[0], children_vars[1])
        elif isinstance(symbol, pybamm.Division):
            symbol_str = \
                "{0}.multiply(1/{1}) if scipy.sparse.issparse({0}) else " \
                "{0} / {1}".format(children_vars[0], children_vars[1])
        else:
            symbol_str = children_vars[0] + ' ' + symbol.name + ' ' + children_vars[1]

    elif isinstance(symbol, pybamm.UnaryOperator):
        # For a Function we create two lines of code, one in constant_symbols that
        # contains the function handle, the other in variable_symbols that calls that
        # function on the child variable
        if isinstance(symbol, pybamm.Function):
            constant_symbols[symbol.id] = symbol.func
            funct_var = id_to_python_variable(symbol.id, True)
            symbol_str = "{}({})".format(funct_var, children_vars[0])

        # Index has a different syntax than other univariate operations
        elif isinstance(symbol, pybamm.Index):
            symbol_str = "{}[{}]".format(children_vars[0], symbol.index)
        else:
            symbol_str = symbol.name + children_vars[0]

    elif isinstance(symbol, pybamm.Concatenation):

        # don't bother to concatenate if there is only a single child
        if isinstance(symbol, pybamm.NumpyConcatenation):
            if len(children_vars) > 1:
                symbol_str = 'np.concatenate(({}))'.format(",".join(children_vars))
            else:
                symbol_str = '{}'.format(",".join(children_vars))

        elif isinstance(symbol, pybamm.SparseStack):
            symbol_str = "scipy.sparse.vstack(({}))".format(",".join(children_vars))

        # DomainConcatenation specifies a particular ordering for the concatenation,
        # which we must follow
        elif isinstance(symbol, pybamm.DomainConcatenation):
            slice_starts = []
            child_vectors = []
            for child_var, slices in zip(children_vars, symbol._children_slices):
                for child_dom, child_slice in slices.items():
                    slice_starts.append(symbol._slices[child_dom].start)
                    child_vectors.append("{}[{}:{}]".format(
                        child_var, child_slice.start, child_slice.stop
                    ))
            child_vectors = [v for _, v in sorted(zip(slice_starts, child_vectors))]
            if len(children_vars) > 1:
                symbol_str = "np.concatenate(({}))".format(",".join(child_vectors))
            else:
                symbol_str = '{}'.format(",".join(children_vars))
        else:
            raise NotImplementedError

    # Note: we assume that y is being passed as a column vector
    elif isinstance(symbol, pybamm.StateVector):
        symbol_str = symbol.name

    elif isinstance(symbol, pybamm.Time):
        symbol_str = 't'

    else:
        raise NotImplementedError(
            "Not implemented for a symbol of type '{}'".format(type(symbol))
        )

    variable_symbols[symbol.id] = symbol_str


def to_python(symbol):
    """
    This function converts an expression tree to a python function that acts like the
    tree's :func:`pybamm.Symbol.evaluate` function

    Parameters
    ----------
    symbol : :class:`pybamm.Symbol`
        The symbol to convert to a python function

    Returns
    -------
    str: string representing a python function with signature func(t=None, y=None)

    """

    constant_values = OrderedDict()
    variable_symbols = OrderedDict()
    find_symbols(symbol, constant_values, variable_symbols)

    line_format = "{} = {}"
    variable_lines = [
        #"print('{}'); ".format(line_format.format(
        #    id_to_python_variable(symbol_id, False),
        #    symbol_line
        #)) +
        line_format.format(
            id_to_python_variable(symbol_id, False),
            symbol_line
        )
        #+
        #"; print(type({0}),{0}.shape)".format(id_to_python_variable(symbol_id, False))
        for symbol_id, symbol_line in variable_symbols.items()
    ]

    return constant_values, "\n".join(variable_lines)


class EvaluatorPython:
    def __init__(self, symbol):
        constants, self._variable_function = pybamm.to_python(symbol)
        for symbol_id, value in constants.items():
            setattr(self, id_to_python_variable(
                symbol_id, True).replace("self.", ""), value)
        self._result_var = id_to_python_variable(symbol.id, symbol.is_constant())
        self._variable_compiled = compile(
            self._variable_function, self._result_var, 'exec')
        self._return_compiled = compile(
            self._result_var, 'return' + self._result_var, 'eval')

    def evaluate(self, t=None, y=None, known_evals=None):
        # generated code assumes y is a column vector
        if y is not None and y.ndim == 1:
            y = y.reshape(-1,1)

        # execute code
        exec(self._variable_compiled)

        # don't need known_evals, but need to reproduce Symbol.evaluate signature
        if known_evals is not None:
            return eval(self._return_compiled), known_evals
        else:
            return eval(self._return_compiled)
