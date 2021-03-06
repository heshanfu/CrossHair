
# *** Not prioritized for v0 ***
# TODO: increase test coverage: TypeVar('T', int, str) vs bounded type vars
# TODO: enforcement wrapper with preconditions that error: problematic for implies()
# TODO: do not claim "unable to meet preconditions" when we have path timeouts
# TODO: consider raises conditions (guaranteed to raise, guaranteed to not raise?)
# TODO: precondition strengthening ban (Subclass constraint rule)
# TODO: double-check counterexamples
# TODO: contracts for builtins
# TODO: standard library contracts
# TODO: identity-aware repr'ing for result messages
# TODO: mutating symbolic Callables?
# TODO: contracts on the contracts of function and object inputs/outputs?
# TODO: conditions on Callable arguments/return values

from dataclasses import dataclass, replace
from typing import *
import ast
import builtins
import collections
import copy
import enum
import inspect
import io
import itertools
import functools
import linecache
import operator
import os.path
import sys
import time
import traceback
import types
import typing

import typing_inspect  # type: ignore
import z3  # type: ignore

from crosshair import contracted_builtins
from crosshair import dynamic_typing
from crosshair.abcstring import AbcString
from crosshair.condition_parser import get_fn_conditions, get_class_conditions, ConditionExpr, Conditions, fn_globals
from crosshair.enforce import EnforcedConditions, PostconditionFailed
from crosshair.objectproxy import ObjectProxy
from crosshair.simplestructs import SimpleDict, SequenceConcatenation, SliceView, ShellMutableSequence
from crosshair.statespace import ReplayStateSpace, TrackingStateSpace, StateSpace, HeapRef, SnapshotRef, SearchTreeNode, model_value_to_python, VerificationStatus, IgnoreAttempt, SinglePathNode, CallAnalysis, MessageType, AnalysisMessage
from crosshair.util import CrosshairInternal, UnexploredPath, IdentityWrapper, AttributeHolder, CrosshairUnsupported, is_iterable
from crosshair.util import debug, set_debug, extract_module_from_file, walk_qualname
from crosshair.type_repo import PYTYPE_SORT, get_subclass_map


def samefile(f1: Optional[str], f2: Optional[str]) -> bool:
    try:
        return f1 is not None and f2 is not None and os.path.samefile(f1, f2)
    except FileNotFoundError:
        return False


def exception_line_in_file(frames: traceback.StackSummary, filename: str) -> Optional[int]:
    for frame in reversed(frames):
        if samefile(frame.filename, filename):
            return frame.lineno
    return None


def frame_summary_for_fn(frames: traceback.StackSummary, fn: Callable) -> Tuple[str, int]:
    fn_name = fn.__name__
    fn_file = cast(str, inspect.getsourcefile(fn))
    for frame in reversed(frames):
        if (frame.name == fn_name and
            samefile(frame.filename, fn_file)):
            return (frame.filename, frame.lineno)
    try:
        (_, fn_start_line) = inspect.getsourcelines(fn)
        return fn_file, fn_start_line
    except OSError:
        debug(f'Unable to get source information for function {fn_name} in file "{fn_file}"')
        return (fn_file, 0)


_MISSING = object()

# TODO Unify common logic here with EnforcedConditions?
class PatchedBuiltins:
    def __init__(self, patches: Mapping[str, object], enabled: Callable[[], bool]):
        self._patches = patches
        self._enabled = enabled

    def patch(self, key: str, patched_fn: Callable):
        orig_fn = builtins.__dict__[key]
        self._originals[key] = orig_fn
        enabled = self._enabled

        def call_if_enabled(*a, **kw):
            if enabled():
                return patched_fn(*a, **kw) 
            else:
                return orig_fn(*a, **kw)
        functools.update_wrapper(call_if_enabled, orig_fn)
        builtins.__dict__[key] = call_if_enabled

    def __enter__(self):
        patches = self._patches
        added_keys = []
        self._added_keys = added_keys
        originals = {}
        self._originals = originals
        for key, val in patches.items():
            if key.startswith('_') or not isinstance(val, Callable):
                continue
            if hasattr(builtins, key):
                self.patch(key, val)
            else:
                added_keys.append(key)
                builtins.__dict__[key] = val
        self._added_keys = added_keys
        self._originals = originals

    def __exit__(self, exc_type, exc_value, tb):
        bdict = builtins.__dict__
        bdict.update(self._originals)
        for key in self._added_keys:
            del bdict[key]


class ExceptionFilter:
    analysis: 'CallAnalysis'
    ignore: bool = False
    ignore_with_confirmation: bool = False
    user_exc: Optional[Tuple[Exception, traceback.StackSummary]] = None
    expected_exceptions: Tuple[Type[BaseException], ...]

    def __init__(self, expected_exceptions: FrozenSet[Type[BaseException]] = frozenset()):
        self.expected_exceptions = (NotImplementedError,) + tuple(expected_exceptions)

    def has_user_exception(self) -> bool:
        return self.user_exc is not None

    def __enter__(self) -> 'ExceptionFilter':
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if isinstance(exc_value, (PostconditionFailed, IgnoreAttempt)):
            if isinstance(exc_value, PostconditionFailed):
                # Postcondition : although this indicates a problem, it's with a
                # subroutine; not this function.
                # Usualy we want to ignore this because it will be surfaced more locally
                # in the subroutine.
                debug(
                    F'Ignoring based on internal failed post condition: {exc_value}')
            self.ignore = True
            self.analysis = CallAnalysis()
            return True
        if isinstance(exc_value, self.expected_exceptions):
            self.ignore = True
            self.analysis = CallAnalysis(VerificationStatus.CONFIRMED)
            return True
        if isinstance(exc_value, TypeError):
            exc_str = str(exc_value)
            if 'SmtStr' in exc_str or 'SmtInt' in exc_str or 'SmtFloat' in exc_str:
                # Ideally we'd attempt literal strings after encountering this.
                # See https://github.com/pschanely/CrossHair/issues/8
                raise CrosshairUnsupported('Detected proxy intolerance: '+exc_str)
        if isinstance(exc_value, (UnexploredPath, CrosshairInternal, z3.Z3Exception)):
            return False  # internal issue: re-raise
        if isinstance(exc_value, BaseException):  # TODO: should this be "Exception" instead?
            # Most other issues are assumed to be user-level exceptions:
            self.user_exc = (
                exc_value, traceback.extract_tb(sys.exc_info()[2]))
            self.analysis = CallAnalysis(VerificationStatus.REFUTED)
            return True  # suppress user-level exception
        return False  # re-raise resource and system issues


def smt_min(x, y):
    if x is y:
        return x
    return z3.If(x <= y, x, y)

def smt_sort_has_heapref(sort: z3.SortRef) -> bool:
    return 'HeapRef' in str(sort)  # TODO: don't do this :)

_HEAPABLE_PYTYPES = set([int, float, str, bool, type(None), complex])

def pytype_uses_heap(typ: Type) -> bool:
    return not (typ in _HEAPABLE_PYTYPES)

def normalize_pytype(typ: Type) -> Type:
    if typing_inspect.is_typevar(typ):
        # we treat type vars in the most general way possible (the bound, or as 'object')
        bound = typing_inspect.get_bound(typ)
        if bound is not None:
            return normalize_pytype(bound)
        constraints = typing_inspect.get_constraints(typ)
        if constraints:
            raise CrosshairUnsupported
            # TODO: not easy; interpreting as a Union allows the type to be
            # instantiated differently in different places. So, this doesn't work:
            # return Union.__getitem__(tuple(map(normalize_pytype, constraints)))
        return object
    if typ is Any:
        # The distinction between any and object is for type checking, crosshair treats them the same
        return object
    if typ is Type:
        return type
    return typ

def typeable_value(val: object) -> object:
    '''
    Foces values of unknown type (SmtObject) into a typed (but possibly still symbolic) value.
    '''
    while type(val) is SmtObject:
        val = cast(SmtObject, val)._wrapped()
    return val

def python_type(o: object) -> Type:
    if isinstance(o, SmtBackedValue):
        return o.python_type
    elif isinstance(o, SmtProxyMarker):
        bases = type(o).__bases__
        assert len(bases) == 2 and bases[0] is SmtBackedValue
        return bases[1]
    else:
        return type(o)

def origin_of(typ: Type) -> Type:
    typ = _WRAPPER_TYPE_TO_PYTYPE.get(typ, typ)
    if hasattr(typ, '__origin__'):
        return typ.__origin__
    return typ

def type_arg_of(typ: Type, index: int) -> Type:
    args = type_args_of(typ)
    return args[index] if index < len(args) else object

def type_args_of(typ: Type) -> Tuple[Type, ...]:
    if getattr(typ, '__args__', None):
        return typing_inspect.get_args(typ, evaluate=True)
    else:
        return ()


def name_of_type(typ: Type) -> str:
    return typ.__name__ if hasattr(typ, '__name__') else str(typ).split('.')[-1]


_SMT_FLOAT_SORT = z3.RealSort()  # difficulty getting the solver to use z3.Float64()

_TYPE_TO_SMT_SORT = {
    bool: z3.BoolSort(),
    str: z3.StringSort(),
    int: z3.IntSort(),
    float: _SMT_FLOAT_SORT,
}


def possibly_missing_sort(sort):
    datatype = z3.Datatype('optional_' + str(sort) + '_')
    datatype.declare('missing')
    datatype.declare('present', ('valueat', sort))
    ret = datatype.create()
    return ret


def type_to_smt_sort(t: Type) -> z3.SortRef:
    t = normalize_pytype(t)
    if t in _TYPE_TO_SMT_SORT:
        return _TYPE_TO_SMT_SORT[t]
    origin = origin_of(t)
    if origin is type:
        return PYTYPE_SORT
    return HeapRef

SmtGenerator = Callable[[StateSpace, type, Union[str, z3.ExprRef]], object]

_PYTYPE_TO_WRAPPER_TYPE: Dict[type, SmtGenerator] = {}  # to be populated later
_WRAPPER_TYPE_TO_PYTYPE: Dict[SmtGenerator, type] = {}


def crosshair_type_that_inhabits_python_type(typ: Type) -> Optional[SmtGenerator]:
    typ = normalize_pytype(typ)
    origin = origin_of(typ)
    if origin is Union:
        return SmtUnion(frozenset(typ.__args__))
    return _PYTYPE_TO_WRAPPER_TYPE.get(origin)


def crosshair_type_for_python_type(typ: Type) -> Optional[SmtGenerator]:
    typ = normalize_pytype(typ)
    origin = origin_of(typ)
    return _PYTYPE_TO_WRAPPER_TYPE.get(origin)


def smt_bool_to_int(a: z3.ExprRef) -> z3.ExprRef:
    return z3.If(a, 1, 0)


def smt_int_to_float(a: z3.ExprRef) -> z3.ExprRef:
    if _SMT_FLOAT_SORT == z3.Float64():
        return z3.fpRealToFP(z3.RNE(), z3.ToReal(a), _SMT_FLOAT_SORT)
    elif _SMT_FLOAT_SORT == z3.RealSort():
        return z3.ToReal(a)
    else:
        raise CrosshairInternal()


