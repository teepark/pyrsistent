import six
from pyrsistent._checked_types import (InvariantException, CheckedType, _restore_pickle, store_invariants)
from pyrsistent._field_common import (set_fields, check_type, PFIELD_NO_INITIAL, serialize, check_global_invariants)
from pyrsistent._transformations import transform


def _is_pclass(bases):
    return len(bases) == 1 and bases[0] == CheckedType


class PClassMeta(type):
    def __new__(mcs, name, bases, dct):
        set_fields(dct, bases, name='_pclass_fields')
        store_invariants(dct, bases, '_pclass_invariants', '__invariant__')
        dct['__slots__'] = ('_pclass_frozen',) + tuple(key for key in dct['_pclass_fields'])

        # There must only be one __weakref__ entry in the inheritance hierarchy,
        # lets put it on the top level class.
        if _is_pclass(bases):
            dct['__slots__'] += ('__weakref__',)

        return super(PClassMeta, mcs).__new__(mcs, name, bases, dct)

_MISSING_VALUE = object()


def _check_and_set_attr(cls, field, name, value, result, invariant_errors):
    check_type(cls, field, name, value)
    is_ok, error_code = field.invariant(value)
    if not is_ok:
        invariant_errors.append(error_code)
    else:
        setattr(result, name, value)


@six.add_metaclass(PClassMeta)
class PClass(CheckedType):
    """
    A PClass is a python class with a fixed set of specified fields. PClasses are declared as python classes inheriting
    from PClass. It is defined the same way that PRecords are and behaves like a PRecord in all aspects except that it
    is not a PMap and hence not a collection but rather a plain Python object.


    More documentation and examples of PClass usage is available at https://github.com/tobgu/pyrsistent
    """
    def __new__(cls, **kwargs):    # Support *args?
        result = super(PClass, cls).__new__(cls)
        bypass_factories = kwargs.pop('_bypass_factories', False)
        missing_fields = []
        invariant_errors = []
        for name, field in cls._pclass_fields.items():
            if name in kwargs:
                if not bypass_factories:
                    value = field.factory(kwargs[name])
                else:
                    value = kwargs[name]
                _check_and_set_attr(cls, field, name, value, result, invariant_errors)
                del kwargs[name]
            elif field.initial is not PFIELD_NO_INITIAL:
                initial = field.initial() if callable(field.initial) else field.initial
                _check_and_set_attr(
                    cls, field, name, initial, result, invariant_errors)
            elif field.mandatory:
                missing_fields.append('{0}.{1}'.format(cls.__name__, name))

        if invariant_errors or missing_fields:
            raise InvariantException(tuple(invariant_errors), tuple(missing_fields), 'Field invariant failed')

        if kwargs:
            raise AttributeError("'{0}' are not among the specified fields for {1}".format(
                ', '.join(kwargs), cls.__name__))

        check_global_invariants(result, cls._pclass_invariants)

        result._pclass_frozen = True
        return result

    def set(self, *args, **kwargs):
        """
        Set a field in the instance. Returns a new instance with the updated value. The original instance remains
        unmodified. Accepts key-value pairs or single string representing the field name and a value.

        >>> from pyrsistent import PClass, field
        >>> class AClass(PClass):
        ...     x = field()
        ...
        >>> a = AClass(x=1)
        >>> a2 = a.set(x=2)
        >>> a3 = a.set('x', 3)
        >>> a
        AClass(x=1)
        >>> a2
        AClass(x=2)
        >>> a3
        AClass(x=3)
        """
        if args:
            kwargs[args[0]] = args[1]

        for key in self._pclass_fields:
            if key not in kwargs:
                value = getattr(self, key, _MISSING_VALUE)
                if value is not _MISSING_VALUE:
                    kwargs[key] = value

        return self.__class__(**kwargs)

    @classmethod
    def create(cls, kwargs, _bypass_factories=False):
        """
        Factory method. Will create a new PClass of the current type and assign the values
        specified in kwargs.
        """
        if isinstance(kwargs, cls):
            return kwargs

        return cls(_bypass_factories=_bypass_factories, **kwargs)

    def serialize(self, format=None):
        """
        Serialize the current PClass using custom serializer functions for fields where
        such have been supplied.
        """
        result = {}
        for name in self._pclass_fields:
            value = getattr(self, name, _MISSING_VALUE)
            if value is not _MISSING_VALUE:
                result[name] = serialize(self._pclass_fields[name].serializer, format, value)

        return result

    def transform(self, *transformations):
        """
        Apply transformations to the currency PClass. For more details on transformations see
        the documentation for PMap. Transformations on PClasses do not support key matching
        since the PClass is not a collection. Apart from that the transformations available
        for other persistent types work as expected.
        """
        return transform(self, transformations)

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            for name in self._pclass_fields:
                if getattr(self, name, _MISSING_VALUE) != getattr(other, name, _MISSING_VALUE):
                    return False

            return True

        return NotImplemented

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        # May want to optimize this by caching the hash somehow
        return hash(tuple((key, getattr(self, key, _MISSING_VALUE)) for key in self._pclass_fields))

    def __setattr__(self, key, value):
        if getattr(self, '_pclass_frozen', False):
            raise AttributeError("Can't set attribute, key={0}, value={1}".format(key, value))

        super(PClass, self).__setattr__(key, value)

    def __delattr__(self, key):
            raise AttributeError("Can't delete attribute, key={0}, use remove()".format(key))

    def _to_dict(self):
        result = {}
        for key in self._pclass_fields:
            value = getattr(self, key, _MISSING_VALUE)
            if value is not _MISSING_VALUE:
                result[key] = value

        return result

    def __repr__(self):
        return "{0}({1})".format(self.__class__.__name__,
                                 ', '.join('{0}={1}'.format(k, repr(v)) for k, v in self._to_dict().items()))

    def __reduce__(self):
        # Pickling support
        data = dict((key, getattr(self, key)) for key in self._pclass_fields if hasattr(self, key))
        return _restore_pickle, (self.__class__, data,)

    def evolver(self):
        """
        Returns an evolver for this object.
        """
        return _PClassEvolver(self, self._to_dict())

    def remove(self, name):
        """
        Remove attribute given by name from the current instance. Raises AttributeError if the
        attribute doesn't exist.
        """
        evolver = self.evolver()
        del evolver[name]
        return evolver.persistent()


