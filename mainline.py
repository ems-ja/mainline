from vkproxy.log import get_logger

_LOG = get_logger()

import threading
import functools
import collections
import inspect
import wrapt
import os
import itertools
import six


class classproperty(object):
    def __init__(self, f):
        self.f = f

    def __get__(self, obj, owner):
        return self.f(owner)


def consume(iterator, n=None):
    "Advance the iterator n-steps ahead. If n is none, consume entirely."
    # Use functions that consume iterators at C speed.
    if n is None:
        # feed the entire iterator into a zero-length deque
        collections.deque(iterator, maxlen=0)
    else:
        # advance to the empty slice starting at position n
        next(itertools.islice(iterator, n, n), None)


class Scope(collections.MutableMapping):
    register = True

    @classproperty
    def name(cls):
        remove_end = 'Scope'
        name = cls.__name__
        if name.endswith(remove_end):
            name = name.rsplit(remove_end, 1)[0]
        return name.lower()

    instances_factory = dict

    def __init__(self, *args, **kwargs):
        self.instances = self.instances_factory()
        self.update(dict(*args, **kwargs))

    def __key_transform__(self, key):
        return key

    def __getitem__(self, key):
        key = self.__key_transform__(key)
        return self.instances[key]

    def __setitem__(self, key, value):
        key = self.__key_transform__(key)
        self.instances[key] = value

    def __delitem__(self, key):
        key = self.__key_transform__(key)
        del self.instances[key]

    def __iter__(self):
        return iter(self.instances)

    def __len__(self):
        return len(self.instances)


class SingletonScope(Scope):
    pass


class ProcessScope(Scope):
    def __key_transform__(self, key):
        return '%s_%s' % (os.getpid(), key)


class ThreadScope(Scope):
    def __init__(self, *args, **kwargs):
        self._thread_local = threading.local()
        super(ThreadScope, self).__init__(*args, **kwargs)

    def instances_factory(self):
        if not hasattr(self._thread_local, 'instances'):
            self._thread_local.instances = dict()
        return self._thread_local.instances


class NoneScope(Scope):
    def __setitem__(self, key, value):
        return


class NamedScope(Scope):
    register = False

    def __init__(self, name):
        self.name = name


class ScopeRegistry(object):
    def __init__(self, parent):
        self._lookup = {}
        self._build()

    def _build(self):
        scope_classes = Scope.__subclasses__()
        scope_classes = [s for s in scope_classes if s.register]
        consume(map(self.add, scope_classes))

    _scope_type = collections.MutableMapping

    def is_scope(self, obj):
        return isinstance(obj, self._scope_type) or issubclass(obj, self._scope_type)

    def add(self, obj, name=None):
        if not self.is_scope(obj):
            raise ValueError("Scope %s does not supply %s interface" % (obj, self._scope_type))
        if name is None:
            name = getattr(obj, 'name')
        self._lookup[name] = obj

    def resolve(self, scope):
        # Lookup names
        if scope in self._lookup:
            scope = self._lookup[scope]

        if self.is_scope(scope):
            return scope

        raise KeyError("Scope %s does not exist" % scope)

    def get(self, scope):
        scope = self.resolve(scope)

        # Class means we maintain a single instance
        if inspect.isclass(scope):
            # We initialize them here (in get) to create them lazily
            if scope not in self._lookup:
                self._lookup[scope] = scope()
            scope = self.resolve(scope)

        # Instance means a scope factory created it
        return scope


class Registry(object):
    def __init__(self, parent):
        self._parent = parent
        self._scopes = parent._scopes

        self._factories = {}

    def add(self, key, factory, scope):
        scope = self._scopes.resolve(scope)
        self._factories[key] = factory, scope

    def add_instance(self, key, obj):
        # Scope is forced to singleton for instances.
        # We may want to be able to register an instance to a specific scope at some point.
        scope = 'singleton'
        factory = lambda: obj
        return self.add(key, factory, scope)

    def get(self, key):
        return self._factories[key]

    def has(self, key):
        return key in self._factories


class DependencyRegistry(object):
    def __init__(self, parent):
        self._registry = parent._registry
        self._depends_obj = collections.defaultdict(set)

    def add(self, obj, *keys):
        for k in keys:
            self._depends_obj[obj].add(k)

    def missing(self, obj):
        deps = self.get(obj)
        if not deps:
            return
        return itertools.ifilterfalse(self._registry.has, deps)

    def get(self, obj):
        if obj not in self._depends_obj:
            return
        return self._depends_obj[obj]


