# Copyright 2013 Novo Nordisk Foundation Center for Biosustainability,
# Technical University of Denmark.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""
Interface for the Python MIP (Mixed-Integer Linear Programming) Tools.

Wraps the MIP solver by subclassing and extending :class:`Model`,
:class:`Variable`, and :class:`Constraint` from :mod:`interface`.

MIP is an open source MILP Python wrapper around the open-source COIN-OR
Branch-&-Cut CBC solver. To use MIP you need to install the 'mip'
python package (with pip or from https://www.python-mip.com/)
and make sure that 'import mip' runs without error.
"""
import logging

import os
import six

from optlang.util import inheritdocstring, TemporaryFilename
from optlang.expression_parsing import parse_optimization_expression
from optlang import interface
from optlang import symbolics

import mip
from math import isclose, ceil, floor

log = logging.getLogger(__name__)

_MIP_STATUS_TO_STATUS = {
    mip.OptimizationStatus.CUTOFF: interface.CUTOFF,
    mip.OptimizationStatus.ERROR: interface.ABORTED,
    mip.OptimizationStatus.FEASIBLE: interface.FEASIBLE,
    mip.OptimizationStatus.INFEASIBLE: interface.INFEASIBLE,
    mip.OptimizationStatus.INT_INFEASIBLE: interface.SPECIAL,
    mip.OptimizationStatus.LOADED: interface.LOADED,
    mip.OptimizationStatus.NO_SOLUTION_FOUND: interface.NOFEASIBLE,
    mip.OptimizationStatus.OPTIMAL: interface.OPTIMAL,
    mip.OptimizationStatus.UNBOUNDED: interface.UNBOUNDED,
    mip.OptimizationStatus.OTHER: interface.SPECIAL
}

_MIP_VTYPE_TO_VTYPE = {
    mip.CONTINUOUS: 'continuous',
    mip.INTEGER: 'integer',
    mip.BINARY: 'binary'
}

_VTYPE_TO_MIP_VTYPE = dict(
    [(val, key) for key, val in six.iteritems(_MIP_VTYPE_TO_VTYPE)]
)

_DIR_TO_MIP_DIR = {
    'max': mip.MAXIMIZE,
    'min': mip.MINIMIZE
}

_MIP_DIR_TO_DIR = dict(
    [(val, key) for key, val in six.iteritems(_DIR_TO_MIP_DIR)]
)

# Needs to be used for to_bound during _initialize_model_from_problem
INFINITY = 1.7976931348623157e+308

def to_float(number, is_lb=True):
    """Converts None type and sympy.core.numbers.Float to float."""
    if number is not None:
        return float(number)
    if is_lb:
        return -INFINITY
    return INFINITY

def to_bound(number):
    """Convert float with infs to None."""
    if abs(number) == INFINITY:
        return None
    return number

def to_symbolic_expr(coeffs):
    """Converts coeffs dict to symbolic expression."""
    return symbolics.add([var * coef for var, coef in coeffs.items()
                          if not isclose(0, to_float(coef))])

@six.add_metaclass(inheritdocstring)
class Variable(interface.Variable):
    def __init__(self, name, *args, **kwargs):
        super(Variable, self).__init__(name, **kwargs)

    @interface.Variable.lb.setter
    def lb(self, value):
        interface.Variable.lb.fset(self, value)
        if self.problem is not None:
            self.problem._update_var_lb(self.name, value)

    @interface.Variable.ub.setter
    def ub(self, value):
        interface.Variable.ub.fset(self, value)
        if self.problem is not None:
            self.problem._update_var_ub(self.name, value)

    def set_bounds(self, lb, ub):
        super(Variable, self).set_bounds(lb, ub)
        if self.problem is not None:
            self.problem._update_var_lb(self.name, lb)
            self.problem._update_var_ub(self.name, ub)

    @interface.Variable.type.setter
    def type(self, value):
        if not value in _VTYPE_TO_MIP_VTYPE:
            raise ValueError(
                'COIN-OR CBC cannot handle variables of type %s. ' % value +
                'The following variable types are available:\n' +
                ' '.join(_VTYPE_TO_MIP_VTYPE.keys()))

        if self.problem is not None:
            self.problem._update_var_type(self.name, _VTYPE_TO_MIP_VTYPE[value])
        interface.Variable.type.fset(self, value)

        if value == 'integer':
            if self.lb is not None:
                self.lb = ceil(self.lb)
            if self.ub is not None:
                self.ub = floor(self.ub)
        elif value == 'binary':
            self.lb, self.ub = 0, 1

    @property
    def primal(self):
        if self.problem is None:
            return None
        return self.problem._var_primal(self.name)

    @property
    def dual(self):
        if self.type != 'continuous':
            raise ValueError('Dual is only available for continuous variables')
        if self.problem is None:
            return None
        return self.problem._var_dual(self.name)

    @interface.Variable.name.setter
    def name(self, value):
        super(Variable, Variable).name.fset(self, value)
        if getattr(self, 'problem', None) is not None:
            raise Exception('COIN-OR CBC doesn\'t support variable name change')


@six.add_metaclass(inheritdocstring)
class Constraint(interface.Constraint):
    _INDICATOR_CONSTRAINT_SUPPORT = False

    def __init__(self, expression, sloppy=False, *args, **kwargs):
        super(Constraint, self).__init__(expression, sloppy=sloppy, *args, **kwargs)

    def constraint_name(self, is_lb):
        if is_lb:
            return self.name + '_lower'
        return self.name + '_upper'

    @property
    def primal(self):
        if getattr(self, 'problem', None) is not None \
           and self.problem.status == interface.OPTIMAL:
            if self.lb is not None:
                return self.problem._constr_primal(self, True)
            if self.ub is not None:
                return self.problem._constr_primal(self, False)
        return None

    @property
    def dual(self):
        if getattr(self, 'problem', None) is None:
            return None
        if self.lb is not None:
            return self.problem._constr_dual(self, True)
        if self.ub is not None:
            return self.problem._constr_dual(self, False)
        return None

    def _update_bound(self, new, old, is_lb):
        """Updates associated mip model with new constraint bounds."""
        if getattr(self, 'problem', None) is None:
            return

        if old is None and new is not None:
            self.problem._add_mip_constraint(self, is_lb)
        elif new is None and old is not None:
            self.problem._remove_mip_constraint(self, is_lb)
        elif new is not None and old is not None:
            self.problem._update_constraint_bound(self, is_lb)

    @interface.Constraint.lb.setter
    def lb(self, value):
        self._check_valid_lower_bound(value)
        self._lb, old_lb = value, getattr(self, '_lb', None)
        self._update_bound(value, old_lb, True)

    @interface.Constraint.ub.setter
    def ub(self, value):
        self._check_valid_upper_bound(value)
        self._ub, old_ub = value, getattr(self, '_ub', None)
        self._update_bound(value, old_ub, False)

    @interface.Constraint.name.setter
    def name(self, value):
        super(Constraint, Constraint).name.fset(self, value)
        if getattr(self, 'problem', None) is not None:
            raise Exception('COIN-OR CBC doesn\'t support constraint name change')

    def set_linear_coefficients(self, coefficients):
        if self.problem is None:
            raise Exception('Can\'t change coefficients if constraint is not associated with a model.')

        self.problem.update()
        coeffs = self._expression.as_coefficients_dict()
        coeffs.update(coefficients)
        self._expression = to_symbolic_expr(coeffs)

        # _remove_constraints deletes reference to self.problem
        problem = self.problem
        problem._remove_constraints([self])
        problem._add_constraints([self])

    def get_linear_coefficients(self, variables):
        if self.problem is None:
            raise Exception('Can\'t get coefficients from solver if constraint is not in a model')

        self.problem.update()
        coeffs = self._expression.as_coefficients_dict()
        return {v: coeffs.get(v, 0) for v in variables}


@six.add_metaclass(inheritdocstring)
class Objective(interface.Objective):
    def __init__(self, expression, sloppy=False, **kwargs):
        super(Objective, self).__init__(expression, sloppy=sloppy, **kwargs)
        if not (sloppy or self.is_Linear):
            raise ValueError(
                'COIN-OR CBC only supports linear objectives. %s is not linear.' % self)

    @property
    def value(self):
        if getattr(self, 'problem', None) is None:
            return None
        return self.problem.problem.objective_value

    @interface.Objective.direction.setter
    def direction(self, value):
        super(Objective, self.__class__).direction.fset(self, value)
        if getattr(self, 'problem', None) is not None:
            self.problem.problem.sense = _DIR_TO_MIP_DIR[value]

    def set_linear_coefficients(self, coefficients):
        if self.problem is None:
            raise Exception('Can\'t change coefficients if objective is not associated with a model.')

        self.problem.update()
        coeffs = self._expression.as_coefficients_dict()
        coeffs.update(coefficients)
        self._expression = to_symbolic_expr(coeffs)
        self.problem.objective = self


    def get_linear_coefficients(self, variables):
        if self.problem is None:
            raise Exception('Can\'t get coefficients from solver if objective is not in a model')

        self.problem.update()
        coeffs = self._expression.as_coefficients_dict()
        return {v: coeffs.get(v, 0) for v in variables}


@six.add_metaclass(inheritdocstring)
class Configuration(interface.MathematicalProgrammingConfiguration):
    def __init__(self, verbosity=0, tolerance=1e-10, timeout=float('inf'),
                 max_nodes=None, max_solutions=None, relax=False, *args, **kwargs):
        super(Configuration, self).__init__(*args, **kwargs)
        self.verbosity = verbosity
        # Tolerance for the quality of the optimal solution, if a solution with
        # cost c and a lower bound b are available and c - b < mip_gap_abs,
        # the search will be concluded
        self.tolerance = tolerance
        # Time limit in seconds for search
        self.timeout = timeout
        # Maximum number of nodes to be explored in the search tree
        self.max_nodes = max_nodes
        # Solution limit, search will be stopped when max_solutions are found
        self.max_solutions = max_solutions
        # If true only the linear programming relaxation will be solved, i.e.
        # integrality constraints will be temporarily discarded. Changes the
        # type of all integer and binary variables to continuous. Bounds are preserved.
        self.relax = relax

    @property
    def verbosity(self):
        return self._verbosity

    @verbosity.setter
    def verbosity(self, value):
        self._verbosity = value

    @property
    def presolve(self):
        return False

    @presolve.setter
    def presolve(self, value):
        # TODO: implement if possible
        if value:
            raise ValueError('Not implemented')

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, value):
        self._timeout = value

    @property
    def tolerance(self):
        return self._tolerance

    @tolerance.setter
    def tolerance(self, value):
        self._tolerance = value

    @property
    def max_nodes(self):
        return self._max_nodes

    @max_nodes.setter
    def max_nodes(self, value):
        self._max_nodes = value

    @property
    def max_solutions(self):
        return self._max_solutions

    @max_solutions.setter
    def max_solutions(self, value):
        self._max_solutions = value

    @property
    def relax(self):
        return self._relax

    @relax.setter
    def relax(self, value):
        self._relax = value


@six.add_metaclass(inheritdocstring)
class Model(interface.Model):

    def _configure_model(self):
        self.problem.verbose = 1 if self.configuration.verbosity > 1 else 0
        self.problem.max_mip_gap_abs = self.configuration.tolerance
        self.problem.max_seconds = self.configuration.timeout
        if self.configuration.max_nodes is not None:
            self.problem.max_nodes = self.configuration.max_nodes
        if self.configuration.max_solutions is not None:
            self.problem.max_solutions = self.configuration.max_solutions
        if self.configuration.relax:
            self.problem.relax()
            self._initialize_model_from_problem(self.problem)

    def _initialize_problem(self):
        self.problem = mip.Model(solver_name=mip.CBC)

    def _initialize_model_from_problem(self, problem):
        if not isinstance(problem, mip.Model):
            raise TypeError('Problem must be an instance of mip.Model, not ' + repr(type(problem)))

        # Set problem
        self.problem = problem

        self.variables.clear()
        # Set variables
        for v in problem.vars:
            self.variables.append(Variable(name=v.name[2:],
                                           lb=to_bound(v.lb),
                                           ub=to_bound(v.ub),
                                           type=_MIP_VTYPE_TO_VTYPE[v.var_type],
                                           problem=self))

        self.constraints.clear()
        # Set constraints
        for c in problem.constrs:
            name, suffix = c.name[2:-6], c.name[-6:]
            if name not in self.constraints:
                expr = sum(coef * self.variables[var.name[2:]]
                           for var, coef in c.expr.expr.items())
                if suffix == '_lower':
                    expr *= -1
                self.constraints.append(Constraint(expr, name=name, problem=self))

            if suffix == '_lower':
                self.constraints[name].lb = to_bound(-1*c.rhs)
            else:
                self.constraints[name].ub = to_bound(c.rhs)

        # Set objective
        terms = sum(coef * self.variables[var.name[2:]]
                    for var, coef in problem.objective.expr.items())
        self._objective = Objective(terms + problem.objective.const,
                              direction=_MIP_DIR_TO_DIR[problem.sense],
                              problem=self)

    def _var_primal(self, name):
        return self.problem.var_by_name('v_' + name).x

    def _var_dual(self, name):
        return self.problem.var_by_name('v_' + name).rc

    def _update_var_lb(self, name, lb):
        self.problem.var_by_name('v_' + name).lb = to_float(lb, True)

    def _update_var_ub(self, name, ub):
        self.problem.var_by_name('v_' + name).ub = to_float(ub, False)

    def _update_var_type(self, name, var_type):
        self.problem.var_by_name('v_' + name).var_type = var_type

    def _add_variables(self, variables):
        super(Model, self)._add_variables(variables)
        for var in variables:
            # TODO: may need to handle obj, column options
            self.problem.add_var(name='v_' + var.name,
                                 var_type=_VTYPE_TO_MIP_VTYPE[var.type],
                                 lb=to_float(var.lb, True),
                                 ub=to_float(var.ub, False))

    def _remove_variables(self, variables):
        super(Model, self)._remove_variables(variables)
        for var in variables:
            self.problem.remove(self.problem.var_by_name('v_' + var.name))

    def _constr_primal(self, con, is_lb):
        slack = self.problem.constr_by_name('c_' + con.constraint_name(is_lb)).slack
        return con.lb + slack if is_lb else con.ub - slack

    def _constr_dual(self, con, is_lb):
        return self.problem.constr_by_name('c_' + con.constraint_name(is_lb)).pi

    def _update_constraint_bound(self, con, is_lb):
        name = con.constraint_name(is_lb)
        constr = self.problem.constr_by_name('c_' + name)
        constr.rhs = to_float(-1*con.lb if is_lb else con.ub)

    def _expr_to_mip_expr(self, expr):
        """Parses mip linear expression from expression."""
        offset, coeffs, _ = parse_optimization_expression(expr)
        return offset + mip.xsum(to_float(coef) * self.problem.var_by_name('v_' + var.name)
                                 for var, coef in coeffs.items())

    def _remove_mip_constraint(self, con, is_lb):
        name = con.constraint_name(is_lb)
        constr = self.problem.constr_by_name('c_' + name)
        if constr is not None:
            self.problem.remove(constr)

    def _add_mip_constraint(self, con, is_lb=True, constr=None):
        # Optimization for precomputing the parsed constr expression
        if constr is None:
            constr = self._expr_to_mip_expr(con)

        # TODO: check if mip supports indicator_variable
        name = 'c_' + con.constraint_name(is_lb)
        if is_lb and con.lb is not None:
           self.problem.add_constr(-constr <= -con.lb, name)
        elif not is_lb and con.ub is not None:
            self.problem.add_constr(constr <= con.ub, name)

    def _add_constraints(self, constraints, sloppy=False):
        super(Model, self)._add_constraints(constraints, sloppy=sloppy)
        for con in constraints:
            constr = self._expr_to_mip_expr(con)
            # Attempt to add lb and ub constraints
            self._add_mip_constraint(con, True, constr)
            self._add_mip_constraint(con, False, constr)

    def _remove_constraints(self, constraints):
        super(Model, self)._remove_constraints(constraints)
        for con in constraints:
            # Remove lb and ub constraints
            self._remove_mip_constraint(con, True)
            self._remove_mip_constraint(con, False)

    def _optimize(self):
        self._configure_model()
        # TODO: could set self.problem.threads = -1 to use all available cores
        # TODO: could pass in max_nodes, max_solutions, relax by setting them
        #       in configuration

        # TODO: presolve. can improve numerical stability.

        status = self.problem.optimize()

        # TODO: make more robust. See glpk_interface.py
        #       (only do relax version if user specifies a flag)
        #       handle INT_INFEASIBLE case. mip.Model has a relax method that
        #       changes all integer and binary variable types to continuous.
        #       if we call this, make sure to update the associated Variable objects.
        return _MIP_STATUS_TO_STATUS[status]

    @interface.Model.objective.setter
    def objective(self, value):
        super(Model, Model).objective.fset(self, value)
        self.update()

        self.problem.objective = self._expr_to_mip_expr(value)
        self.problem.sense = _DIR_TO_MIP_DIR[value.direction]
        value.problem = self

    def __copy__(self):
        cls = self.__class__
        result = cls.__new__(cls)
        result.__dict__.update(self.__dict__)
        return result

    def to_lp(self):
        self.update()

        lp_form = ('Maximize' if self.problem.sense == mip.MAXIMIZE else 'Minimize') + '\n'
        for i, (var, coef) in enumerate(self.problem.objective.expr.items()):
            if i == 0:
                lp_form += f'OBJROW: {coef} {var.name}'
            else:
                sign = '+' if coef > 0 else '-'
                lp_form += f' {sign} {abs(coef)} {var.name}'

        lp_form += '\nSubject To\n'
        for con in self.problem.constrs:
            lp_form += f'{con}\n'

        lp_form += 'Bounds\n'
        for v in self.problem.vars:
            lp_form += f'{v.lb} <= {v} <= {v.ub}\n'
        lp_form += 'End\n'

        return lp_form

    @classmethod
    def from_lp(cls, lp_form):
        problem = mip.Model(solver_name=mip.CBC)
        with TemporaryFilename(suffix=".lp", content=lp_form) as tmp_file_name:
            problem.read(tmp_file_name)
        model = cls(problem=problem)
        return model
