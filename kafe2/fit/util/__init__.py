r"""
.. module:: kafe2.fit.util
    :platform: Unix
    :synopsis: This submodule provides utility functions for other modules

.. moduleauthor:: Johannes Gaessler <johannes.gaessler@student.kit.edu>
"""

import numpy as np

from . import function_library

# no __all__: import everything


# -- general utility functions

def string_join_if(pieces, delim='_', condition=lambda x: x):
    '''Join all elements of `pieces` that pass `condition` together
    using delimiter `delim`.'''
    return delim.join((p for p in pieces if condition(p)))


# -- array/matrix utility functions

def add_in_quadrature(*args):
    '''return the square root of the sum of squares of all arguments'''
    return np.sqrt(np.sum([_a**2 for _a in args], axis=0))

def invert_matrix(mat):
    '''perform matrix inversion'''
    #try:
    return np.linalg.inv(mat)
    #except np.linalg.LinAlgError:
    #    return None

def collect(*args):
    '''collect arguments into array'''
    return np.asarray(args)
