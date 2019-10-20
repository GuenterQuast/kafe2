import abc
import numpy as np
import sys
import textwrap
import six

from collections import OrderedDict
from functools import partial

from kafe2.core.constraint import GaussianMatrixParameterConstraint, GaussianSimpleParameterConstraint
from ...tools import print_dict_as_table

from ...core import NexusFitter, Nexus
from ...core.fitters.nexus import Parameter, Alias, Empty, NexusError

from ...config import kc
from .container import DataContainerException
from ..io.file import FileIOMixin
from ..util import function_library, add_in_quadrature, collect, invert_matrix, string_join_if, zip_longest_dict

__all__ = ["FitBase", "FitException"]


class FitException(Exception):
    pass


@six.add_metaclass(abc.ABCMeta)
class FitBase(FileIOMixin, object):
    """
    This is a purely abstract class implementing the minimal interface required by all
    types of fitters.
    """

    CONTAINER_TYPE = None
    MODEL_TYPE = None
    PLOT_ADAPTER_TYPE = None
    EXCEPTION_TYPE = FitException
    RESERVED_NODE_NAMES = None

    # nexus nodes '<axis>_data' etc. are added for these
    AXES = None

    def __init__(self):
        self._data_container = None
        self._param_model = None
        self._nexus = None
        self._fitter = None
        self._poi_value_dict = None
        self._poi_names = None
        self._fit_param_names = None
        self._fit_param_constraints = None
        self._model_function = None
        self._cost_function = None
        self._loaded_result_dict = None  # contains potential fit results when loading from a file
        super(FitBase, self).__init__()

    # -- private methods

    def _add_property_to_nexus(self, prop, nexus=None, name=None, depends_on=None):
        '''register a property of this object in the nexus as a function node'''

        nexus = nexus if nexus is not None else self._nexus
        name = name if name is not None else prop

        _node = nexus.add_function(
            partial(getattr(self.__class__, prop).fget, self),
            func_name=name,
            par_names=tuple(),  # properties have no arguments
            existing_behavior='replace_if_empty'
        )

        # register explicit dependencies
        if depends_on is not None:
            nexus.add_dependency(name, depends_on=depends_on)

        return _node

    def _init_fit_parameters(self):

        # get names and default values of all model parameters
        self._poi_value_dict = self._get_default_values(
            model_function=self._model_function,
            x_name=getattr(self._model_function, 'x_name', None)
        )

        self._fit_param_names = list(self._poi_value_dict)
        self._poi_names = tuple(self._poi_value_dict)

    def _init_nexus(self, nexus=None):
        '''initialize a nexus or update an existing nexus'''
        # create and attach a nexus if none exists
        if nexus is None:
            nexus = self._nexus = Nexus()

        # pseudo-node for declaring dependencies on an external state
        nexus.add(Empty(name='_external'))

        # -- data and model-related nodes

        _added = {}
        for _axis in (self.AXES or (None,)):
            for _type in ('data', 'model'):
                for _prop in (None, 'error', 'cov_mat', 'cor_mat'):

                    _full_prop = string_join_if((_axis, _type, _prop))

                    try:
                        _node = self._add_property_to_nexus(
                            _full_prop,
                            nexus=nexus,
                            depends_on='_external'
                        )
                    except AttributeError:
                        # property not supported by object
                        continue
                    else:
                        _added[_full_prop] = _node

                    if _prop == 'cov_mat':
                        # add inverse cov mat node to nexus
                        nexus.add_function(
                            invert_matrix,
                            _full_prop + '_inverse',
                            par_names=(_full_prop,)
                        )

            # aggregate (i.e. 'total') properties
            for _prop in ('error', 'cov_mat'):

                # determine correct aggregation function
                if _prop == 'error':
                    _func = add_in_quadrature
                else:
                    _func = np.ndarray.__add__

                try:
                    _component_names = [
                        _added[string_join_if((_axis, _type, _prop))].name
                        for _type in ('data', 'model')
                    ]
                except KeyError:
                    # at least one component was not added before
                    pass
                else:
                    _full_prop = string_join_if((_axis, 'total', _prop))

                    nexus.add_function(
                        _func,
                        func_name=_full_prop,
                        par_names=_component_names
                    )

                # add inverse
                if _prop != 'error':
                    nexus.add_function(
                        invert_matrix,
                        func_name=_full_prop + '_inverse',
                        par_names=(_full_prop,),
                        existing_behavior='ignore'
                    )


        # -- concatenation of data and model across all axes

        if self.AXES is not None:
            for _type in ('data', 'model'):
                for _prop in (None, 'error', 'cov_mat', 'cor_mat'):
                    nexus.add_function(
                        lambda *args: np.array(args),
                        func_name=string_join_if((_type, _prop)),
                        par_names=[
                            string_join_if((_axis, _type, _prop))
                            for _axis in self.AXES
                        ]
                    )

        # -- parameters of interest (a.k.a model parameters, POIs)

        for _pn, _pv in six.iteritems(self._poi_value_dict):
            self._nexus.add(Parameter(_pv, name=_pn))

        self._add_property_to_nexus(
            'poi_values', nexus=nexus, depends_on=self._poi_names)
        self._add_property_to_nexus(
            'parameter_values', nexus=nexus, depends_on='poi_values')
        self._add_property_to_nexus(
            'parameter_constraints', nexus=nexus)

        # update parametric model when POIs change
        nexus.get('poi_values').register_callback(
            lambda: self.MODEL_TYPE.parameters.fset(
                self._param_model,
                self.poi_values
            )
        )

        # 'model' depends on both model and nuisance parameters
        nexus.add_dependency(
            'model',
            depends_on=(
                'parameter_values'
            )
        )

        # add the original function name as an alias for 'model'
        nexus.add_alias(
            self._model_function.name,
            alias_for='model',
            existing_behavior='ignore'  # allow 'model' as function name for model
        )

        # -- nuisance parameters

        # TODO: implement nuisance parameters

        #nexus.add_function(
        #    collect,
        #    func_name="nuisance_vector"
        #)

        # -- cost function

        # the cost function (the function to be minimized)
        _cost_node = nexus.add_function(
            self._cost_function.func,
            func_name='cost',
        )

        # add the cost function name as an alias for 'cost'
        try:
            nexus.add_alias(self._cost_function.name, alias_for='cost')
        except NexusError:
            pass  # allow 'cost' as function name for cost function

        return nexus

    @classmethod
    def _get_base_class(cls):
        return FitBase

    @classmethod
    def _get_object_type_name(cls):
        return 'fit'

    def _new_data_container(self, *args, **kwargs):
        """create a DataContainer of the right type for this fit"""
        return self.__class__.CONTAINER_TYPE(*args, **kwargs)

    def _new_parametric_model(self, *args, **kwargs):
        """create a ParametricModel of the right type for this fit"""
        return self.__class__.MODEL_TYPE(*args, **kwargs)

    def _new_plot_adapter(self, *args, **kwargs):
        """create a PlotAdapter of the right type for this fit"""
        if self.__class__.PLOT_ADAPTER_TYPE is None:
            raise NotImplementedError(
                "No `PlotAdapter` configured for fit type '{}'!".format(
                    self.__class__.__name__
                ))
        return self.__class__.PLOT_ADAPTER_TYPE(self, *args, **kwargs)

    def _validate_model_function_for_fit_raise(self):
        """make sure the supplied model function is compatible with the fit type"""
        # disallow using reserved keywords as model function arguments
        if not self.RESERVED_NODE_NAMES.isdisjoint(set(self._model_function.signature.parameters)):
            _invalid_args = self.RESERVED_NODE_NAMES.intersection(set(self._model_function.signature.parameters))
            raise self.__class__.EXCEPTION_TYPE(
                "The following names are reserved and cannot be used as model function arguments: %r"
                % (_invalid_args,))
        # disallow using reserved keywords as the model function name
        if self._model_function.name in self.RESERVED_NODE_NAMES:
            raise self.__class__.EXCEPTION_TYPE(
                "The following name is reserved and cannot be used as a model function name: %r"
                % (self._model_function.name,))

    def _initialize_fitter(self, minimizer=None, minimizer_kwargs=None):
        # save minimizer, minimizer_kwargs for serialization
        self._minimizer = minimizer
        self._minimizer_kwargs = minimizer_kwargs
        self._fitter = NexusFitter(nexus=self._nexus,
                                   parameters_to_fit=self._fit_param_names,
                                   parameter_to_minimize=self._cost_function.name,
                                   minimizer=minimizer,
                                   minimizer_kwargs=minimizer_kwargs)

    @staticmethod
    def _latexify_ascii(ascii_string):
        """function computing a fallback LaTeX representation of an plain-text string"""
        _lpn = ascii_string.replace('_', r"\_")
        return r"{\tt %s}" % (_lpn,)

    @staticmethod
    def _get_default_values(model_function=None, x_name=None):
        """
        :param model_function: model function handle
        :param x_name: name of the independent parameter
        :return: ordered dict with default values for fit parameters
        :rtype: dict
        """
        _nexus_new_dict = OrderedDict()
        for _par in model_function.signature.parameters.values():
            # skip independent variable parameter
            if _par.name == x_name:
                continue
            if _par.default == _par.empty:
                _nexus_new_dict[_par.name] = kc('core', 'default_initial_parameter_value')
            else:
                _nexus_new_dict[_par.name] = _par.default

        return _nexus_new_dict

    @abc.abstractmethod
    def _set_new_data(self, new_data): pass

    @abc.abstractmethod
    def _set_new_parametric_model(self): pass

    # Gets overwritten by multi models
    def _get_model_report_dict_entry(self):
        return self._model_function.formatter.get_formatted(
            with_par_values=False,
            n_significant_digits=2,
            format_as_latex=False,
            with_expression=True)

    def _update_parameter_formatters(self, update_asymmetric_errors=False):
        for _fpf, _pv, _pe in zip(
                self._model_function.argument_formatters, self.parameter_values, self.parameter_errors):
            _fpf.value = _pv
            _fpf.error = _pe
        if update_asymmetric_errors:
            for _fpf, _ape in zip(self._model_function.argument_formatters, self.asymmetric_parameter_errors):
                _fpf.asymmetric_error = _ape

    # -- public properties

    @property
    @abc.abstractmethod
    def data(self): pass

    @data.setter
    def data(self, new_data):
        """
        Sets new data of the fit Object
        :param new_data: Array or Data-Container with the new data
        """
        self._set_new_data(new_data)

        # validate cost function
        _data_and_cost_compatible, _reason = self._cost_function.is_data_compatible(self.data)
        if not _data_and_cost_compatible:
            raise self.EXCEPTION_TYPE('Fit data and cost function are not compatible: %s' % _reason)

        self._set_new_parametric_model()

        # TODO: check where to update this (set/release/etc.)
        # FIXME: nicer way than len()?
        self._cost_function.ndf = self._data_container.size - len(self._param_model.parameters)

    @property
    @abc.abstractmethod
    def model(self): pass

    @property
    def parameter_values(self):
        """the current parameter values"""
        if not self._fitter:
            return self.poi_values
        return tuple((self._fitter.fit_parameter_values[_pn] for _pn in self._fitter.parameters_to_fit))

    @property
    def parameter_names(self):
        """the current parameter names"""
        if not self._fitter:
            return self.poi_names
        return self._fitter.parameters_to_fit

    @property
    def parameter_errors(self):
        """the current parameter uncertainties"""
        if self._loaded_result_dict is not None:
            return self._loaded_result_dict['parameter_errors']
        else:
            return self._fitter.fit_parameter_errors

    @property
    def parameter_cov_mat(self):
        """the current parameter covariance matrix"""
        if self._loaded_result_dict is not None:
            return self._loaded_result_dict['parameter_cov_mat']
        else:
            return self._fitter.fit_parameter_cov_mat

    @property
    def parameter_cor_mat(self):
        """the current parameter correlation matrix"""
        if self._loaded_result_dict is not None:
            return self._loaded_result_dict['parameter_cor_mat']
        else:
            return self._fitter.fit_parameter_cor_mat

    @property
    def asymmetric_parameter_errors(self):
        """the current asymmetric parameter uncertainties"""
        if self._loaded_result_dict is not None and self._loaded_result_dict['asymmetric_parameter_errors'] is not None:
            return self._loaded_result_dict['asymmetric_parameter_errors']
        else:
            return self._fitter.asymmetric_fit_parameter_errors

    @property
    def parameter_name_value_dict(self):
        """a dictionary mapping each parameter name to its current value"""
        return self._fitter.fit_parameter_values if self._fitter else self._poi_value_dict

    @property
    def parameter_constraints(self):
        """the gaussian constraints given for the fit parameters"""
        return self._fit_param_constraints

    @property
    def cost_function_value(self):
        """the current value of the cost function"""
        return self._fitter.parameter_to_minimize_value

    @property
    def data_size(self):
        """the size (number of points) of the data container"""
        return self._data_container.size

    @property
    def has_model_errors(self):
        """``True`` if at least one uncertainty source is defined for the model"""
        return self._param_model.has_errors

    @property
    def has_data_errors(self):
        """``True`` if at least one uncertainty source is defined for the data"""
        return self._data_container.has_errors

    @property
    def has_errors(self):
        """``True`` if at least one uncertainty source is defined for either the data or the model"""
        return True if self.has_data_errors or self.has_model_errors else False

    @property
    def model_count(self):
        """the number of model functions contained in the fit, 1 by default"""
        return 1

    @property
    def poi_values(self):
        """the values of the parameters of interest, equal to ``self.parameter_values`` minus nuisance parameters"""
        return tuple((self.parameter_name_value_dict[_pn] for _pn in self.poi_names))

    @property
    def poi_names(self):
        """the names of the parameters of interest, equal to ``self.parameter_names`` minus nuisance parameter names"""
        return self._poi_names

    @property
    def did_fit(self):
        """whether a fit was performed for the given data and model"""
        if self._loaded_result_dict is not None:
            return self._loaded_result_dict['did_fit']
        else:
            return self._fitter.state_is_from_minimizer

    # -- public methods

    def set_parameter_values(self, **param_name_value_dict):
        """
        Set the fit parameters to new values. Valid keyword arguments are the names
        of the declared fit parameters.

        :param param_name_value_dict: new parameter values
        """
        return self._fitter.set_fit_parameter_values(**param_name_value_dict)

    def set_all_parameter_values(self, param_value_list):
        """
        Set all the fit parameters at the same time.

        :param param_value_list: list of parameter values (mind the order)
        """
        return self._fitter.set_all_fit_parameter_values(param_value_list)

    def fix_parameter(self, name, value=None):
        """
        Fix a parameter so that its value doesn't change when calling ``self.do_fit``.

        :param name: The name of the parameter to be fixed
        :type name: str
        :param value: The value to be given to the fixed parameter, optional
        :type value: float
        """
        self._fitter.fix_parameter(par_name=name, par_value=value)

    def release_parameter(self, par_name):
        """
        Release a fixed parameter so that its value once again changes when calling ``self.do_fit``.

        :param par_name: The name of the fixed parameter to be released
        :type par_name: str
        """
        self._fitter.release_parameter(par_name=par_name)

    def limit_parameter(self, par_name, par_limits):
        """
        Limit a parameter to a given range
        :param par_name: The name of the parameter to limited
        :type par_name: str
        :param par_limits: The range of the parameter to be limited to
        :type par_limits: tuple
        """
        self._fitter.limit_parameter(par_name, par_limits)

    def unlimit_parameter(self, par_name):
        """
        Unlimit a parameter
        :param par_name: The name of the parameter to unlimit
        :type par_name: str
        """
        self._fitter.unlimit_parameter(par_name)

    def add_matrix_parameter_constraint(self, names, values, matrix, matrix_type='cov', uncertainties=None,
                                        relative=False):
        """
        Advanced class for applying correlated constraints to several parameters of a fit.
        The order of ``names``, ``values``, ``matrix``, and ``uncertainties`` must be aligned.
        In other words the first index must belong to the first value, the first row/column in the matrix, etc.

        Let N be the number of parameters to be constrained.

        :param names: The names of the parameters to be constrained
        :type names: iterable of str, shape (N,)
        :param values: The values to which the parameters should be constrained
        :type values: iterable of float, shape (N,)
        :param matrix: The matrix that defines the correlation between the parameters. By default interpreted as a
            covariance matrix. Can also be interpreted as a correlation matrix by setting ``matrix_type``
        :type matrix: iterable of float, shape (N, N)
        :param matrix_type: Whether the matrix should be interpreted as a covariance matrix or as a correlation matrix
        :type matrix_type: str, either 'cov' or 'cor'
        :param uncertainties: The uncertainties to be used in conjunction with a correlation matrix
        :type uncertainties: ``None`` or iterable of float, shape (N,)
        :param relative: Whether the covariance matrix/the uncertainties should be interpreted as relative to ``values``
        :type relative: bool
        """
        if len(names) != len(values):
            raise self.EXCEPTION_TYPE(
                'Lengths of names and values are different: %s <-> %s' % (len(names), len(values)))
        _par_indices = []
        for _name in names:
            try:
                _par_indices.append(self.poi_names.index(_name))
            except ValueError:
                raise self.EXCEPTION_TYPE('Unknown parameter name: %s' % _name)
        self._fit_param_constraints.append(GaussianMatrixParameterConstraint(
            indices=_par_indices, values=values, matrix=matrix, matrix_type=matrix_type, uncertainties=uncertainties,
            relative=relative
        ))

    def add_parameter_constraint(self, name, value, uncertainty, relative=False):
        """
        Simple class for applying a gaussian constraint to a single fit parameter.

        :param name: The name of the parameter to be constrained
        :type name: str
        :param value: The value to which the parameter should be constrained
        :type value: float
        :param uncertainty: The uncertainty with which the parameter should be constrained to the given value
        :type uncertainty: float
        :param relative: Whether the given uncertainty is relative to the given value
        :type relative: bool
        """
        try:
            _index = self.poi_names.index(name)
        except ValueError:
            raise self.EXCEPTION_TYPE('Unknown parameter name: %s' % name)
        self._fit_param_constraints.append(GaussianSimpleParameterConstraint(
            index=_index, value=value, uncertainty=uncertainty, relative=relative
        ))

    def get_matching_errors(self, matching_criteria=None, matching_type='equal'):
        """
        Return a list of uncertainty objects fulfilling the specified
        matching criteria.

        Valid keys for ``matching_criteria``:

            * ``name`` (the unique error name)
            * ``type`` (either ``'simple'`` or ``'matrix'``)
            * ``correlated`` (bool, only matches simple errors!)
            * ``reference`` (either ``'model'`` or ``'data'``)

        NOTE: The error objects contained in the dictionary are not copies,
        but the original error objects.
        Modifying them is possible, but not recommended. If you do modify any
        of them, the changes will not be reflected in the total error calculation
        until the error cache is cleared. This can be done by calling the
        private method
        :py:meth:`~kafe2.fit._base.container.DataContainerBase._clear_total_error_cache`.

        :param matching_criteria: key-value pairs specifying matching criteria.
                                  The resulting error array will only contain
                                  error objects matching *all* provided criteria.
                                  If ``None``, all error objects are returned.
        :type matching_criteria: dict or ``None``
        :param matching_type: how to perform the matching. If ``'equal'``, the
                              value in ``matching_criteria`` is checked for equality
                              against the stored value. If ``'regex', the
                              value in ``matching_criteria`` is interpreted as a regular
                              expression and is matched against the stored value.
        :type matching_type: ``'equal'`` or ``'regex'``
        :return: list of error objects
        :rtype: dict mapping error name to `~kafe2.core.error.GausianErrorBase`-derived
        """
        if matching_criteria is not None:
            _crit_ref_value = matching_criteria.pop('reference', None)
            if _crit_ref_value == 'data':
                return self._data_container.get_matching_errors(matching_criteria, matching_type=matching_type)
            elif _crit_ref_value == 'model':
                return self._param_model.get_matching_errors(matching_criteria, matching_type=matching_type)
            elif _crit_ref_value is None:
                pass  # don't raise, continue evaluation below
            else:
                raise ValueError("Unknown value '{}' for matching "
                                 "criterion 'reference'. Valid: 'data', 'model' or None".format(_crit_ref_value))

        _result = self._data_container.get_matching_errors(matching_criteria, matching_type=matching_type)
        _result_model = self._param_model.get_matching_errors(matching_criteria, matching_type=matching_type)

        # be paranoid about collisions
        for _k in _result_model:
            assert _k not in _result # FATAL: there is an error with the same name in the data and model containers
            _result[_k] = _result_model[_k]

        return _result

    def add_simple_error(self, err_val, name=None, correlation=0, relative=False, reference='data', **kwargs):
        """
        Add a simple uncertainty source to the data container.
        Returns an error id which uniquely identifies the created error source.

        :param err_val: pointwise uncertainty/uncertainties for all data points
        :type err_val: float or iterable of float
        :param name: unique name for this uncertainty source. If ``None``, the name
                     of the error source will be set to a random alphanumeric string.
        :type name: str or ``None``
        :param correlation: correlation coefficient between any two distinct data points
        :type correlation: float
        :param relative: if ``True``, **err_val** will be interpreted as a *relative* uncertainty
        :type relative: bool
        :param reference: which reference values to use when calculating absolute errors from relative errors
        :type reference: 'data' or 'model'
        :return: error id
        :rtype: int
        """
        if reference == 'data':
            # delegate to data container
            _reference_object = self._data_container
        elif reference == 'model':
            # delegate to model container
            _reference_object = self._param_model
        else:
            raise FitException("Cannot add simple error: unknown reference "
                               "specification '{}', expected one of: 'data', 'model'...".format(reference))

        _ret = _reference_object.add_simple_error(err_val=err_val,
                                                  name=name, correlation=correlation, relative=relative, **kwargs)

        return _ret

    def add_matrix_error(self, err_matrix, matrix_type,
                         name=None, err_val=None, relative=False, reference='data', **kwargs):
        """
        Add a matrix uncertainty source for use in the fit.
        Returns an error id which uniquely identifies the created error source.

        :param err_matrix: covariance or correlation matrix
        :param matrix_type: one of ``'covariance'``/``'cov'`` or ``'correlation'``/``'cor'``
        :type matrix_type: str
        :param name: unique name for this uncertainty source. If ``None``, the name
                     of the error source will be set to a random alphanumeric string.
        :type name: str or ``None``
        :param err_val: the pointwise uncertainties (mandatory if only a correlation matrix is given)
        :type err_val: iterable of float
        :param relative: if ``True``, the covariance matrix and/or **err_val** will be interpreted as a *relative* uncertainty
        :type relative: bool
        :param reference: which reference values to use when calculating absolute errors from relative errors
        :type reference: 'data' or 'model'
        :return: error id
        :rtype: int
        """
        if reference == 'data':
            # delegate to data container
            _reference_object = self._data_container
        elif reference == 'model':
            # delegate to model container
            _reference_object = self._param_model
        else:
            raise FitException("Cannot add matrix error: unknown reference "
                               "specification '{}', expected one of: 'data', 'model'...".format(reference))

        _ret = _reference_object.add_matrix_error(err_matrix=err_matrix, matrix_type=matrix_type,
                                                  name=name, err_val=err_val, relative=relative, **kwargs)

        return _ret

    def disable_error(self, err_id):
        """
        Temporarily disable an uncertainty source so that it doesn't count towards calculating the
        total uncertainty.

        :param err_id: error id
        :type err_id: int
        """
        try:
            # try to find error in data container
            _ret = self._data_container.disable_error(err_id)
        except DataContainerException:
            # try to find error in model container
            _ret = self._param_model.disable_error(err_id)

        return _ret

    def do_fit(self):
        """
        Perform the minimization of the cost function.
        """
        if self._cost_function.needs_errors and not self._data_container.has_errors:
            self._cost_function.on_no_errors()
        self._fitter.do_fit()
        self._loaded_result_dict = None
        self._update_parameter_formatters()

    def assign_model_function_expression(self, expression_format_string):
        """Assign a plain-text-formatted expression string to the model function."""
        self._model_function.formatter.expression_format_string = expression_format_string

    def assign_model_function_latex_name(self, latex_name):
        """Assign a LaTeX-formatted string to be the model function name."""
        self._model_function.formatter.latex_name = latex_name

    def assign_model_function_latex_expression(self, latex_expression_format_string):
        """Assign a LaTeX-formatted expression string to the model function."""
        self._model_function.formatter.latex_expression_format_string = latex_expression_format_string

    def assign_parameter_latex_names(self, **par_latex_names_dict):
        """Assign LaTeX-formatted strings to the model function parameters."""
        for _pf in self._model_function.argument_formatters:
            _pln = par_latex_names_dict.get(_pf.name, None)
            if _pln is not None:
                _pf.latex_name = _pln

    def generate_plot(self):
        from kafe2.fit._base.plot import Plot
        return Plot(self)

    def get_result_dict_for_robots(self):
        """Return a dictionary of the fit results."""
        _result_dict = dict()

        _result_dict['did_fit'] = self.did_fit
        _cost = float(self.cost_function_value)  # convert numpy scalar to float for yaml representation
        _ndf = self._cost_function.ndf
        _result_dict['cost'] = _cost
        _result_dict['ndf'] = _ndf
        _result_dict['cost/ndf'] = _cost / _ndf
        _result_dict['parameter_values'] = self.parameter_values
        if _result_dict['did_fit']:
            _result_dict['parameter_cov_mat'] = self.parameter_cov_mat
            _result_dict['parameter_errors'] = self.parameter_errors
            _result_dict['parameter_cor_mat'] = self.parameter_cor_mat
        else:
            _result_dict['parameter_cov_mat'] = None
            _result_dict['parameter_errors'] = None
            _result_dict['parameter_cor_mat'] = None

        if self._loaded_result_dict is not None and self._loaded_result_dict['asymmetric_parameter_errors'] is not None:
            _result_dict['asymmetric_parameter_errors'] = self._loaded_result_dict['asymmetric_parameter_errors']
        else:
            _result_dict['asymmetric_parameter_errors'] = self._fitter.asymmetric_fit_parameter_errors_if_calculated

        return _result_dict

    def report(self, output_stream=sys.stdout, asymmetric_parameter_errors=False):
        """
        Print a summary of the fit state and/or results.

        :param output_stream: the output stream to which the report should be printed
        :type output_stream: TextIOBase
        :param asymmetric_parameter_errors: if ``True``, use two different parameter errors for up/down directions
        :type asymmetric_parameter_errors: bool
        """

        _indent = ' ' * 4

        output_stream.write(textwrap.dedent("""
                    ###############
                    # Fit Results #
                    ###############

                """))

        if not self.did_fit:
            output_stream.write('WARNING: No fit has been performed as of yet. Did you forget to run fit.do_fit()?\n\n')

        output_stream.write(_indent + "Model Parameters\n")
        output_stream.write(_indent + "================\n\n")

        self._update_parameter_formatters(update_asymmetric_errors=asymmetric_parameter_errors)
        for _pf in self._model_function.argument_formatters:
            output_stream.write(_indent * 2)
            output_stream.write(
                _pf.get_formatted(with_name=True,
                                  with_value=True,
                                  with_errors=True,
                                  format_as_latex=False,
                                  asymmetric_error=asymmetric_parameter_errors)
            )
            output_stream.write('\n')
        output_stream.write('\n')

        output_stream.write(_indent + "Model Parameter Correlations\n")
        output_stream.write(_indent + "============================\n\n")

        _cor_mat_content = self.parameter_cor_mat
        if _cor_mat_content is not None:
            _cor_mat_as_dict = OrderedDict()
            _cor_mat_as_dict['_invisible_first_column'] = self._fit_param_names
            for _par_name, _row in zip(self._fit_param_names, self.parameter_cor_mat.T):
                _cor_mat_as_dict[_par_name] = np.atleast_1d(np.squeeze(np.asarray(_row)))

            print_dict_as_table(_cor_mat_as_dict, output_stream=output_stream, indent_level=2)
        else:
            output_stream.write(_indent * 2 + '<not available>\n')
        output_stream.write('\n')

        output_stream.write(_indent + "Cost Function\n")
        output_stream.write(_indent + "=============\n\n")

        _pf = self._cost_function._formatter
        output_stream.write(_indent * 2 + "cost function: {}\n\n".format(_pf.description))
        output_stream.write(_indent * 2 + "cost / ndf = ")
        output_stream.write(
            _pf.get_formatted(value=self.cost_function_value,
                              n_degrees_of_freedom=self._cost_function.ndf,
                              with_name=False,
                              with_value_per_ndf=True,
                              format_as_latex=False)
        )
        output_stream.write('\n')

    def to_file(self, filename, format=None, calculate_asymmetric_errors=False):
        """Write kafe2 object to file"""
        if calculate_asymmetric_errors:
            self.asymmetric_parameter_errors
        super(FitBase, self).to_file(filename=filename, format=None)
