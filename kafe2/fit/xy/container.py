import numpy as np
import six

from ...core.error import MatrixGaussianError, SimpleGaussianError
from ..indexed import IndexedContainer
from ..indexed.container import IndexedContainerException


__all__ = ["XYContainer"]


class XYContainerException(IndexedContainerException):
    pass


class XYContainer(IndexedContainer):
    """
    This object is a specialized data container for *xy* data.

    """
    _AXIS_SPEC_DICT = {0:0, 1:1, '0':0, '1':1, 'x':0, 'y':1}

    #TODO Why does the XYContainer constructor require data while
    #     HistContainer and IndexedContainer don't?
    def __init__(self, x_data, y_data, dtype=float):
        """
        Construct a container for *xy* data:

        :param x_data: a one-dimensional array of measurement *x* values
        :type x_data: iterable of type <dtype>
        :param y_data: a one-dimensional array of measurement *y* values
        :type y_data: iterable of type <dtype>
        :param dtype: data type of the measurements
        :type dtype: type
        """
        # TODO: check user input (?)
        if len(x_data) != len(y_data):
            raise XYContainerException("x_data and y_data must have the same length!")
        self._xy_data = np.array([x_data, y_data], dtype=dtype)
        self._error_dicts = {}
        self._xy_total_errors = None
        self._full_cor_split_system = None


    # -- private methods

    @staticmethod
    def _find_axis_raise(axis_spec):
        try:
            axis_spec = axis_spec.lower()
        except AttributeError:
            # integers have no .lower() method
            pass
        _axis_id = XYContainer._AXIS_SPEC_DICT.get(axis_spec, None)
        if _axis_id is None:
            raise XYContainerException("No axis with id %r!" % (axis_spec,))
        return _axis_id

    def _get_data_for_axis(self, axis_id):
        return self._xy_data[axis_id]

    def _calculate_total_error(self):
        _sz = self.size
        _tmp_cov_mat_x = np.zeros((_sz, _sz))
        _tmp_cov_mat_y = np.zeros((_sz, _sz))
        for _err_dict in self._error_dicts.values():
            if not _err_dict['enabled']:
                continue
            assert _err_dict['axis'] in (0, 1)
            if _err_dict['axis'] == 0:
                _tmp_cov_mat_x += _err_dict['err'].cov_mat
            elif _err_dict['axis'] == 1:
                _tmp_cov_mat_y += _err_dict['err'].cov_mat

        _total_err_x = MatrixGaussianError(_tmp_cov_mat_x, 'cov', relative=False, reference=self.x)
        _total_err_y = MatrixGaussianError(_tmp_cov_mat_y, 'cov', relative=False, reference=self.y)
        self._xy_total_errors = [_total_err_x, _total_err_y]

    def _clear_total_error_cache(self):
        """recalculate total errors next time they are needed"""
        self._xy_total_errors = None
        self._full_cor_split_system = None

    def _calculate_uncor_cov_mat(self, axis):
        """calculate uncorrelated part of the covariance matrix"""

        _sz = self.size
        _tmp_uncor_cov_mat = np.zeros((_sz, _sz))

        for _err_dict in self._error_dicts.values():
            # skip disabled errors
            if not _err_dict['enabled']:
                continue

            # skip error for other axes
            if not _err_dict['axis'] == axis:
                continue

            # retrieve error object
            _err = _err_dict["err"]

            if isinstance(_err, MatrixGaussianError) or not _err_dict['splittable']:
                # cannot decorrelate full matrix errors: count as uncorrelated
                _tmp_uncor_cov_mat += _err.cov_mat
            else:
                # sum up uncorrelated parts
                _tmp_uncor_cov_mat += _err.cov_mat_uncor

        return np.array(_tmp_uncor_cov_mat)

    def _calculate_cor_nuisance_des_mat(self, axis):
        """calculate the design matrix describing a linear map between
        the nuisance parameters for the correlated uncertainties
        and the model predictions for an axis"""

        # retrieve all fully correlated errors for axis
        _axis_cor_errors = self.get_matching_errors(
            matching_criteria=dict(
                axis=axis,
                enabled=True,
                correlated=True,
                splittable=True
            )
        )

        _data_size = self.size
        _err_size = len(_axis_cor_errors)

        _des_mat = np.zeros((_err_size, _data_size))
        for _col, (_err_name, _err) in enumerate(six.iteritems(_axis_cor_errors)):
            _des_mat[_col, :] = _err.error_cor

        return _des_mat

    def _calculate_full_cor_split_system(self):
        self._full_cor_split_system = [
            (
                self._calculate_cor_nuisance_des_mat(axis),
                self._calculate_uncor_cov_mat(axis)
            )
            for axis in (0, 1)
        ]


    # -- public properties

    @property
    def size(self):
        """number of data points"""
        return self._xy_data.shape[1]

    @property
    def data(self):
        """container data (both *x* and *y*, two-dimensional :py:obj:`numpy.ndarray`)"""
        return self._xy_data.copy()  # copy to ensure no modification by user

    @data.setter
    def data(self, new_data):
        _new_data = np.asarray(new_data)
        if _new_data.ndim != 2:
            raise XYContainerException("XYContainer data must be 2-d array of floats! Got shape: %r..." % (_new_data.shape,))
        if _new_data.shape[0] == 2:
            self._xy_data = _new_data.copy()
        elif _new_data.shape[1] == 2:
            self._xy_data = _new_data.T.copy()
        else:
            raise XYContainerException(
                "XYContainer data length must be 2 in at least one axis! Got shape: %r..." % (_new_data.shape,))
        self._clear_total_error_cache()

    @property
    def x(self):
        """container *x* data (one-dimensional :py:obj:`numpy.ndarray`)"""
        return self._get_data_for_axis(0)

    @x.setter
    def x(self, new_x):
        _new_x_data = np.squeeze(np.array(new_x))
        if len(_new_x_data.shape) > 1:
            raise XYContainerException("XYContainer 'x' data must be 1-d array of floats! Got shape: %r..." % (_new_x_data.shape,))
        self._xy_data[0,:] = new_x
        for _err_dict in self._error_dicts.values():
            if _err_dict['axis'] == 0:
                _err_dict['err'].reference = self._get_data_for_axis(0)
        self._clear_total_error_cache()

    @property
    def x_err(self):
        """absolute total data *x*-uncertainties (one-dimensional :py:obj:`numpy.ndarray`)"""
        _total_error_x = self.get_total_error(axis=0)
        return _total_error_x.error

    @property
    def x_cov_mat(self):
        """absolute data *x* covariance matrix (:py:obj:`numpy.matrix`)"""
        _total_error_x = self.get_total_error(axis=0)
        return _total_error_x.cov_mat

    @property
    def x_cov_mat_inverse(self):
        """inverse of absolute data *x* covariance matrix (:py:obj:`numpy.matrix`), or ``None`` if singular"""
        _total_error_x = self.get_total_error(axis=0)
        return _total_error_x.cov_mat_inverse

    @property
    def x_cor_mat(self):
        """absolute data *x* correlation matrix (:py:obj:`numpy.matrix`)"""
        _total_error_x = self.get_total_error(axis=0)
        return _total_error_x.cor_mat

    @property
    def y(self):
        return self._get_data_for_axis(1)

    @y.setter
    def y(self, new_y):
        """container *y* data (one-dimensional :py:obj:`numpy.ndarray`)"""
        _new_y_data = np.squeeze(np.array(new_y))
        if len(_new_y_data.shape) > 1:
            raise XYContainerException("XYContainer 'y' data must be 1-d array of floats! Got shape: %r..." % (_new_y_data.shape,))
        self._xy_data[1,:] = new_y
        for _err_dict in self._error_dicts.values():
            if _err_dict['axis'] == 1:
                _err_dict['err'].reference = self._get_data_for_axis(1)
        self._clear_total_error_cache()

    @property
    def y_err(self):
        """absolute total data *y*-uncertainties (one-dimensional :py:obj:`numpy.ndarray`)"""
        _total_error_y = self.get_total_error(axis=1)
        return _total_error_y.error

    @property
    def y_cov_mat(self):
        """absolute data *y* covariance matrix (:py:obj:`numpy.matrix`)"""
        _total_error_y = self.get_total_error(axis=1)
        return _total_error_y.cov_mat

    @property
    def y_cov_mat_inverse(self):
        """inverse of absolute data *y* covariance matrix (:py:obj:`numpy.matrix`), or ``None`` if singular"""
        _total_error_y = self.get_total_error(axis=1)
        return _total_error_y.cov_mat_inverse

    @property
    def y_cor_mat(self):
        """absolute data *y* correlation matrix (:py:obj:`numpy.matrix`)"""
        _total_error_y = self.get_total_error(axis=1)
        return _total_error_y.cor_mat

    @property
    def x_range(self):
        """x data range"""
        _x = self.x
        return np.min(_x), np.max(_x)

    @property
    def y_range(self):
        """y data range"""
        _y = self.y
        return np.min(_y), np.max(_y)

    @property
    def y_uncor_cov_mat(self):
        # y uncorrelated covariance matrix
        _y_uncor_cov_mat = self._calculate_uncor_cov_mat(axis=1)
        return _y_uncor_cov_mat

    @property
    def y_uncor_cov_mat_inverse(self):
        # y uncorrelated inverse covariance matrix
        return np.linalg.inv(self.y_uncor_cov_mat)

    @property
    def x_uncor_cov_mat(self):
        # x uncorrelated covariance matrix
        return self._calculate_uncor_cov_mat(axis=0)

    @property
    def x_uncor_cov_mat_inverse(self):
        # x uncorrelated inverse covariance matrix
        return np.linalg.inv(self.x_uncor_cov_mat)

    # -- public methods

    def add_simple_error(self, axis, err_val, name=None, correlation=0, relative=False, splittable=True):
        """
        Add a simple uncertainty source for an axis to the data container.
        Returns an error id which uniquely identifies the created error source.

        :param axis: ``'x'``/``0`` or ``'y'``/``1``
        :type axis: str or int
        :param err_val: pointwise uncertainty/uncertainties for all data points
        :type err_val: float or iterable of float
        :param name: unique name for this uncertainty source. If ``None``, the name
                     of the error source will be set to a random alphanumeric string.
        :type name: str or ``None``
        :param correlation: correlation coefficient between any two distinct data points
        :type correlation: float
        :param relative: if ``True``, **err_val** will be interpreted as a *relative* uncertainty
        :type relative: bool
        :param splittable: if ``False``, the error will be marked as not splittable (see `set_error_splittable`)
        :type splittable: bool or ``None``
        :return: error id
        :rtype: int
        """
        _axis = self._find_axis_raise(axis)
        try:
            err_val.ndim   # will raise if simple float
        except AttributeError:
            err_val = np.asarray(err_val, dtype=float)

        if err_val.ndim == 0:  # if dimensionless numpy array (i.e. float64), add a dimension
            err_val = np.ones(self.size) * err_val

        _err = SimpleGaussianError(err_val=err_val, corr_coeff=correlation,
                                   relative=relative, reference=self._get_data_for_axis(_axis))
        _name = self._add_error_object(name=name, error_object=_err, axis=_axis, splittable=splittable and (correlation != 0))
        return _name

    def add_matrix_error(self, axis, err_matrix, matrix_type, name=None, err_val=None, relative=False):
        """
        Add a matrix uncertainty source for an axis to the data container.
        Returns an error id which uniquely identifies the created error source.

        :param axis: ``'x'``/``0`` or ``'y'``/``1``
        :type axis: str or int
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
        :return: error id
        :rtype: int
        """
        _axis = self._find_axis_raise(axis)
        _err = MatrixGaussianError(
            err_matrix=err_matrix, matrix_type=matrix_type, err_val=err_val,
            relative=relative, reference=self._get_data_for_axis(_axis)
        )
        _name = self._add_error_object(name=name, error_object=_err, axis=_axis, splittable=False)
        return _name

    def get_total_error(self, axis):
        """
        Get the error object representing the total uncertainty for an axis.

        :param axis: ``'x'``/``0`` or ``'y'``/``1``
        :type axis: str or int

        :return: error object representing the total uncertainty
        :rtype: :py:class:`~kafe2.core.error.MatrixGaussianError`
        """
        _axis = self._find_axis_raise(axis)
        if self._xy_total_errors is None:
            self._calculate_total_error()
        return self._xy_total_errors[_axis]

    def split_errors(self, axis):
        """Separate out fully correlated errors for an axis.

        Returns two matrices `G` and `U`, which are related to the
        total covariance matrix `V` by:

        .. math::
            V = G G^T + U

        The first matrix `G` contains the fully correlated components
        of the errors. `G(i, k)` represents the change in the `i`-th
        data point due to the `k`-th fully correlated error source.

        The second matrix `U` is the covariance matrix containing
        the remaining (non-fully-correlated) error contributions.
        """
        _axis = self._find_axis_raise(axis)
        if self._full_cor_split_system is None:
            self._calculate_full_cor_split_system()
        return self._full_cor_split_system[_axis]

    def get_shift_coefficients(self, axis, residuals):
        r"""Compute the shift coefficients for which a `residuals`
        vector shifted by all splittable fully correlated uncertainties
        together with the reduced covariance matrix containing only
        unsplittable or non-fully correlated uncertainties
        and a gaussian penalty for the coefficients themselves
        would yield the same gaussian penalty as the `residuals` with
        the full covariance matrix.

        Only the matrices for axis `axis` are considered.

        In short, this method finds the solution vector `b` to the equation:

        .. math::
            r^T V^{-1} r == (r - G b)^T U^{-1} (r - G b) + b^T b

        In the above, `r` is the residuals vector, `V` is the full covariance
        matrix, and `G` and `U` are the matrices of the split errors system
        as returned by `split_errors`.

        :param axis: ``'x'``/``0`` or ``'y'``/``1``
        :param residuals: str or int
        :return: array of expected nuisance
        :rtype: numpy.array
        """
        _v = self.get_total_error(axis).cov_mat

        _g, _u = self.split_errors(axis)

        try:
            _uinv = np.linalg.inv(_u)
        except np.linalg.LinAlgError:
            raise np.linalg.LinAlgError(
                "Cannot get shift coefficients: unsplittable "
                "part of covariance matrix is singular!")

        _eye = np.eye(_g.shape[0])
        return np.linalg.inv(_eye + _g.dot(_uinv).dot(_g.T)).dot(_g).dot(_uinv).dot(residuals)

    @property
    def has_x_errors(self):
        """``True`` if at least one *x* uncertainty source is defined for the data container"""
        for _err_dict in self._error_dicts.values():
            if _err_dict['axis'] == 0:
                return True
        return False

    @property
    def has_uncor_x_errors(self):
        """``True`` if at least one *x* uncertainty source, which is not fully correlated, is defined for the data container"""
        for _err_dict in self._error_dicts.values():
            if _err_dict['axis'] == 0 and _err_dict['err'].corr_coeff != 1.0:
                return True
        return False

    @property
    def has_y_errors(self):
        """``True`` if at least one *x* uncertainty source is defined for the data container"""
        for _err_dict in self._error_dicts.values():
            if _err_dict['axis'] == 1:
                return True
        return False