class _PClassEvolver(object):
    __slots__ = ('_pclass_evolver_original', '_pclass_evolver_data', '_pclass_evolver_data_is_dirty')

    def __init__(self, original, initial_dict):
        self._pclass_evolver_original = original
        self._pclass_evolver_data = initial_dict
        self._pclass_evolver_data_is_dirty = False

    def __getitem__(self, item):
        return self._pclass_evolver_data[item]

    def set(self, key, value):
        if self._pclass_evolver_data.get(key, _MISSING_VALUE) is not value:
            self._pclass_evolver_data[key] = value
            self._pclass_evolver_data_is_dirty = True

        return self

    def __setitem__(self, key, value):
        self.set(key, value)

    def remove(self, item):
        if item in self._pclass_evolver_data:
            del self._pclass_evolver_data[item]
            self._pclass_evolver_data_is_dirty = True
            return self

        raise AttributeError(item)

    def __delitem__(self, item):
        self.remove(item)

    def persistent(self):
        if self._pclass_evolver_data_is_dirty:
            return self._pclass_evolver_original.__class__(**self._pclass_evolver_data)

        return self._pclass_evolver_original

    def __setattr__(self, key, value):
        if key not in self.__slots__:
            self.set(key, value)
        else:
            super(_PClassEvolver, self).__setattr__(key, value)

    def __getattr__(self, item):
        return self[item]