def smt_bool_to_float(a: z3.ExprRef) -> z3.ExprRef:
    if _SMT_FLOAT_SORT == z3.Float64():
        return z3.If(a, z3.FPVal(1.0, _SMT_FLOAT_SORT), z3.FPVal(0.0, _SMT_FLOAT_SORT))
    elif _SMT_FLOAT_SORT == z3.RealSort():
        return z3.If(a, z3.RealVal(1), z3.RealVal(0))
    else:
        raise CrosshairInternal()


_NUMERIC_PROMOTION_FNS = {
    (bool, bool): lambda x, y: (smt_bool_to_int(x), smt_bool_to_int(y), int),
    (bool, int): lambda x, y: (smt_bool_to_int(x), y, int),
    (int, bool): lambda x, y: (x, smt_bool_to_int(y), int),
    (bool, float): lambda x, y: (smt_bool_to_float(x), y, float),
    (float, bool): lambda x, y: (x, smt_bool_to_float(y), float),
    (int, int): lambda x, y: (x, y, int),
    (int, float): lambda x, y: (smt_int_to_float(x), y, float),
    (float, int): lambda x, y: (x, smt_int_to_float(y), float),
    (float, float): lambda x, y: (x, y, float),
}

_LITERAL_PROMOTION_FNS = {
    bool: z3.BoolVal,
    int: z3.IntVal,
    float: z3.RealVal if _SMT_FLOAT_SORT == z3.RealSort() else (lambda v: z3.FPVal(v, _SMT_FLOAT_SORT)),
    str: z3.StringVal,
}

def smt_coerce(val: Any) -> z3.ExprRef:
    if isinstance(val, SmtBackedValue):
        return val.var
    return val

def coerce_to_smt_sort(space: StateSpace, input_value: Any, desired_sort: z3.SortRef) -> Optional[z3.ExprRef]:
    natural_value = None
    input_value = typeable_value(input_value)
    promotion_fn = _LITERAL_PROMOTION_FNS.get(type(input_value))
    if isinstance(input_value, SmtBackedValue):
        natural_value = input_value.var
        if type(natural_value) is tuple:
            # Many container types aren't described by a single z3 value:
            return None
    elif promotion_fn:
        natural_value = promotion_fn(input_value)
    natural_sort = natural_value.sort() if natural_value is not None else None
    if natural_sort == desired_sort:
        return natural_value
    if desired_sort == HeapRef:
        return space.find_val_in_heap(input_value)
    if desired_sort == PYTYPE_SORT and isinstance(input_value, type):
        return space.type_repo.get_type(input_value)
    return None


def coerce_to_smt_var(space: StateSpace, v: Any) -> Tuple[z3.ExprRef, Type]:
    v = typeable_value(v)
    if isinstance(v, SmtBackedValue):
        return (v.var, v.python_type)
    promotion_fn = _LITERAL_PROMOTION_FNS.get(type(v))
    if promotion_fn:
        return (promotion_fn(v), type(v))
    return (space.find_val_in_heap(v), type(v))


def smt_to_ch_value(space: StateSpace, snapshot: SnapshotRef, smt_val: z3.ExprRef, pytype: type) -> object:
    def proxy_generator(typ: Type) -> object:
        return proxy_for_type(typ, space, 'heapval' + str(typ) + space.uniq())
    if smt_val.sort() == HeapRef:
        return space.find_key_in_heap(smt_val, pytype, proxy_generator, snapshot)
    ch_type = crosshair_type_that_inhabits_python_type(pytype)
    assert ch_type is not None
    return ch_type(space, pytype, smt_val)


def coerce_to_ch_value(v: Any, statespace: StateSpace) -> object:
    if isinstance(v, CrossHairValue):
        return v
    (smt_var, py_type) = coerce_to_smt_var(statespace, v)
    Typ = crosshair_type_that_inhabits_python_type(py_type)
    if Typ is None:
        raise TypeError
    return Typ(statespace, py_type, smt_var)

def realize(value: object):
    if not isinstance(value, SmtBackedValue):
        return value
    if type(value) is SmtType:
        return cast(SmtType, value)._realized()
    elif type(value) is SmtCallable:
        return value # we don't realize callables right now
    return origin_of(value.python_type)(value)

class CrossHairValue:
    pass

class SmtBackedValue(CrossHairValue):
    def __init__(self, statespace: StateSpace, typ: Type, smtvar: object):
        self.statespace = statespace
        self.snapshot = SnapshotRef(-1)
        self.python_type = typ
        if isinstance(smtvar, str):
            self.var = self.__init_var__(typ, smtvar)
        else:
            self.var = smtvar
            # TODO test that smtvar's sort matches expected?

    def __init_var__(self, typ, varname):
        z3type = type_to_smt_sort(typ)
        return z3.Const(varname, z3type)

    def __deepcopy__(self, memo):
        shallow = copy.copy(self)
        shallow.snapshot = self.statespace.current_snapshot()
        return shallow

    def __bool__(self):
        return NotImplemented

    def __eq__(self, other):
        coerced = coerce_to_smt_sort(self.statespace, other, self.var.sort())
        if coerced is None:
            return False
        return SmtBool(self.statespace, bool, self.var == coerced)

    def _coerce_into_compatible(self, other):
        raise TypeError(f'Unexpected type "{type(other)}"')

    def __ne__(self, other):
        return not self.__eq__(other)

    def __req__(self, other):
        return self.__eq__(other)

    def __rne__(self, other):
        return coerce_to_ch_value(other, self.statespace).__ne__(self)

    def __lt__(self, other):
        raise TypeError

    def __gt__(self, other):
        raise TypeError

    def __le__(self, other):
        raise TypeError

    def __ge__(self, other):
        raise TypeError

    def __add__(self, other):
        raise TypeError

    def __sub__(self, other):
        raise TypeError

    def __mul__(self, other):
        raise TypeError

    def __pow__(self, other):
        raise TypeError

    def __truediv__(self, other):
        raise TypeError

    def __floordiv__(self, other):
        raise TypeError

    def __mod__(self, other):
        raise TypeError

    def __and__(self, other):
        raise TypeError

    def __or__(self, other):
        raise TypeError

    def __xor__(self, other):
        raise TypeError

    def _binary_op(self, other, smt_op, py_op=None, expected_sort=None):
        #debug(f'binary op ({smt_op}) on value of type {type(other)}')
        left = self.var
        if expected_sort is None:
            right = coerce_to_smt_var(self.statespace, other)[0]
        else:
            right = coerce_to_smt_sort(self.statespace, other, expected_sort)
            if right is None:
                return py_op(self._coerce_into_compatible(self), self._coerce_into_compatible(other))
        try:
            ret = smt_op(left, right)
        except z3.z3types.Z3Exception as e:
            debug('Raising z3 error as Python TypeError: ', str(e))
            raise TypeError
        return self.__class__(self.statespace, self.python_type, ret)

    def _unary_op(self, op):
        return self.__class__(self.statespace, self.python_type, op(self.var))


class SmtNumberAble(SmtBackedValue):
    def _numeric_binary_smt_op(self, other, op) -> Optional[Tuple[z3.ExprRef, type]]:
        l_var, lpytype = self.var, self.python_type
        r_var, rpytype = coerce_to_smt_var(self.statespace, other)
        promotion_fn = _NUMERIC_PROMOTION_FNS.get((lpytype, rpytype))
        if promotion_fn is None:
            return None
        l_var, r_var, common_pytype = promotion_fn(l_var, r_var)
        return (op(l_var, r_var), common_pytype)

    def _numeric_binary_op(self, other, op, op_result_pytype=None):
        if type(other) == complex:
            return op(complex(self), complex(other))
        result = self._numeric_binary_smt_op(other, op)
        if result is None:
            raise TypeError
        smt_result, common_pytype = result
        if op_result_pytype is not None:
            common_pytype = op_result_pytype
        cls = _PYTYPE_TO_WRAPPER_TYPE[common_pytype]
        return cls(self.statespace, common_pytype, smt_result)

    def _numeric_unary_op(self, op):
        var, pytype = self.var, self.python_type
        if pytype is bool:
            var = smt_bool_to_int(var)
            pytype = int
        cls = _PYTYPE_TO_WRAPPER_TYPE[pytype]
        return cls(self.statespace, pytype, op(var))

    def __pos__(self):
        return self._unary_op(operator.pos)

    def __neg__(self):
        return self._unary_op(operator.neg)

    def __abs__(self):
        return self._unary_op(lambda v: z3.If(v < 0, -v, v))

    def __lt__(self, other):
        return self._numeric_binary_op(other, operator.lt, op_result_pytype=bool)

    def __gt__(self, other):
        return self._numeric_binary_op(other, operator.gt, op_result_pytype=bool)

    def __le__(self, other):
        return self._numeric_binary_op(other, operator.le, op_result_pytype=bool)

    def __ge__(self, other):
        return self._numeric_binary_op(other, operator.ge, op_result_pytype=bool)

    def __eq__(self, other):
        # Note this is a little different than the other comparison operations, because
        # equality doesn't raise TypeErrors on mismatched types
        result = self._numeric_binary_smt_op(other, operator.eq)
        if result is None:
            return False
        return SmtBool(self.statespace, bool, result[0])

    def __add__(self, other):
        return self._numeric_binary_op(other, operator.add)

    def __sub__(self, other):
        return self._numeric_binary_op(other, operator.sub)

    def __mul__(self, other):
        if isinstance(other, (str, SmtStr)):
            return other.__mul__(self)
        return self._numeric_binary_op(other, operator.mul)

    def __pow__(self, other):
        if other < 0 and self == 0:
            raise ZeroDivisionError
        return self._numeric_binary_op(other, operator.pow)

    def __rmul__(self, other):
        return coerce_to_ch_value(other, self.statespace).__mul__(self)

    def __radd__(self, other):
        return coerce_to_ch_value(other, self.statespace).__add__(self)

    def __rsub__(self, other):
        return coerce_to_ch_value(other, self.statespace).__sub__(self)

    def __rtruediv__(self, other):
        return coerce_to_ch_value(other, self.statespace).__truediv__(self)

    def __rfloordiv__(self, other):
        return coerce_to_ch_value(other, self.statespace).__floordiv__(self)

    def __rmod__(self, other):
        return coerce_to_ch_value(other, self.statespace).__mod__(self)

    def __rdivmod__(self, other):
        return coerce_to_ch_value(other, self.statespace).__divmod__(self)

    def __rpow__(self, other):
        return coerce_to_ch_value(other, self.statespace).__pow__(self)

    def __rlshift__(self, other):
        return coerce_to_ch_value(other, self.statespace).__lshift__(self)

    def __rrshift__(self, other):
        return coerce_to_ch_value(other, self.statespace).__rshift__(self)

    def __rand__(self, other):
        return coerce_to_ch_value(other, self.statespace).__and__(self)

    def __rxor__(self, other):
        return coerce_to_ch_value(other, self.statespace).__xor__(self)

    def __ror__(self, other):
        return coerce_to_ch_value(other, self.statespace).__or__(self)