class DiError(Exception):
    pass


class UnresolvableError(DiError):
    pass


class Di(object):
    def __init__(self):
        self._scopes = ScopeRegistry(self)
        self._registry = Registry(self)
        self._depends = DependencyRegistry(self)

    def scope(self):
        return Scope()

    def named_scope(self, name):
        return NamedScope(name)

    def register(self, key, factory=None, scope='singleton'):
        if factory is None:
            return functools.partial(self.register, key, scope=scope)
        self._registry.add(key, factory, scope)
        return factory

    def register_instance(self, key, obj):
        return self._registry.add_instance(key, obj)

    def _coerce_key_or_keys(self, key_or_keys, *more_keys):
        keys = []

        if more_keys:
            # Multiple arguments given
            keys.append(key_or_keys)
            keys.extend(more_keys)
        elif isinstance(key_or_keys, six.string_types):
            # Singular str
            keys.append(key_or_keys)
        else:
            # Singular item; treat as an iterable
            keys.extend(key_or_keys)

        return keys

    def depends_on(self, key_or_keys, obj=None):
        if obj is None:
            return functools.partial(self.depends_on, keys_or_keys)
        keys = self._coerce_key_or_keys(key_or_keys)
        self._depends.add(obj, *keys)
        return obj

    def get_deps(self, obj):
        return self._depends.get(obj)

    def _resolve_one(self, key):
        factory, factory_scope = self._registry.get(key)

        missing = self._depends.missing(key)
        if missing:
            raise UnresolvableError("Missing dependencies for %s: %s" % (key, missing))

        scope = self._scopes.get(factory_scope)
        if key in scope:
            value = scope[key]
        else:
            scope[key] = value = factory()

        return value

    def resolve(self, key_or_keys, *more_keys, **kwargs):
        # only py3k can have default args with *args
        with_keys = kwargs.get('with_keys', False)
        keys = self._coerce_key_or_keys(key_or_keys, *more_keys)

        ret = list(map(self._resolve_one, keys))
        if with_keys:
            ret = zip(keys, ret)

        if more_keys:
            # Always return a sequence when given multiple arguments
            return ret
        else:
            return ret[0]

    def resolve_deps(self, obj, **kwargs):
        deps = self.get_deps(obj)
        return self.resolve(deps, **kwargs)

    def _wrap_classproperty(self, cls, key, name, replace_on_access, owner=None):
        # owner is set to the instance if applicable
        val = self.resolve(key)
        if replace_on_access:
            setattr(cls, name, val)
        return val

    def provide_classproperty(self, key, klass=None, name=None, replace_on_access=False):
        if klass is None:
            return functools.partial(
                self.provide_classproperty,
                key, name=name,
                replace_on_access=replace_on_access,
            )

        if not name:
            name = key

        # Register as dependency for klass
        self._depends.add(klass, key)

        # Add in arguments
        partial = functools.partial(self._wrap_classproperty, klass, key, name, replace_on_access)
        # Create classproperty from it
        clsprop = classproperty(partial)

        # Attach descriptor to object
        setattr(klass, name, clsprop)

        # Return class as this is a decorator
        return klass

    def provide_partial(self, wrapped=None, args=None):
        if wrapped is None:
            return functools.partial(self.provide_partial, args=args)

        if args:
            self._depends.add(wrapped, *args)
        else:
            args = self._depends.get(wrapped)

        injected_args = self.resolve(*args)
        return functools.partial(wrapped, *injected_args)

    def provide_args(self, wrapped=None, args=None):
        if wrapped is None:
            return functools.partial(self.provide_args, args=args)

        if args:
            self._depends.add(wrapped, *args)
        else:
            args = self._depends.get(wrapped)

        # HACK Remove the number of arguments from the wrapped function's argspec
        spec = inspect.getargspec(wrapped)
        sargs = spec.args[len(args):]

        # Update argspec
        spec = inspect.ArgSpec(sargs, *spec[1:])

        @wrapt.decorator(adapter=spec)
        def wrapper(wrapped, instance, wargs, wkwargs):
            injected_args = self.resolve(*args)

            def _execute(*_wargs, **_wkwargs):
                if _wargs:
                    injected_args.extend(_wargs)
                return wrapped(*injected_args, **_wkwargs)

            return _execute(*wargs, **wkwargs)

        return wrapper(wrapped)


di = Di()
di.register_instance('di', di)