class SmtIntable(SmtNumberAble):
    # bitwise operators
    def __invert__(self):
        return -(self + 1)

    def __lshift__(self, other):
        if other < 0:
            raise ValueError('negative shift count')
        return self * (2 ** other)

    def __rshift__(self, other):
        if other < 0:
            raise ValueError('negative shift count')
        return self // (2 ** other)

    def __and__(self, other):
        return self._apply_bitwise(operator.and_, self, other)

    def __or__(self, other):
        return self._apply_bitwise(operator.or_, self, other)

    def __xor__(self, other):
        return self._apply_bitwise(operator.xor, self, other)

    def __divmod__(self, other):
        return (self // other, self % other)

class SmtBool(SmtIntable):
    def __init__(self, statespace: StateSpace, typ: Type, smtvar: object):
        assert typ == bool
        SmtBackedValue.__init__(self, statespace, typ, smtvar)

    def __repr__(self):
        return self.__bool__().__repr__()

    def __hash__(self):
        return self.__bool__().__hash__()

    def __index__(self):
        return SmtInt(self.statespace, int, smt_bool_to_int(self.var))

    def __xor__(self, other):
        return self._binary_op(other, z3.Xor)

    def __bool__(self):
        return self.statespace.choose_possible(self.var)

    def __int__(self):
        return SmtInt(self.statespace, int, smt_bool_to_int(self.var))

    def __float__(self):
        return SmtFloat(self.statespace, float, smt_bool_to_float(self.var))

    def __complex__(self):
        return complex(self.__float__())

    def __add__(self, other):
        return self._numeric_binary_op(other, operator.add)

    def __sub__(self, other):
        return self._numeric_binary_op(other, operator.sub)


class SmtInt(SmtIntable):
    def __init__(self, statespace: StateSpace, typ: Type, smtvar: Union[str, z3.ArithRef]):
        assert typ == int
        assert type(smtvar) != int
        SmtIntable.__init__(self, statespace, typ, smtvar)

    def _apply_bitwise(self, op: Callable, v1: int, v2: int) -> int:
        return op(v1.__index__(), v2.__index__())

    def __repr__(self):
        return self.__index__().__repr__()

    def __hash__(self):
        return self.__index__().__hash__()

    def __float__(self):
        return SmtFloat(self.statespace, float, smt_int_to_float(self.var))

    def __complex__(self):
        return complex(self.__float__())

    def __index__(self):
        #debug('WARNING: attempting to materialize symbolic integer. Trace:')
        # traceback.print_stack()
        if self == 0:
            return 0
        ret = self.statespace.find_model_value(self.var)
        assert type(ret) is int
        return ret

    def __bool__(self):
        return SmtBool(self.statespace, bool, self.var != 0).__bool__()

    def __int__(self):
        return self.__index__()

    def __truediv__(self, other):
        return self.__float__() / other

    def __floordiv__(self, other):
        if not isinstance(other, (bool, int, SmtInt, SmtBool)):
            return realize(self) // realize(other)
        return self._numeric_binary_op(other, lambda x, y: z3.If(
            x % y == 0 or x >= 0, x / y, z3.If(y >= 0, x / y + 1, x / y - 1)))

    def __mod__(self, other):
        if not isinstance(other, (bool, int, SmtInt, SmtBool)):
            return realize(self) % realize(other)
        if other == 0:
            raise ZeroDivisionError()
        return self._numeric_binary_op(other, operator.mod)
        #return self._binary_op(other, operator.mod)


_Z3_ONE_HALF = z3.RealVal("1/2")


class SmtFloat(SmtNumberAble):
    def __init__(self, statespace: StateSpace, typ: Type, smtvar: object):
        assert typ == float
        SmtBackedValue.__init__(self, statespace, typ, smtvar)

    def __repr__(self):
        return self.statespace.find_model_value(self.var).__repr__()

    def __hash__(self):
        return self.statespace.find_model_value(self.var).__hash__()

    def __bool__(self):
        return SmtBool(self.statespace, bool, self.var != 0).__bool__()
    
    def __float__(self):
        return self.statespace.find_model_value(self.var).__float__()

    def __complex__(self):
        return complex(self.__float__())

    def __round__(self, ndigits=None):
        if ndigits is not None:
            factor = 10 ** ndigits
            return round(self * factor) / factor
        else:
            var, floor, nearest = self.var, z3.ToInt(
                self.var), z3.ToInt(self.var + _Z3_ONE_HALF)
            return SmtInt(self.statespace, int, z3.If(var != floor + _Z3_ONE_HALF, nearest, z3.If(floor % 2 == 0, floor, floor + 1)))

    def __floor__(self):
        return SmtInt(self.statespace, int, z3.ToInt(self.var))

    def __ceil__(self):
        var, floor = self.var, z3.ToInt(self.var)
        return SmtInt(self.statespace, int, z3.If(var == floor, floor, floor + 1))

    def __mod__(self, other):
        return realize(self) % realize(other) # TODO: z3 does not support modulo on reals

    def __trunc__(self):
        var, floor = self.var, z3.ToInt(self.var)
        return SmtInt(self.statespace, int, z3.If(var >= 0, floor, floor + 1))

    def __truediv__(self, other):
        if not other:
            raise ZeroDivisionError('division by zero')
        return self._numeric_binary_op(other, operator.truediv)


_CONVERSION_METHODS: Dict[Tuple[type, type], Any] = {
    (bool, int): int,
    (bool, float): float,
    (bool, complex): complex,
    (SmtBool, int): lambda i: SmtInt(i.statespace, int, smt_bool_to_int(i.var)),
    (SmtBool, float): lambda i: SmtFloat(i.statespace, float, smt_bool_to_float(i.var)),
    (SmtBool, complex): complex,
    
    (int, float): float,
    (int, complex): complex,
    (SmtInt, float): lambda i: SmtFloat(i.statespace, float, smt_int_to_float(i.var)),
    (SmtInt, complex): complex,
    
    (float, complex): complex,
    (SmtFloat, complex): complex,
}
    
def convert(val: object, target_type: type) -> object:
    '''
    Attempt to convert to the given type, as Python would perform
    implicit conversion. Handles both crosshair and native values.
    '''
    orig_type = type(val)
    converter = _CONVERSION_METHODS.get((orig_type, target_type))
    if converter:
        return converter(val)
    return val


class SmtDictOrSet(SmtBackedValue):
    def __init__(self, statespace: StateSpace, typ: Type, smtvar: object):
        self.key_pytype = normalize_pytype(type_arg_of(typ, 0))
        SmtBackedValue.__init__(self, statespace, typ, smtvar)
        self.key_ch_type = crosshair_type_for_python_type(self.key_pytype)
        self.statespace.add(self._len() >= 0)

    def _arr(self):
        return self.var[0]

    def _len(self):
        return self.var[1]

    def __len__(self):
        return SmtInt(self.statespace, int, self._len())

    def __bool__(self):
        return SmtBool(self.statespace, bool, self._len() != 0).__bool__()


class SmtDict(SmtDictOrSet, collections.abc.MutableMapping):
    def __init__(self, statespace: StateSpace, typ: Type, smtvar: object):
        self.val_pytype = normalize_pytype(type_arg_of(typ, 1))
        SmtDictOrSet.__init__(self, statespace, typ, smtvar)
        self.val_ch_type = crosshair_type_for_python_type(self.val_pytype)
        arr_var = self._arr()
        len_var = self._len()
        self.val_missing_checker = arr_var.sort().range().recognizer(0)
        self.val_missing_constructor = arr_var.sort().range().constructor(0)
        self.val_constructor = arr_var.sort().range().constructor(1)
        self.val_accessor = arr_var.sort().range().accessor(1, 0)
        self.empty = z3.K(arr_var.sort().domain(),
                          self.val_missing_constructor())
        self.statespace.add((arr_var == self.empty) == (len_var == 0))

    def __init_var__(self, typ, varname):
        assert typ == self.python_type
        smt_key_type = type_to_smt_sort(self.key_pytype)
        smt_val_type = type_to_smt_sort(self.val_pytype)
        arr_smt_type = z3.ArraySort(
            smt_key_type, possibly_missing_sort(smt_val_type))
        return (
            z3.Const(varname + '_map' + self.statespace.uniq(), arr_smt_type),
            z3.Const(varname + '_len' + self.statespace.uniq(), z3.IntSort())
        )

    def __eq__(self, other):
        (self_arr, self_len) = self.var
        has_heapref = smt_sort_has_heapref(
            self.var[1].sort()) or smt_sort_has_heapref(self.var[0].sort())
        if not has_heapref:
            if isinstance(other, SmtDict):
                (other_arr, other_len) = other.var
                return SmtBool(self.statespace, bool, z3.And(self_len == other_len, self_arr == other_arr))
        # Manually check equality. Drive the loop from the (likely) concrete value 'other':
        if len(self) != len(other):
            return False
        for k, v in other.items():
            if k not in self or self[k] != v:
                return False
        return True

    def __repr__(self):
        return str(dict(self.items()))

    def __setitem__(self, k, v):
        missing = self.val_missing_constructor()
        (k, _), (v, _) = coerce_to_smt_var(
            self.statespace, k), coerce_to_smt_var(self.statespace, v)
        old_arr, old_len = self.var
        new_len = z3.If(z3.Select(old_arr, k) == missing, old_len + 1, old_len)
        self.var = (z3.Store(old_arr, k, self.val_constructor(v)), new_len)

    def __delitem__(self, k):
        missing = self.val_missing_constructor()
        (k, _) = coerce_to_smt_var(self.statespace, k)
        old_arr, old_len = self.var
        if SmtBool(self.statespace, bool, z3.Select(old_arr, k) == missing).__bool__():
            raise KeyError(k)
        if SmtBool(self.statespace, bool, self._len() == 0).__bool__():
            raise IgnoreAttempt('SmtDict in inconsistent state')
        self.var = (z3.Store(old_arr, k, missing), old_len - 1)

    def __getitem__(self, k):
        with self.statespace.framework():
            smt_key, _ = coerce_to_smt_var(self.statespace, k)
            debug('lookup', self._arr().sort(), smt_key)
            possibly_missing = self._arr()[smt_key]
            is_missing = self.val_missing_checker(possibly_missing)
            if SmtBool(self.statespace, bool, is_missing).__bool__():
                raise KeyError(k)
            if SmtBool(self.statespace, bool, self._len() == 0).__bool__():
                raise IgnoreAttempt('SmtDict in inconsistent state')
            return smt_to_ch_value(self.statespace,
                                   self.snapshot,
                                   self.val_accessor(possibly_missing),
                                   self.val_pytype)

    def __iter__(self):
        arr_var, len_var = self.var
        idx = 0
        arr_sort = self._arr().sort()
        missing = self.val_missing_constructor()
        while SmtBool(self.statespace, bool, idx < len_var).__bool__():
            if SmtBool(self.statespace, bool, arr_var == self.empty).__bool__():
                raise IgnoreAttempt('SmtDict in inconsistent state')
            k = z3.Const('k' + str(idx) + self.statespace.uniq(),
                         arr_sort.domain())
            v = z3.Const('v' + str(idx) + self.statespace.uniq(),
                         self.val_constructor.domain(0))
            remaining = z3.Const('remaining' + str(idx) +
                                 self.statespace.uniq(), arr_sort)
            idx += 1
            self.statespace.add(arr_var == z3.Store(
                remaining, k, self.val_constructor(v)))
            self.statespace.add(z3.Select(remaining, k) == missing)
            yield smt_to_ch_value(self.statespace,
                                  self.snapshot,
                                  k,
                                  self.key_pytype)
            arr_var = remaining
        # In this conditional, we reconcile the parallel symbolic variables for length
        # and contents:
        if SmtBool(self.statespace, bool, arr_var != self.empty).__bool__():
            raise IgnoreAttempt('SmtDict in inconsistent state')

    def copy(self):
        return SmtDict(self.statespace, self.python_type, self.var)

    # TODO: investigate this approach for type masquerading:
    # @property
    # def __class__(self):
    #    return dict


class SmtSet(SmtDictOrSet, collections.abc.Set):
    def __init__(self, statespace: StateSpace, typ: Type, smtvar: object):
        SmtDictOrSet.__init__(self, statespace, typ, smtvar)
        self.empty = z3.K(self._arr().sort().domain(), False)
        self.statespace.add((self._arr() == self.empty) == (self._len() == 0))

    def __eq__(self, other):
        (self_arr, self_len) = self.var
        if isinstance(other, SmtSet):
            (other_arr, other_len) = other.var
            if other_arr.sort() == self_arr.sort():
                return SmtBool(self.statespace, bool, z3.And(self_len == other_len, self_arr == other_arr))
        if not isinstance(other, (set, frozenset, SmtSet)):
            return False
        # Manually check equality. Drive size from the (likely) concrete value 'other':
        if len(self) != len(other):
            return False
        # Then iterate on self (iteration will create a lot of good symbolic constraints):
        for item in self:
            # We iterate over other instead of just checking "if item in other:" because we
            # don't want to hash our symbolic item, which would materialize it.
            found = False
            for oitem in other:
                if item == oitem:
                    found = True
                    break
            if not found:
                return False
        return True

    def __init_var__(self, typ, varname):
        assert typ == self.python_type
        return (
            z3.Const(varname + '_map' + self.statespace.uniq(),
                     z3.ArraySort(type_to_smt_sort(self.key_pytype),
                                  z3.BoolSort())),
            z3.Const(varname + '_len' + self.statespace.uniq(), z3.IntSort())
        )

    def __contains__(self, raw_key):
        converted_key = convert(raw_key, self.key_pytype) # handle implicit numeric conversions
        k = coerce_to_smt_sort(self.statespace, converted_key, self._arr().sort().domain())
        # TODO: test k for nullness (it's a key type error)
        if k is None:
            debug('unable to check containment',
                  raw_key, type(raw_key), self.key_pytype, type(converted_key), 'vs my sort:', self._arr().sort())
            raise TypeError
        present = self._arr()[k]
        return SmtBool(self.statespace, bool, present)

    def __iter__(self):
        arr_var, len_var = self.var
        idx = 0
        arr_sort = self._arr().sort()
        while SmtBool(self.statespace, bool, idx < len_var).__bool__():
            if SmtBool(self.statespace, bool, arr_var == self.empty).__bool__():
                raise IgnoreAttempt('SmtSet in inconsistent state')
            k = z3.Const('k' + str(idx) + self.statespace.uniq(),
                         arr_sort.domain())
            remaining = z3.Const('remaining' + str(idx) +
                                 self.statespace.uniq(), arr_sort)
            idx += 1
            self.statespace.add(arr_var == z3.Store(remaining, k, True))
            self.statespace.add(z3.Not(z3.Select(remaining, k)))
            yield smt_to_ch_value(self.statespace, self.snapshot, k, self.key_pytype)
            arr_var = remaining
        # In this conditional, we reconcile the parallel symbolic variables for length
        # and contents:
        if SmtBool(self.statespace, bool, arr_var != self.empty).__bool__():
            raise IgnoreAttempt('SmtSet in inconsistent state')

    # Hardwire some operations into abc methods
    # (SmtBackedValue defaults these operations into
    # TypeErrors, but must appear first in the mro)
    def __ge__(self, other):
        return collections.abc.Set.__ge__(self, other)

    def __gt__(self, other):
        return collections.abc.Set.__gt__(self, other)

    def __le__(self, other):
        return collections.abc.Set.__le__(self, other)

    def __lt__(self, other):
        return collections.abc.Set.__lt__(self, other)

    def __and__(self, other):
        return collections.abc.Set.__and__(self, other)

    def __or__(self, other):
        return collections.abc.Set.__or__(self, other)

    def __xor__(self, other):
        return collections.abc.Set.__xor__(self, other)


class SmtMutableSet(SmtSet):
    def __repr__(self):
        return str(set(self))

    @classmethod
    def _from_iterable(cls, it):
        # overrides collections.abc.Set's version
        return set(it)

    def add(self, k):
        (k, _) = coerce_to_smt_var(self.statespace, k)
        old_arr, old_len = self.var
        new_len = z3.If(z3.Select(old_arr, k), old_len, old_len + 1)
        self.var = (z3.Store(old_arr, k, True), new_len)

    def discard(self, k):
        (k, _) = coerce_to_smt_var(self.statespace, k)
        old_arr, old_len = self.var
        new_len = z3.If(z3.Select(old_arr, k), old_len - 1, old_len)
        self.var = (z3.Store(old_arr, k, False), new_len)


class SmtFrozenSet(SmtSet):
    def __repr__(self):
        return frozenset(self).__repr__()

    def __hash__(self):
        return frozenset(self).__hash__()

    @classmethod
    def _from_iterable(cls, it):
        # overrides collections.abc.Set's version
        return set(it)

    def add(self, k):
        (k, _) = coerce_to_smt_var(self.statespace, k)
        old_arr, old_len = self.var
        new_len = z3.If(z3.Select(old_arr, k), old_len, old_len + 1)
        self.var = (z3.Store(old_arr, k, True), new_len)

    def discard(self, k):
        (k, _) = coerce_to_smt_var(self.statespace, k)
        old_arr, old_len = self.var
        new_len = z3.If(z3.Select(old_arr, k), old_len - 1, old_len)
        self.var = (z3.Store(old_arr, k, False), new_len)


def process_slice_vs_symbolic_len(space: StateSpace, i: slice, smt_len: z3.ExprRef) -> Union[z3.ExprRef, Tuple[z3.ExprRef, z3.ExprRef]]:
    def normalize_symbolic_index(idx):
        if isinstance(idx, int):
            return idx if idx >= 0 else smt_len + idx
        else:
            # In theory, we could do this without the fork. But it's heavy for the solver, and
            # it breaks z3 sometimes? (unreachable @ crosshair.examples.showcase.duplicate_list)
            # return z3.If(idx >= 0, idx, smt_len + idx)
            if space.smt_fork(smt_coerce(idx >= 0)):
                return idx
            else:
                return smt_len + idx
    if isinstance(i, int) or isinstance(i, SmtInt):
        smt_i = smt_coerce(i)
        if space.smt_fork(z3.Or(smt_i >= smt_len, smt_i < -smt_len)):
            raise IndexError(f'index "{i}" is out of range')
        return normalize_symbolic_index(smt_i)
    elif isinstance(i, slice):
        smt_start, smt_stop, smt_step = (i.start, i.stop, i.step)
        if smt_step not in (None, 1):
            raise CrosshairUnsupported('slice steps not handled')
        start = normalize_symbolic_index(
            smt_start) if i.start is not None else 0
        stop = normalize_symbolic_index(
            smt_stop) if i.stop is not None else smt_len
        return (start, stop)
    else:
        raise TypeError(
            'indices must be integers or slices, not ' + str(type(i)))


class SmtSequence(SmtBackedValue):
    def _smt_getitem(self, i):
        idx_or_pair = process_slice_vs_symbolic_len(
            self.statespace, i, z3.Length(self.var))
        if isinstance(idx_or_pair, tuple):
            (start, stop) = idx_or_pair
            return (z3.Extract(self.var, start, stop - start), True)
        else:
            return (self.var[idx_or_pair], False)

    def __iter__(self):
        idx = 0
        while len(self) > idx:
            yield self[idx]
            idx += 1

    def __len__(self):
        return SmtInt(self.statespace, int, z3.Length(self.var))

    def __bool__(self):
        return SmtBool(self.statespace, bool, z3.Length(self.var) > 0).__bool__()


class SmtArrayBasedUniformTuple(SmtSequence):
    def __init__(self, statespace: StateSpace, typ: Type, smtvar: Union[str, Tuple]):
        if type(smtvar) == str:
            pass
        else:
            assert type(smtvar) is tuple, f'incorrect type {type(smtvar)}'
            assert len(smtvar) == 2
        self.val_pytype = normalize_pytype(type_arg_of(typ, 0))
        self.item_smt_sort = (HeapRef if pytype_uses_heap(self.val_pytype)
                              else type_to_smt_sort(self.val_pytype))
        self.key_pytype = int
        SmtBackedValue.__init__(self, statespace, typ, smtvar)
        arr_var = self._arr()
        len_var = self._len()
        self.statespace.add(len_var >= 0)
        
        self.val_ch_type = crosshair_type_for_python_type(self.val_pytype)
        

    def __init_var__(self, typ, varname):
        assert typ == self.python_type
        arr_smt_type = z3.ArraySort(z3.IntSort(), self.item_smt_sort)
        return (
            z3.Const(varname + '_map' + self.statespace.uniq(), arr_smt_type),
            z3.Const(varname + '_len' + self.statespace.uniq(), z3.IntSort())
        )

    def _arr(self):
        return self.var[0]

    def _len(self):
        return self.var[1]

    def __len__(self):
        return SmtInt(self.statespace, int, self._len())

    def __bool__(self):
        return SmtBool(self.statespace, bool, self._len() != 0).__bool__()
    
    def __eq__(self, other):
        (self_arr, self_len) = self.var
        if not is_iterable(other):
            return False
        if len(self) != len(other):
            return False
        for idx, v in enumerate(other):
            if self[idx] != v:
                return False
        return True

    def __repr__(self):
        return str(list(self))

    def __setitem__(self, k, v):
        raise CrosshairInternal()
        missing = self.val_missing_constructor()
        (k, _), (v, _) = coerce_to_smt_var(
            self.statespace, k), coerce_to_smt_var(self.statespace, v)
        old_arr, old_len = self.var
        new_len = z3.If(z3.Select(old_arr, k) == missing, old_len + 1, old_len)
        self.var = (z3.Store(old_arr, k, self.val_constructor(v)), ___, new_len)

    def __delitem__(self, k):
        raise CrosshairInternal()
        missing = self.val_missing_constructor()
        (k, _) = coerce_to_smt_var(self.statespace, k)
        old_arr, old_len = self.var
        if SmtBool(self.statespace, bool, z3.Select(old_arr, k) == missing).__bool__():
            raise KeyError(k)
        if SmtBool(self.statespace, bool, self._len() == 0).__bool__():
            raise IgnoreAttempt('SmtDict in inconsistent state')
        self.var = (z3.Store(old_arr, k, missing), ___, old_len - 1)

    def __iter__(self):
        arr_var, len_var = self.var
        idx = 0
        while SmtBool(self.statespace, bool, idx < len_var).__bool__():
            yield smt_to_ch_value(self.statespace,
                                  self.snapshot,
                                  z3.Select(arr_var, idx),
                                  self.val_pytype)
            idx += 1

    def __add__(self, other):
        return SequenceConcatenation(self, other)

    def __radd__(self, other):
        return SequenceConcatenation(other, self)

    def __contains__(self, other):
        space = self.statespace
        with space.framework():
            if smt_sort_has_heapref(self.item_smt_sort):
                # Fall back to standard equality and iteration
                for self_item in self:
                    if self_item == other:
                        return True
                return False
            else:
                idx = z3.Const('possible_idx' + space.uniq(), z3.IntSort())
                smt_other = coerce_to_smt_sort(space, other, self.item_smt_sort)
                if smt_other is None: # couldn't coerce the type. TODO: right now this is none when `other` is a proxy of object.
                    return False
                # TODO: test smt_item nullness (incorrect type)
                idx_in_range = z3.Exists(idx, z3.And(0 <= idx,
                                                     idx < self._len(),
                                                     z3.Select(self._arr(), idx) == smt_other))
                return SmtBool(space, bool, idx_in_range)

    def __getitem__(self, i):
        space = self.statespace
        with space.framework():
            idx_or_pair = process_slice_vs_symbolic_len(space, i, self._len())
            if isinstance(idx_or_pair, tuple):
                (start, stop) = idx_or_pair
                (myarr, mylen) = self.var
                stop = SmtInt(space, int, smt_min(mylen, smt_coerce(stop)))
                return SliceView(self, start, stop)
            else:
                smt_result = z3.Select(self._arr(), idx_or_pair)
                return smt_to_ch_value(space, self.snapshot, smt_result, self.val_pytype)

    def insert(self, idx, obj):
        raise CrosshairUnsupported
        (self_arr, self_len) = self.var
        if coerce_to_smt_var(space, idx)[0] == self_len:
            self.var = SequenceConcatenation(self, [obj])
        else:
            idx = process_slice_vs_symbolic_len(space, idx, self_len)
            self.var = z3.Concat(z3.Extract(var, 0, idx),
                                 to_insert,
                                 self.__class__(var, idx, self.len - idx))


class SmtList(ShellMutableSequence, collections.abc.MutableSequence, CrossHairValue):
    def __init__(self, *a):
        ShellMutableSequence.__init__(self, SmtArrayBasedUniformTuple(*a))
    def __mod__(self, *a):
        raise TypeError

class SmtType(SmtBackedValue):
    _realization : Optional[Type] = None
    def __init__(self, statespace: StateSpace, typ: Type, smtvar: object):
        assert origin_of(typ) is type
        self.pytype_cap = origin_of(typ.__args__[0]) if hasattr(typ, '__args__') else object
        assert type(self.pytype_cap) is type
        smt_cap = statespace.type_repo.get_type(self.pytype_cap)
        SmtBackedValue.__init__(self, statespace, typ, smtvar)
        statespace.add(statespace.type_repo.smt_issubclass(self.var, smt_cap))
    def _is_superclass_of_(self, other):
        if type(other) is SmtType:
            # Prefer it this way because only _is_subcless_of_ does the type cap lowering.
            return other._is_subclass_of_(self)
        space = self.statespace
        with space.framework():
            coerced = coerce_to_smt_sort(space, other, self.var.sort())
            if coerced is None:
                return False
            return SmtBool(space, bool, space.type_repo.smt_issubclass(coerced, self.var))
    def _is_subclass_of_(self, other):
        space = self.statespace
        with space.framework():
            coerced = coerce_to_smt_sort(space, other, self.var.sort())
            if coerced is None:
                return False
            ret = SmtBool(space, bool, space.type_repo.smt_issubclass(self.var, coerced))
            other_pytype = other.pytype_cap if type(other) is SmtType else other
            # consider lowering the type cap
            if other_pytype is not self.pytype_cap and issubclass(other_pytype, self.pytype_cap) and ret:
                self.pytype_cap = other_pytype
            return ret
    def _realized(self):
        if self._realization is None:
            self._realization = self._realize()
        return self._realization
    def _realize(self) -> Type:
        cap = self.pytype_cap
        space = self.statespace
        if cap is object:
            pytype_to_smt = space.type_repo.pytype_to_smt
            for pytype, smt_type in pytype_to_smt.items():
                if not issubclass(pytype, cap):
                    continue
                if space.smt_fork(self.var != smt_type):
                    continue
                return pytype
            raise IgnoreAttempt
        else:
            subtype = choose_type(space, cap)
            smt_type = space.type_repo.get_type(subtype)
            if space.smt_fork(self.var != smt_type):
                raise IgnoreAttempt
            return subtype
    def __copy__(self):
        return self if self._realization is None else self._realization
    def __repr__(self):
        return repr(self._realized())
    def __hash__(self):
        return hash(self._realized())


class LazyObject(ObjectProxy):
    _inner: object = _MISSING

    def _realize(self):
        raise NotImplementedError

    def _wrapped(self):
        inner = object.__getattribute__(self, '_inner')
        if inner is _MISSING:
            inner = self._realize()
            object.__setattr__(self, '_inner', inner)
        return inner

    def __deepcopy__(self, memo):
        inner = object.__getattribute__(self, '_inner')
        if inner is _MISSING:
            # CrossHair will deepcopy for mutation checking.
            # That's usually bad for LazyObjects, which want to defer their
            # realization, so we simply don't do mutation checking for these
            # kinds of values right now.
            return self
        else:
            return copy.deepcopy(self.wrapped())


class SmtObject(LazyObject, CrossHairValue):
    '''
    An object with an unknown type.
    We lazily create a more specific smt-based value in hopes that an
    isinstance() check will be called before something is accessed on us.
    Note that this class is not an SmtBackedValue, but its _typ and _inner
    members can be.
    '''
    def __init__(self, space: StateSpace, typ: Type, varname: object):
        object.__setattr__(self, '_typ', SmtType(space, type, varname))
        object.__setattr__(self, '_space', space)
        object.__setattr__(self, '_varname', varname)

    def _realize(self):
        space = object.__getattribute__(self, '_space')
        varname = object.__getattribute__(self, '_varname')

        typ = object.__getattribute__(self, '_typ')
        pytype = realize(typ)
        debug('materializing symbolic object as an instance of', pytype)
        if pytype is object:
            return object()
        return proxy_for_type(pytype, space, varname, allow_subtypes=False)

    @property
    def python_type(self):
        return object.__getattribute__(self, '_typ')

    @property
    def __class__(self):
        return SmtObject

    @__class__.setter
    def __class__(self, value):
        raise CrosshairUnsupported


class SmtCallable(SmtBackedValue):
    __closure__ = None

    def __init___(self, statespace: StateSpace, typ: Type, smtvar: object):
        SmtBackedValue.__init__(self, statespace, typ, smtvar)

    def __eq__(self, other):
        return (self.var is other.var) if isinstance(other, SmtCallable) else False

    def __hash__(self):
        return id(self.var)

    def __init_var__(self, typ, varname):
        type_args = type_args_of(self.python_type)
        if not type_args:
            type_args = [..., Any]
        (self.arg_pytypes, self.ret_pytype) = type_args
        if self.arg_pytypes == ...:
            raise CrosshairUnsupported
        self.arg_ch_type = map(
            crosshair_type_for_python_type, self.arg_pytypes)
        self.ret_ch_type = crosshair_type_for_python_type(self.ret_pytype)
        all_pytypes = tuple(self.arg_pytypes) + (self.ret_pytype,)
        return z3.Function(varname + self.statespace.uniq(),
                           *map(type_to_smt_sort, self.arg_pytypes),
                           type_to_smt_sort(self.ret_pytype))

    def __call__(self, *args):
        if len(args) != len(self.arg_pytypes):
            raise TypeError('wrong number of arguments')
        args = (coerce_to_smt_var(self.statespace, a)[0] for a in args)
        smt_ret = self.var(*args)
        # TODO: detect that `smt_ret` might be a HeapRef here
        return self.ret_ch_type(self.statespace, self.ret_pytype, smt_ret)

    def __repr__(self):
        finterp = self.statespace.find_model_value_for_function(self.var)
        if finterp is None:
            # (z3 model completion will not interpret a function for me currently)
            return '<any function>'
        # 0-arg interpretations seem to be simply values:
        if type(finterp) is not z3.FuncInterp:
            return 'lambda :' + repr(model_value_to_python(finterp))
        if finterp.arity() < 10:
            arg_names = [chr(ord('a') + i) for i in range(finterp.arity())]
        else:
            arg_names = ['a' + str(i + 1) for i in range(finterp.arity())]
        entries = finterp.as_list()
        body = repr(model_value_to_python(entries[-1]))
        for entry in reversed(entries[:-1]):
            conditions = ['{} == {}'.format(arg, repr(model_value_to_python(val)))
                          for (arg, val) in zip(arg_names, entry[:-1])]
            body = '{} if ({}) else ({})'.format(repr(model_value_to_python(entry[-1])),
                                                 ' and '.join(conditions),
                                                 body)
        return 'lambda ({}): {}'.format(', '.join(arg_names), body)


class SmtUniformTuple(SmtArrayBasedUniformTuple, collections.abc.Sequence, collections.abc.Hashable):
    def _coerce_into_compatible(self, other):
        if isinstance(other, tuple):
            return tuple(other)
        else:
            return super()._coerce_into_compatible(other)

    def __repr__(self):
        return tuple(self).__repr__()

    def __hash__(self):
        return tuple(self).__hash__()


class SmtStr(SmtSequence, AbcString):
    def __init__(self, statespace: StateSpace, typ: Type, smtvar: object):
        assert typ == str
        SmtBackedValue.__init__(self, statespace, typ, smtvar)
        self.item_pytype = str
        self.item_ch_type = SmtStr

    def __str__(self):
        return self.statespace.find_model_value(self.var)

    def __copy__(self):
        return SmtStr(self.statespace, str, self.var)

    def __repr__(self):
        return repr(self.__str__())

    def __hash__(self):
        return hash(self.__str__())

    def __add__(self, other):
        return self._binary_op(other, operator.add)

    def __radd__(self, other):
        return self._binary_op(other, lambda a, b: b + a)

    def __mod__(self, other):
        return self.__str__() % realize(other)

    def __mul__(self, other):
        if not isinstance(other, (int, SmtInt)):
            raise TypeError("can't multiply string by non-int")
        ret = ''
        idx = 0
        while idx < other:
            ret = self.__add__(ret)
            idx += 1
        return ret

    def __rmul__(self, other):
        return self.__mul__(other)

    def _cmp_op(self, other, op):
        coerced = coerce_to_smt_sort(self.statespace, other, self.var.sort())
        if coerced is None:
            raise TypeError
        return SmtBool(self.statespace, bool, op(self.var, coerced))

    def __lt__(self, other):
        return self._cmp_op(other, operator.lt)

    def __le__(self, other):
        return self._cmp_op(other, operator.le)

    def __gt__(self, other):
        return self._cmp_op(other, operator.gt)

    def __ge__(self, other):
        return self._cmp_op(other, operator.ge)

    def __contains__(self, other):
        return SmtBool(self.statespace, bool, z3.Contains(self.var, smt_coerce(other)))

    def __getitem__(self, i):
        idx_or_pair = process_slice_vs_symbolic_len(
            self.statespace, i, z3.Length(self.var))
        if isinstance(idx_or_pair, tuple):
            (start, stop) = idx_or_pair
            smt_result = z3.Extract(self.var, start, stop - start)
        else:
            smt_result = z3.Extract(self.var, idx_or_pair, 1)
        return SmtStr(self.statespace, str, smt_result)

    def find(self, substr, start=None, end=None):
        if end is None:
            return SmtInt(self.statespace, int,
                          z3.IndexOf(self.var, smt_coerce(substr), start or 0))
        else:
            return self.__getitem__(slice(start, end, 1)).index(s)


_CACHED_TYPE_ENUMS: Dict[FrozenSet[type], z3.SortRef] = {}


def get_type_enum(types: FrozenSet[type]) -> z3.SortRef:
    ret = _CACHED_TYPE_ENUMS.get(types)
    if ret is not None:
        return ret
    datatype = z3.Datatype('typechoice_' + '_'.join(sorted(map(str, types))))
    for typ in types:
        datatype.declare(name_of_type(typ))
    datatype = datatype.create()
    _CACHED_TYPE_ENUMS[types] = datatype
    return datatype


class SmtUnion:
    def __init__(self, pytypes: FrozenSet[type]):
        self.pytypes = list(pytypes)

    def __call__(self, statespace, pytype, varname):
        for typ in self.pytypes[:-1]:
            if statespace.smt_fork():
                return proxy_for_type(typ, statespace, varname)
        return proxy_for_type(self.pytypes[-1], statespace, varname)


class SmtProxyMarker:
    pass


_SMT_PROXY_TYPES: Dict[type, type] = {}


def get_smt_proxy_type(cls: type) -> type:
    if issubclass(cls, SmtProxyMarker):
        return cls
    global _SMT_PROXY_TYPES
    cls_name = name_of_type(cls)
    if cls not in _SMT_PROXY_TYPES:
        def symbolic_init(self):
            self.__class__ = cls
        class_body = { '__init__': symbolic_init }
        try:
            proxy_cls = type(cls_name + '_proxy', (SmtProxyMarker, cls), class_body)
        except TypeError as e:
            if 'is not an acceptable base type' in str(e):
                raise CrosshairUnsupported(f'Cannot subclass {cls_name}')
            else:
                raise
        _SMT_PROXY_TYPES[cls] = proxy_cls
    return _SMT_PROXY_TYPES[cls]


def make_fake_object(statespace: StateSpace, cls: type, varname: str) -> object:
    constructor = get_smt_proxy_type(cls)
    debug(constructor)
    try:
        proxy = constructor()
    except TypeError as e:
        # likely the type has a __new__ that expects arguments
        raise CrosshairUnsupported(f'Unable to proxy {name_of_type(cls)}: {e}')
    for name, typ in get_type_hints(cls).items():
        origin = getattr(typ, '__origin__', None)
        if origin is Callable:
            continue
        value = proxy_for_type(typ, statespace, varname +
                               '.' + name + statespace.uniq())
        object.__setattr__(proxy, name, value)
    return proxy


def choose_type(space: StateSpace, from_type: Type) -> Type:
    subtypes = get_subclass_map()[from_type]
    # Note that this is written strangely to leverage the default
    # preference for false when forking:
    if not subtypes or not space.smt_fork():
        return from_type
    for subtype in subtypes[:-1]:
        if not space.smt_fork():
            return choose_type(space, subtype)
    return choose_type(space, subtypes[-1])


_SIMPLE_PROXIES: MutableMapping[object, Callable] = {}

_RESOLVED_FNS: Set[IdentityWrapper[Callable]] = set()
def get_resolved_signature(fn: Callable) -> inspect.Signature:
    wrapped = IdentityWrapper(fn)
    if wrapped not in _RESOLVED_FNS:
        _RESOLVED_FNS.add(wrapped)
        try:
            fn.__annotations__ = get_type_hints(fn)
        except Exception as e:
            debug('Could not resolve annotations on', fn, ':', e)
    return inspect.signature(fn)

def get_constructor_params(cls: Type) -> Iterable[inspect.Parameter]:
    # TODO inspect __new__ as well
    init_fn = cls.__init__
    if init_fn is object.__init__:
        return ()
    init_sig = get_resolved_signature(init_fn)
    return list(init_sig.parameters.values())[1:]

def proxy_class_as_concrete(typ: Type, statespace: StateSpace,
                            varname: str) -> object:
    '''
    Try aggressively to create an instance of a class with symbolic members.
    '''
    data_members = get_type_hints(typ)
    if issubclass(typ, tuple):
        # Special handling for namedtuple which does magic that we don't
        # otherwise support.
        args = {k: proxy_for_type(t, statespace, varname + '.' + k)
                for (k, t) in data_members.items()}
        return typ(**args) # type: ignore
    constructor_params = get_constructor_params(typ)
    EMPTY = inspect.Parameter.empty
    args = {}
    for param in constructor_params:
        name = param.name
        smtname = varname + '.' + name
        annotation = param.annotation
        if annotation is not EMPTY:
            args[name] = proxy_for_type(annotation, statespace, smtname)
        else:
            if param.default is EMPTY:
                debug('unable to create concrete instance of', typ,
                      'due to lack of type annotation on', name)
                return _MISSING
            else:
                # TODO: consider whether we should fall back to a proxy
                # instead of letting this slide. Or try both paths?
                pass
    try:
        obj = typ(**args)
    except BaseException as e:
        debug('unable to create concrete proxy with init:', e)
        return _MISSING

    # Additionally, for any typed members, ensure that they are also
    # symbolic. (classes sometimes have valid states that are not directly
    # constructable)
    for (key, typ) in data_members.items():
        if isinstance(getattr(obj, key, None), (SmtBackedValue, SmtProxyMarker)):
            continue
        symbolic_value = proxy_for_type(typ, statespace, varname + '.' + key)
        try:
            setattr(obj, key, symbolic_value)
        except Exception as e:
            debug('Unable to assign symbolic value to concrete class:', e)
            # TODO: consider whether we should fall back to a proxy
            # instead of letting this slide. Or try both paths?
    return obj


def proxy_for_class(typ: Type, space: StateSpace, varname: str, meet_class_invariants: bool) -> object:
    # if the class has data members, we attempt to create a concrete instance with
    # symbolic members; otherwise, we'll create an object proxy that emulates it.
    obj = proxy_class_as_concrete(typ, space, varname)
    if obj is _MISSING:
        debug('Creating', typ, 'as an independent proxy class')
        obj = make_fake_object(space, typ, varname)
    else:
        debug('Creating', typ, 'with symbolic attribute assignments')
    class_conditions = get_class_conditions(typ)
    # symbolic custom classes may assume their invariants:
    if meet_class_invariants and class_conditions is not None:
        for inv_condition in class_conditions.inv:
            if inv_condition.expr is None:
                continue
            isok = False
            with ExceptionFilter() as efilter:
                isok = inv_condition.evaluate({'self': obj})
            if efilter.user_exc:
                raise IgnoreAttempt(
                    f'Class proxy could not meet invariant "{inv_condition.expr_source}" on '
                    f'{varname} (proxy of {typ}) because it raised: {repr(efilter.user_exc[0])}')
            else:
                symbolic_isok = coerce_to_smt_sort(space, isok, z3.BoolSort())
                isok = space.choose_possible(symbolic_isok, favor_true=True)
                if efilter.ignore or not isok:
                    raise IgnoreAttempt('Class proxy did not meet invariant ',
                                        inv_condition.expr_source)
    return obj

def register_type(typ: Type,
                  creator: Union[Type, Callable]) -> None:
    assert typ is origin_of(typ), \
            f'Only origin types may be registered, not "{typ}": try "{origin_of(typ)}" instead.'
    _SIMPLE_PROXIES[typ] = creator

def proxy_for_type(typ: Type, space: StateSpace, varname: str,
                   meet_class_invariants=True,
                   allow_subtypes=False) -> object:
    typ = normalize_pytype(typ)
    origin = origin_of(typ)
    # special cases
    if origin is tuple:
        if len(typ.__args__) == 2 and typ.__args__[1] == ...:
            return SmtUniformTuple(space, typ, varname)
        else:
            return tuple(proxy_for_type(t, space, varname + '_at_' + str(idx), allow_subtypes=True)
                         for (idx, t) in enumerate(typ.__args__))
    elif isinstance(typ, type) and issubclass(typ, enum.Enum):
        enum_values = list(typ)  # type:ignore
        for enum_value in enum_values[:-1]:
            if space.smt_fork():
                return enum_value
        return enum_values[-1]
    elif isinstance(origin, type) and issubclass(origin, Mapping):
        if hasattr(typ, '__args__'):
            args = typ.__args__
            if smt_sort_has_heapref(type_to_smt_sort(args[0])):
                return SimpleDict(proxy_for_type(List[Tuple[args[0], args[1]]], space, # type: ignore
                                                 varname, allow_subtypes=False))
    elif typ is object:
        return SmtObject(space, typ, varname)
    proxy_factory = _SIMPLE_PROXIES.get(origin)
    if proxy_factory:
        def recursive_proxy_factory(t: Type):
            return proxy_for_type(t, space, varname + space.uniq(),
                                  allow_subtypes=allow_subtypes)
        return proxy_factory(recursive_proxy_factory, *type_args_of(typ))
    # This part handles most of the basic types:
    Typ = crosshair_type_that_inhabits_python_type(typ)
    if Typ is not None:
        ret = Typ(space, typ, varname)
        if space.fork_parallel(false_probability=0.98):
            ret = realize(ret)
            debug('Prematurely realized', typ, 'value')
        return ret
    if allow_subtypes and typ is not object:
        typ = choose_type(space, typ)
    return proxy_for_class(typ, space, varname, meet_class_invariants)


def gen_args(sig: inspect.Signature, statespace: StateSpace) -> inspect.BoundArguments:
    args = sig.bind_partial()
    for param in sig.parameters.values():
        smt_name = param.name + statespace.uniq()
        proxy_maker = lambda typ, **kw: proxy_for_type(typ, statespace, smt_name, allow_subtypes=True, **kw)
        has_annotation = (param.annotation != inspect.Parameter.empty)
        value: object
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            if has_annotation:
                varargs_type = List[param.annotation]  # type: ignore
                value = proxy_maker(varargs_type)
            else:
                value = proxy_maker(List[Any])
        elif param.kind == inspect.Parameter.VAR_KEYWORD:
            if has_annotation:
                varargs_type = Dict[str, param.annotation]  # type: ignore
                value = cast(dict, proxy_maker(varargs_type))
                # Using ** on a dict requires concrete string keys. Force
                # instiantiation of keys here:
                value = {k.__str__(): v for (k, v) in value.items()}
            else:
                value = proxy_maker(Dict[str, Any])
        else:
            is_self = param.name == 'self'
            # Object parameters should meet thier invariants iff they are not the
            # class under test ("self").
            meet_class_invariants = not is_self
            allow_subtypes = not is_self
            if has_annotation:
                value = proxy_for_type(param.annotation, statespace, smt_name,
                                       meet_class_invariants, allow_subtypes)
            else:
                value = proxy_for_type(cast(type, Any), statespace, smt_name,
                                       meet_class_invariants, allow_subtypes)
        debug('created proxy for', param.name, 'as type:', type(value))
        args.arguments[param.name] = value
    return args

_UNABLE_TO_REPR = '<unable to repr>'
def message_sort_key(m: AnalysisMessage) -> tuple:
    return (m.state, _UNABLE_TO_REPR not in m.message, -len(m.message))

class MessageCollector:
    def __init__(self):
        self.by_pos = {}

    def extend(self, messages: Iterable[AnalysisMessage]) -> None:
        for message in messages:
            self.append(message)

    def append(self, message: AnalysisMessage) -> None:
        key = (message.filename, message.line, message.column)
        if key in self.by_pos:
            self.by_pos[key] = max(
                self.by_pos[key], message, key=message_sort_key)
        else:
            self.by_pos[key] = message

    def get(self) -> List[AnalysisMessage]:
        return [m for (k, m) in sorted(self.by_pos.items())]


@dataclass
class AnalysisOptions:
    per_condition_timeout: float = 1.5
    deadline: float = float('NaN')
    per_path_timeout: float = 0.75
    stats: Optional[collections.Counter] = None

    def incr(self, key: str):
        if self.stats is not None:
            self.stats[key] += 1


_DEFAULT_OPTIONS = AnalysisOptions()


def analyzable_members(module: types.ModuleType) -> Iterator[Tuple[str, Union[Type, Callable]]]:
    module_name = module.__name__
    for name, member in inspect.getmembers(module):
        if not (inspect.isclass(member) or inspect.isfunction(member)):
            continue
        if member.__module__ != module_name:
            continue
        yield (name, member)


def analyze_any(entity: object, options: AnalysisOptions) -> List[AnalysisMessage]:
    if inspect.isclass(entity):
        return analyze_class(cast(Type, entity), options)
    elif inspect.isfunction(entity):
        self_class: Optional[type] = None
        fn = cast(Callable, entity)
        if fn.__name__ != fn.__qualname__:
            self_thing = walk_qualname(sys.modules[fn.__module__],
                                       fn.__qualname__.split('.')[-2])
            assert isinstance(self_thing, type)
            self_class = self_thing
        return analyze_function(fn, options, self_type=self_class)
    elif inspect.ismodule(entity):
        return analyze_module(cast(types.ModuleType, entity), options)
    else:
        raise CrosshairInternal(
            'Entity type not analyzable: ' + str(type(entity)))


def analyze_module(module: types.ModuleType, options: AnalysisOptions) -> List[AnalysisMessage]:
    debug('Analyzing module ', module)
    messages = MessageCollector()
    for (name, member) in analyzable_members(module):
        messages.extend(analyze_any(member, options))
    message_list = messages.get()
    debug('Module', module.__name__, 'has', len(message_list), 'messages')
    return message_list


def message_class_clamper(cls: type):
    '''
    We clamp messages for a clesses method to appear on the class itself.
    So, even if the method is defined on a superclass, or defined dynamically (via
    decorator etc), we report it on the class definition instead.
    '''
    cls_file = inspect.getsourcefile(cls)
    (lines, cls_start_line) = inspect.getsourcelines(cls)

    def clamp(message: AnalysisMessage):
        if not samefile(message.filename, cls_file):
            return replace(message, filename=cls_file, line=cls_start_line)
        else:
            return message
    return clamp


def analyze_class(cls: type, options: AnalysisOptions = _DEFAULT_OPTIONS) -> List[AnalysisMessage]:
    debug('Analyzing class ', cls.__name__)
    messages = MessageCollector()
    class_conditions = get_class_conditions(cls)
    for method, conditions in class_conditions.methods.items():
        if conditions.has_any():
            cur_messages = analyze_function(getattr(cls, method),
                                            options=options,
                                            self_type=cls)
            clamper = message_class_clamper(cls)
            messages.extend(map(clamper, cur_messages))

    return messages.get()


def analyze_function(fn: Callable,
                     options: AnalysisOptions = _DEFAULT_OPTIONS,
                     self_type: Optional[type] = None) -> List[AnalysisMessage]:
    debug('Analyzing ', fn.__name__)
    all_messages = MessageCollector()

    if self_type is not None:
        class_conditions = get_class_conditions(self_type)
        conditions = class_conditions.methods[fn.__name__]
    else:
        conditions = get_fn_conditions(fn, self_type=self_type)
        if conditions is None:
            debug('Skipping ', str(fn),
                  ': Unable to determine the function signature.')
            return []

    for syntax_message in conditions.syntax_messages():
        all_messages.append(AnalysisMessage(MessageType.SYNTAX_ERR,
                                            syntax_message.message,
                                            syntax_message.filename,
                                            syntax_message.line_num, 0, ''))
    conditions = conditions.compilable()
    for post_condition in conditions.post:
        messages = analyze_single_condition(fn, options, replace(
            conditions, post=[post_condition]))
        all_messages.extend(messages)
    return all_messages.get()


def analyze_single_condition(fn: Callable,
                             options: AnalysisOptions,
                             conditions: Conditions) -> Sequence[AnalysisMessage]:
    debug('Analyzing postcondition: "', conditions.post[0].expr_source, '"')
    debug('assuming preconditions: ', ','.join(
        [p.expr_source for p in conditions.pre]))
    options.deadline = time.time() + options.per_condition_timeout

    analysis = analyze_calltree(fn, options, conditions)

    (condition,) = conditions.post
    if analysis.verification_status is VerificationStatus.UNKNOWN:
        addl_ctx = ' ' + condition.addl_context if condition.addl_context else ''
        message = 'I cannot confirm this' + addl_ctx
        analysis.messages = [AnalysisMessage(MessageType.CANNOT_CONFIRM, message,
                                             condition.filename, condition.line, 0, '')]

    return analysis.messages

_IMMUTABLE_TYPES = (int, float, complex, bool, tuple, frozenset, type(None))
def forget_contents(value: object, space: StateSpace):
    if isinstance(value, SmtBackedValue):
        clean_smt = type(value)(space, value.python_type,
                                str(value.var) + space.uniq())
        value.var = clean_smt.var
    elif isinstance(value, SmtProxyMarker):
        cls = python_type(value)
        clean = proxy_for_type(cls, space, space.uniq())
        for name, val in value.__dict__.items():
            value.__dict__[name] = clean.__dict__[name]
    elif hasattr(value, '__dict__'):
        for subvalue in value.__dict__.values():
            forget_contents(subvalue, space)
    elif isinstance(value, _IMMUTABLE_TYPES):
        return # immutable
    else:
        # TODO: handle mutable values without __dict__
        raise CrosshairUnsupported
            


class ShortCircuitingContext:
    engaged = False
    intercepted = False

    def __init__(self, space_getter: Callable[[], StateSpace]):
        self.space_getter = space_getter

    def __enter__(self):
        assert not self.engaged
        self.engaged = True

    def __exit__(self, exc_type, exc_value, tb):
        assert self.engaged
        self.engaged = False

    def make_interceptor(self, original: Callable) -> Callable:
        subconditions = get_fn_conditions(original)
        if subconditions is None:
            return original
        sig = subconditions.sig

        def wrapper(*a: object, **kw: Dict[str, object]) -> object:
            #debug('short circuit wrapper ', original)
            if (not self.engaged) or self.space_getter().running_framework_code:
                return original(*a, **kw)
            # We *heavily* bias towards concrete execution, because it's often the case
            # that a single short-circuit will render the path useless. TODO: consider
            # decaying short-crcuit probability over time.
            use_short_circuit = self.space_getter().fork_with_confirm_or_else(0.95)
            if not use_short_circuit:
                debug('short circuit: Choosing not to intercept', original)
                return original(*a, **kw)
            try:
                self.engaged = False
                debug('short circuit: Intercepted a call to ', original)
                self.intercepted = True
                return_type = sig.return_annotation

                # Deduce type vars if necessary
                if len(typing_inspect.get_parameters(sig.return_annotation)) > 0 or typing_inspect.is_typevar(sig.return_annotation):
                    typevar_bindings: typing.ChainMap[object, type] = collections.ChainMap(
                    )
                    bound = sig.bind(*a, **kw)
                    bound.apply_defaults()
                    for param in sig.parameters.values():
                        argval = bound.arguments[param.name]
                        value_type = argval.python_type if isinstance(
                            argval, SmtBackedValue) else type(argval)
                        #debug('unify', value_type, param.annotation)
                        if not dynamic_typing.unify(value_type, param.annotation, typevar_bindings):
                            debug(
                                'aborting intercept due to signature unification failure')
                            return original(*a, **kw)
                        #debug('unify bindings', typevar_bindings)
                    return_type = dynamic_typing.realize(
                        sig.return_annotation, typevar_bindings)
                    debug('short circuit: Deduced return type was ', return_type)

                # adjust arguments that may have been mutated
                assert subconditions is not None
                bound = sig.bind(*a, **kw)
                mutable_args = subconditions.mutable_args
                for argname, arg in bound.arguments.items():
                    if mutable_args is None or argname in mutable_args:
                        forget_contents(arg, self.space_getter())

                if return_type is type(None):
                    return None
                # note that the enforcement wrapper ensures postconditions for us, so we
                # can just return a free variable here.
                return proxy_for_type(return_type, self.space_getter(), 'proxyreturn' + self.space_getter().uniq())
            finally:
                self.engaged = True
        functools.update_wrapper(wrapper, original)
        return wrapper

@dataclass
class CallTreeAnalysis:
    messages: Sequence[AnalysisMessage]
    verification_status: VerificationStatus
    num_confirmed_paths: int = 0


def replay(fn: Callable,
           message: AnalysisMessage,
           conditions: Conditions) -> CallAnalysis:
    debug('replay log', message.test_fn, message.execution_log)
    assert message.execution_log is not None
    assert fn.__qualname__ == message.test_fn
    conditions = replace(conditions, post=[c for c in conditions.post
                                           if c.expr_source == message.condition_src])
    space = ReplayStateSpace(message.execution_log)
    short_circuit = ShortCircuitingContext(lambda: space)
    envs = [fn_globals(fn), contracted_builtins.__dict__]
    enforced_conditions = EnforcedConditions(*envs)
    def in_symbolic_mode(): return not space.running_framework_code
    patched_builtins = PatchedBuiltins(
        contracted_builtins.__dict__, in_symbolic_mode)
    with patched_builtins:
        return attempt_call(conditions, space, fn, short_circuit, enforced_conditions)


def analyze_calltree(fn: Callable,
                     options: AnalysisOptions,
                     conditions: Conditions) -> CallTreeAnalysis:
    debug('Begin analyze calltree ', fn.__name__)

    all_messages = MessageCollector()
    search_root = SinglePathNode(True)
    space_exhausted = False
    failing_precondition: Optional[ConditionExpr] = conditions.pre[0] if conditions.pre else None
    failing_precondition_reason: str = ''
    num_confirmed_paths = 0

    cur_space: List[StateSpace] = [cast(StateSpace, None)]
    short_circuit = ShortCircuitingContext(lambda: cur_space[0])
    _ = get_subclass_map()  # ensure loaded
    top_analysis: Optional[CallAnalysis] = None
    enforced_conditions = EnforcedConditions(
        fn_globals(fn), contracted_builtins.__dict__,
        interceptor=short_circuit.make_interceptor)
    def in_symbolic_mode():
        return (cur_space[0] is not None and
                not cur_space[0].running_framework_code)
    patched_builtins = PatchedBuiltins(
        contracted_builtins.__dict__, in_symbolic_mode)
    with enforced_conditions, patched_builtins, enforced_conditions.disabled_enforcement():
        for i in itertools.count(1):
            start = time.time()
            if start > options.deadline:
                debug('Exceeded condition timeout, stopping')
                break
            options.incr('num_paths')
            debug('iteration ', i)
            space = TrackingStateSpace(execution_deadline=start + options.per_path_timeout,
                                       model_check_timeout=options.per_path_timeout / 2,
                                       search_root=search_root)
            cur_space[0] = space
            try:
                # The real work happens here!:
                call_analysis = attempt_call(
                    conditions, space, fn, short_circuit, enforced_conditions)
                if failing_precondition is not None:
                    cur_precondition = call_analysis.failing_precondition
                    if cur_precondition is None:
                        if call_analysis.verification_status is not None:
                            # We escaped the all the pre conditions on this try:
                            failing_precondition = None
                    elif (cur_precondition.line == failing_precondition.line and
                          call_analysis.failing_precondition_reason):
                        failing_precondition_reason = call_analysis.failing_precondition_reason
                    elif cur_precondition.line > failing_precondition.line:
                        failing_precondition = cur_precondition
                        failing_precondition_reason = call_analysis.failing_precondition_reason

            except UnexploredPath:
                call_analysis = CallAnalysis(VerificationStatus.UNKNOWN)
            except IgnoreAttempt:
                call_analysis = CallAnalysis()
            status = call_analysis.verification_status
            if status == VerificationStatus.CONFIRMED:
                num_confirmed_paths += 1
            top_analysis, space_exhausted = space.bubble_status(call_analysis)
            overall_status = top_analysis.verification_status if top_analysis else None
            debug('Iter complete', overall_status.name if overall_status else 'None',
                  'exhausted=', space_exhausted)
            if space_exhausted or top_analysis == VerificationStatus.REFUTED:
                break
    top_analysis = search_root.child.get_result()
    if top_analysis.messages:
        #log = space.execution_log()
        all_messages.extend(
            replace(m,
                    #execution_log=log,
                    test_fn=fn.__qualname__,
                    condition_src=conditions.post[0].expr_source)
            for m in top_analysis.messages)
    if top_analysis.verification_status is None:
        top_analysis.verification_status = VerificationStatus.UNKNOWN
    if failing_precondition:
        assert num_confirmed_paths == 0
        addl_ctx = ' ' + failing_precondition.addl_context if failing_precondition.addl_context else ''
        message = f'Unable to meet precondition {addl_ctx}'
        if failing_precondition_reason:
            message += f' (possibly because {failing_precondition_reason}?)'
        all_messages.extend([AnalysisMessage(MessageType.PRE_UNSAT, message,
                                             failing_precondition.filename, failing_precondition.line, 0, '')])
        top_analysis = CallAnalysis(VerificationStatus.REFUTED)

    assert top_analysis.verification_status is not None
    debug(('Exhausted' if space_exhausted else 'Aborted'),
          ' calltree search with', top_analysis.verification_status.name,
          'and', len(all_messages.get()), 'messages.',
          'Number of iterations: ', i)
    return CallTreeAnalysis(messages=all_messages.get(),
                            verification_status=top_analysis.verification_status,
                            num_confirmed_paths=num_confirmed_paths)


def get_input_description(statespace: StateSpace,
                          fn_name: str,
                          bound_args: inspect.BoundArguments,
                          return_val: object = _MISSING,
                          addl_context: str = '') -> str:
    debug('get_input_description: return_val: ', type(return_val))
    call_desc = ''
    if return_val is not _MISSING:
        try:
            repr_str = repr(return_val)
        except Exception as e:
            if isinstance(e, IgnoreAttempt):
                raise
            debug(f'Exception attempting to repr function output: {e}')
            repr_str = _UNABLE_TO_REPR
        if repr_str != 'None':
            call_desc = call_desc + ' (which returns ' + repr_str + ')'
    messages: List[str] = []
    for argname, argval in list(bound_args.arguments.items()):
        try:
            repr_str = repr(argval)
        except Exception as e:
            if isinstance(e, IgnoreAttempt):
                raise
            debug(f'Exception attempting to repr input "{argname}": {repr(e)}')
            repr_str = _UNABLE_TO_REPR
        messages.append(argname + ' = ' + repr_str)
    call_desc = fn_name + '(' + ', '.join(messages) + ')' + call_desc

    if addl_context:
        return addl_context + ' when calling ' + call_desc # ' and '.join(messages)
    elif messages:
        return 'when calling ' + call_desc # ' and '.join(messages)
    else:
        return 'for any input'


class UnEqual:
    pass


_UNEQUAL = UnEqual()


def deep_eq(old_val: object, new_val: object, visiting: Set[Tuple[int, int]]) -> bool:
    # TODO: test just about all of this
    if old_val is new_val:
        return True
    if type(old_val) != type(new_val):
        return False
    visit_key = (id(old_val), id(new_val))
    if visit_key in visiting:
        return True
    visiting.add(visit_key)
    try:
        if isinstance(old_val, SmtBackedValue):
            return old_val == new_val
        elif hasattr(old_val, '__dict__') and hasattr(new_val, '__dict__'):
            return deep_eq(old_val.__dict__, new_val.__dict__, visiting)
        elif isinstance(old_val, dict):
            assert isinstance(new_val, dict)
            for key in set(itertools.chain(old_val.keys(), *new_val.keys())):
                if (key in old_val) ^ (key in new_val):
                    return False
                if not deep_eq(old_val.get(key, _UNEQUAL), new_val.get(key, _UNEQUAL), visiting):
                    return False
            return True
        elif isinstance(old_val, Iterable):
            assert isinstance(new_val, Iterable)
            if isinstance(old_val, Sized):
                if len(old_val) != len(new_val):
                    return False
            return all(deep_eq(o, n, visiting) for (o, n) in
                       itertools.zip_longest(old_val, new_val, fillvalue=_UNEQUAL))
        elif type(old_val) is object:
            # deepclone'd object instances are close enough to equal for our purposes
            return True
        else:
            # hopefully this is just ints, bools, etc
            return old_val == new_val
    finally:
        visiting.remove(visit_key)


def attempt_call(conditions: Conditions,
                 space: StateSpace,
                 fn: Callable,
                 short_circuit: ShortCircuitingContext,
                 enforced_conditions: EnforcedConditions) -> CallAnalysis:
    bound_args = gen_args(conditions.sig, space)

    code_obj = fn.__code__
    fn_filename, fn_start_lineno = (
        code_obj.co_filename, code_obj.co_firstlineno)
    try:
        (lines, _) = inspect.getsourcelines(fn)
    except OSError:
        lines = []
    fn_end_lineno = fn_start_lineno + len(lines)

    def locate_msg(detail: str, suggested_filename: str, suggested_lineno: int) -> Tuple[str, str, int, int]:
        if ((os.path.abspath(suggested_filename) == os.path.abspath(fn_filename)) and
            (fn_start_lineno <= suggested_lineno <= fn_end_lineno)):
            return (detail, suggested_filename, suggested_lineno, 0)
        else:
            try:
                exprline = linecache.getlines(suggested_filename)[
                    suggested_lineno - 1].strip()
            except IndexError:
                exprline = '<unknown>'
            detail = f'"{exprline}" yields {detail}'
            return (detail, fn_filename, fn_start_lineno, 0)

    with space.framework():
        original_args = copy.deepcopy(bound_args)
    space.checkpoint()

    expected_exceptions = conditions.raises
    for precondition in conditions.pre:
        with ExceptionFilter(expected_exceptions) as efilter:
            with enforced_conditions.enabled_enforcement(), short_circuit:
                precondition_ok = precondition.evaluate(bound_args.arguments)
            if not precondition_ok:
                debug('Failed to meet precondition', precondition.expr_source)
                return CallAnalysis(failing_precondition=precondition)
        if efilter.ignore:
            debug('Ignored exception in precondition', efilter.analysis)
            return efilter.analysis
        elif efilter.user_exc is not None:
            (user_exc, tb) = efilter.user_exc
            debug('Exception attempting to meet precondition',
                  precondition.expr_source, ':',
                  user_exc,
                  tb.format())
            return CallAnalysis(failing_precondition=precondition,
                                failing_precondition_reason=
                                f'it raised "{repr(user_exc)} at {tb.format()[-1]}"')

    with ExceptionFilter(expected_exceptions) as efilter:
        a, kw = bound_args.args, bound_args.kwargs
        with enforced_conditions.enabled_enforcement(), short_circuit:
            assert not space.running_framework_code
            __return__ = fn(*a, **kw)
        lcls = {**bound_args.arguments,
                '__return__': __return__,
                '_': __return__,
                '__old__': AttributeHolder(original_args.arguments),
                fn.__name__: fn}

    if efilter.ignore:
        debug('Ignored exception in function', efilter.analysis)
        return efilter.analysis
    elif efilter.user_exc is not None:
        (e, tb) = efilter.user_exc
        detail = name_of_type(type(e)) + ': ' + str(e)
        frame_filename, frame_lineno = frame_summary_for_fn(tb, fn)
        debug('exception while evaluating function body:', detail, frame_filename, 'line', frame_lineno)
        detail += ' ' + get_input_description(space, fn.__name__, original_args, _MISSING)
        return CallAnalysis(VerificationStatus.REFUTED,
                            [AnalysisMessage(MessageType.EXEC_ERR,
                                             *locate_msg(detail, frame_filename, frame_lineno),
                                             ''.join(tb.format()))])

    for argname, argval in bound_args.arguments.items():
        if (conditions.mutable_args is not None and
            argname not in conditions.mutable_args):
            old_val, new_val = original_args.arguments[argname], argval
            if not deep_eq(old_val, new_val, set()):
                detail = 'Argument "{}" is not marked as mutable, but changed from {} to {}'.format(
                    argname, old_val, new_val)
                debug('Mutablity problem:', detail)
                return CallAnalysis(VerificationStatus.REFUTED,
                                    [AnalysisMessage(MessageType.POST_ERR, detail,
                                                     fn_filename, fn_start_lineno, 0, '')])

    (post_condition,) = conditions.post
    with ExceptionFilter(expected_exceptions) as efilter:
        # TODO: re-enable post-condition short circuiting. This will require refactoring how
        # enforced conditions and short curcuiting interact, so that post-conditions are
        # selectively run when, and only when, performing a short circuit.
        #with enforced_conditions.enabled_enforcement(), short_circuit:
        isok = bool(post_condition.evaluate(lcls))
    if efilter.ignore:
        debug('Ignored exception in postcondition', efilter.analysis)
        return efilter.analysis
    elif efilter.user_exc is not None:
        (e, tb) = efilter.user_exc
        detail = repr(e) + ' ' + get_input_description(space, fn.__name__,
                                                       original_args, __return__, post_condition.addl_context)
        debug('exception while calling postcondition:', detail)
        failures = [AnalysisMessage(MessageType.POST_ERR,
                                    *locate_msg(detail, post_condition.filename, post_condition.line),
                                    ''.join(tb.format()))]
        return CallAnalysis(VerificationStatus.REFUTED, failures)
    if isok:
        debug('Confirmed.')
        return CallAnalysis(VerificationStatus.CONFIRMED)
    else:
        detail = 'false ' + \
                 get_input_description(
                     space, fn.__name__, original_args, __return__, post_condition.addl_context)
        debug(detail)
        failures = [AnalysisMessage(MessageType.POST_FAIL,
                                    *locate_msg(detail, post_condition.filename, post_condition.line), '')]
        return CallAnalysis(VerificationStatus.REFUTED, failures)


_PYTYPE_TO_WRAPPER_TYPE = {
    type(None): (lambda *a: None),
    bool: SmtBool,
    int: SmtInt,
    float: SmtFloat,
    str: SmtStr,
    list: SmtList,
    dict: SmtDict,
    set: SmtMutableSet,
    frozenset: SmtFrozenSet,
    type: SmtType,
}

# Type ignore pending https://github.com/python/mypy/issues/6864
_PYTYPE_TO_WRAPPER_TYPE[collections.abc.Callable] = SmtCallable  # type:ignore

_WRAPPER_TYPE_TO_PYTYPE = dict((v, k)
                               for (k, v) in _PYTYPE_TO_WRAPPER_TYPE.items())
